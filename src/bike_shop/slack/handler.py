from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from bike_shop.accumulator import MessageAccumulator
from bike_shop.agents import PROJECT_LEAD
from bike_shop.config import AgentConfig
from bike_shop.github_auth import GitHubAuth
from bike_shop.memory_agent import MemoryAgent
from bike_shop.model_switch import ModelSwitcher
from bike_shop.providers import LLMProvider
from bike_shop.router import SemanticRouter
from bike_shop.session import SessionStore
from bike_shop.slack.context import (
    build_mention_instruction,
    get_channel_context,
    get_team_mentions,
    get_thread_context,
    is_mentioned,
    resolve_user,
    strip_mention,
)
from bike_shop.worktree import ensure_worktree

logger = logging.getLogger(__name__)

MAX_AGENT_INTERACTIONS = int(os.environ.get("MAX_AGENT_INTERACTIONS", "20"))

# Track agent-to-agent messages per thread: thread_ts -> count
_agent_interactions: dict[str, int] = {}

# Bot user IDs of all agents — resolved lazily
_bot_user_ids: set[str] | None = None


def _get_bot_user_ids() -> set[str]:
    global _bot_user_ids
    if _bot_user_ids is None:
        mentions = get_team_mentions()
        _bot_user_ids = set(mentions.values())
        logger.info("Bot user IDs resolved: %s", _bot_user_ids)
    return _bot_user_ids


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_BASE_MCP_CONFIG = os.path.join(_PROJECT_ROOT, "mcp.json")


def _resolve_env_vars(obj):
    """Recursively resolve ${VAR} placeholders in dicts/lists/strings."""
    if isinstance(obj, str) and obj.startswith("${") and obj.endswith("}"):
        return os.environ.get(obj[2:-1], "")
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars(v) for v in obj]
    return obj


def _build_mcp_config(config: AgentConfig) -> str:
    """Build per-agent mcp.json with env vars resolved. Returns path."""
    with open(_BASE_MCP_CONFIG) as f:
        mcp = json.load(f)

    mcp = _resolve_env_vars(mcp)

    cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "bike-shop")
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(
        cache_dir,
        f"mcp-{config.name.lower().replace(' ', '-')}.json",
    )
    with open(path, "w") as f:
        json.dump(mcp, f)
    return path


def _read_project_context() -> str:
    """Read MANIFEST.md from project root to give agents project awareness."""
    manifest_path = os.path.join(_PROJECT_ROOT, "MANIFEST.md")
    if not os.path.exists(manifest_path):
        return ""
    try:
        with open(manifest_path) as f:
            return f"\n\n--- PROJECT MANIFEST (keep this in mind at all times) ---\n{f.read()}\n--- END MANIFEST ---\n"
    except OSError:
        return ""


def _build_prompt(config: AgentConfig, context: str, question: str,
                  github_token: str | None,
                  shared_memory: str = "") -> str:
    """Assemble the full prompt from system prompt + instructions + context."""
    parts = [config.system_prompt]

    # Project context
    parts.append(_read_project_context())

    if github_token:
        parts.append(
            "\n\nIMPORTANT: For ALL GitHub operations (issues, PRs, comments, etc.) "
            "use the gh CLI via Bash. The GH_TOKEN environment variable is already set "
            "with your unique bot identity. Do NOT use 'gh auth login'. "
            "Example: gh api repos/OWNER/REPO/issues -f title='...' -f body='...'"
        )

    # Shared project memory from Mem0
    if shared_memory:
        parts.append(shared_memory)

    parts.append(build_mention_instruction(config.name))
    parts.append(f"\n\n--- CONVERSATION CONTEXT ---\n{context}")
    parts.append(f"\n\n--- NEW MESSAGE TO RESPOND ---\n{question}")

    return "".join(parts)


def _build_batch_prompt(config: AgentConfig, context: str,
                        messages: list[dict[str, Any]],
                        github_token: str | None,
                        shared_memory: str = "") -> str:
    """Assemble prompt for a batch of messages."""
    parts = [config.system_prompt]
    parts.append(_read_project_context())

    if github_token:
        parts.append(
            "\n\nIMPORTANT: For ALL GitHub operations (issues, PRs, comments, etc.) "
            "use the gh CLI via Bash. The GH_TOKEN environment variable is already set "
            "with your unique bot identity. Do NOT use 'gh auth login'. "
            "Example: gh api repos/OWNER/REPO/issues -f title='...' -f body='...'"
        )

    if shared_memory:
        parts.append(shared_memory)

    parts.append(build_mention_instruction(config.name))
    parts.append(f"\n\n--- CONVERSATION CONTEXT ---\n{context}")

    # Batch instructions
    parts.append(f"\n\n--- BATCH: {len(messages)} MESSAGES RECEIVED ---\n")
    parts.append(
        "You received multiple messages in quick succession. Analyze them:\n\n"
        "1. **Independent tasks** (no shared files, no dependency between outputs):\n"
        "   → Use the Agent tool to spawn one sub-agent per task in isolated worktrees\n"
        "   → Each sub-agent runs in parallel with isolation: \"worktree\"\n"
        "   → Collect results and respond with a consolidated summary\n\n"
        "2. **Dependent tasks** (task B needs output of task A):\n"
        "   → Execute sequentially, in dependency order\n\n"
        "3. **Related tasks** (all part of the same feature):\n"
        "   → Execute together in a single worktree\n\n"
        "Messages:\n"
    )
    for i, msg in enumerate(messages, 1):
        user = msg.get("user_name", "someone")
        text = msg.get("text", "")
        parts.append(f"  {i}. [{user}]: {text}\n")

    parts.append("\n--- END BATCH ---")

    return "".join(parts)


class SlackAgentHandler:
    """Wires a single agent to Slack via Socket Mode."""

    def __init__(self, config: AgentConfig, provider: LLMProvider) -> None:
        self._config = config
        self._provider = provider
        self._session = SessionStore(config.agent_key)
        self._github = GitHubAuth(config)
        self._switcher = ModelSwitcher()
        self._router = SemanticRouter()
        self._memory_agent = MemoryAgent(agent_key=config.agent_key)
        self._accumulator = MessageAccumulator(flush_callback=self._on_batch_flush)

        # Stash say/client per thread for batch callback
        self._thread_context: dict[str, dict[str, Any]] = {}
        self._thread_context_lock = threading.Lock()

    def _get_workspace(self, task_id: str | None = None) -> str:
        """Get or create an isolated worktree for this agent.

        Raises:
            RuntimeError: If worktree creation fails. Worktrees are mandatory;
                          there is no fallback to a shared directory.
        """
        return ensure_worktree(self._config.agent_key, task_id=task_id)

    def _call_llm(self, context: str, question: str, thread_ts: str,
                  model_override: str | None = None, agent_override: str | None = None,
                  router_meta: dict | None = None,
                  channel: str = "",
                  memory_requests: list | None = None,
                  workspace: str | None = None) -> str:
        """Call the LLM provider and handle session tracking."""
        config = self._config
        mcp_config = _build_mcp_config(config)
        github_token = self._github.get_token()
        session_id = self._session.get(thread_ts)

        # Memory recall: full recall on new threads + router-driven filtered recall
        shared_memory = self._memory_agent.recall(question, has_session=session_id is not None)
        if memory_requests:
            filtered = self._memory_agent.recall_filtered(memory_requests)
            shared_memory = (shared_memory + filtered) if shared_memory else filtered
        prompt = _build_prompt(config, context, question, github_token, shared_memory)

        # Get worktree workspace if not provided
        if workspace is None:
            workspace = self._get_workspace()

        response, new_session_id = self._provider.call(
            config,
            prompt,
            user_message=question,
            model_override=model_override,
            agent=agent_override,
            session_id=session_id,
            memory_file=None,
            mcp_config=mcp_config,
            github_token=github_token,
            router_meta=router_meta,
            workspace=workspace,
        )

        if new_session_id and thread_ts:
            self._session.store(thread_ts, new_session_id)

        return response

    def _call_llm_batch(self, context: str, messages: list[dict[str, Any]],
                        thread_ts: str, workspace: str | None = None) -> str:
        """Call LLM with a batch of messages."""
        config = self._config
        mcp_config = _build_mcp_config(config)
        github_token = self._github.get_token()
        session_id = self._session.get(thread_ts)

        # Combine message texts for memory lookup
        combined_text = " ".join(m.get("text", "") for m in messages)
        shared_memory = self._memory_agent.recall(combined_text, has_session=session_id is not None)

        prompt = _build_batch_prompt(config, context, messages, github_token, shared_memory)

        if workspace is None:
            workspace = self._get_workspace()

        response, new_session_id = self._provider.call(
            config,
            prompt,
            user_message=combined_text[:500],
            model_override=config.model_id,
            session_id=session_id,
            memory_file=None,
            mcp_config=mcp_config,
            github_token=github_token,
            workspace=workspace,
        )

        if new_session_id and thread_ts:
            self._session.store(thread_ts, new_session_id)

        return response

    def _process_and_reply(self, say, client: WebClient,
                           context: str, question: str, thread_ts: str,
                           channel: str = "", user_name: str = "") -> None:
        """Process single message LLM call in background thread and reply when done."""
        config = self._config
        try:
            # Semantic Router — decide agent + model + memory (with Slack thread context)
            route = self._router.route(question, thread_context=context)
            agent_override = route.get("agent")
            model_override = route.get("model")
            router_model_name = route.get("model_name", "sonnet")
            router_reason = route.get("reason", "")
            memory_requests = route.get("memory", [])

            logger.info("[%s] Router: agent=%s model=%s memory_lookups=%d reason=%s",
                        config.name, agent_override or "direct",
                        router_model_name, len(memory_requests), router_reason)

            # Manual trigger overrides router's model choice
            force_opus = self._switcher.is_manual_trigger(question)
            if force_opus:
                model_override = config.opus_model_id
                router_model_name = "opus (manual override)"
                logger.info("[%s] Project lead override → Opus", config.name)

            reply = self._call_llm(context, question, thread_ts,
                                   model_override=model_override,
                                   agent_override=agent_override,
                                   router_meta={"model_name": router_model_name,
                                                "reason": router_reason,
                                                "agent": agent_override},
                                   channel=channel,
                                   memory_requests=memory_requests)

            if self._switcher.has_marker(reply):
                if not self._switcher.should_escalate(thread_ts):
                    reply = self._switcher.strip_marker(reply)
                    reply += f"\n\n⚠️ _Atingi o limite de escalações — preciso da sua decisão, {PROJECT_LEAD}._"
                else:
                    self._switcher.record_escalation(thread_ts)
                    say("_(pensando mais profundamente...)_", thread_ts=thread_ts)

                    reply = self._call_llm(context, question, thread_ts,
                                           model_override=config.opus_model_id,
                                           channel=channel)
                    reply = self._switcher.strip_marker(reply)

            logger.info("[%s] Replied (%d chars): %s", config.name, len(reply), reply[:80])

            # Memory Agent — observe the exchange for selective extraction (fire-and-forget)
            self._memory_agent.observe(config.name, question, reply)

            self._post_reply(say, reply, thread_ts)

        except Exception as e:
            logger.error("[%s] Background processing error: %s", config.name, e)
            say("(error processing — I saved my progress and will pick up next time)", thread_ts=thread_ts)

    def _on_batch_flush(self, key: str, messages: list[dict[str, Any]]) -> None:
        """Callback from accumulator — process a batch of messages."""
        # Parse key: "{agent_key}:{thread_ts}"
        parts = key.split(":", 1)
        if len(parts) != 2:
            return
        _, thread_ts = parts

        # Retrieve stashed context
        with self._thread_context_lock:
            ctx = self._thread_context.pop(key, None)
        if not ctx:
            logger.warning("[%s] No context for batch key %s", self._config.name, key)
            return

        say = ctx["say"]
        client = ctx["client"]
        channel = ctx["channel"]

        if len(messages) == 1:
            # Single message — standard flow
            msg = messages[0]
            context = get_thread_context(client, channel, thread_ts)
            if not context:
                context = get_channel_context(client, channel)

            thread = threading.Thread(
                target=self._process_and_reply,
                args=(say, client, context, msg["text"], thread_ts, channel),
                kwargs={"user_name": msg.get("user_name", "someone")},
                daemon=True,
            )
            thread.start()
        else:
            # Batch — consolidated processing
            thread = threading.Thread(
                target=self._process_batch,
                args=(say, client, messages, thread_ts, channel),
                daemon=True,
            )
            thread.start()

    def _process_batch(self, say, client: WebClient,
                       messages: list[dict[str, Any]],
                       thread_ts: str, channel: str) -> None:
        """Process a batch of messages with a single consolidated LLM call."""
        config = self._config
        try:
            context = get_thread_context(client, channel, thread_ts)
            if not context:
                context = get_channel_context(client, channel)

            logger.info("[%s] Processing batch of %d messages", config.name, len(messages))

            say(f"_(Processing {len(messages)} tasks...)_", thread_ts=thread_ts)

            reply = self._call_llm_batch(context, messages, thread_ts)

            logger.info("[%s] Batch replied (%d chars): %s", config.name, len(reply), reply[:80])

            # Observe combined exchange
            combined = " | ".join(m.get("text", "") for m in messages)
            self._memory_agent.observe(config.name, combined, reply)

            self._post_reply(say, reply, thread_ts)

        except Exception as e:
            logger.error("[%s] Batch processing error: %s", config.name, e)
            say("(error processing batch — I saved my progress and will pick up next time)", thread_ts=thread_ts)

    def _post_reply(self, say, reply: str, thread_ts: str) -> None:
        """Post reply to Slack, handling suppression and mention formatting."""
        # Strip markdown bold/italic wrapping mentions
        reply = re.sub(r'\*{1,2}(<@[A-Z0-9]+>)\*{1,2}', r'\1', reply)

        # Suppress empty/no-action responses
        skip_phrases = {"no response requested", "no action needed", "nothing to do", "..."}
        if reply.strip().lower().rstrip(".!") in skip_phrases or len(reply.strip()) < 5:
            logger.info("[%s] Suppressed non-substantive response", self._config.name)
        else:
            say(reply, thread_ts=thread_ts)

    def _handle_message(self, event: dict, say, client: WebClient) -> None:
        text = event.get("text", "").strip()
        if not text:
            return

        # bot_message events use "bot_id" instead of "user"
        user_id = event.get("user", "")
        bot_id = event.get("bot_id", "")
        is_bot_msg = event.get("subtype") == "bot_message"

        # Skip own messages (check both user_id and bot_id)
        if user_id and user_id == self._config.bot_user_id:
            return
        if is_bot_msg and bot_id == self._config.bot_id:
            return

        if not is_mentioned(text, self._config.bot_user_id):
            return

        clean_text = strip_mention(text)
        if not clean_text:
            return

        thread_ts = event.get("thread_ts") or event.get("ts")

        # Track agent-to-agent interactions and enforce limit
        is_from_agent = is_bot_msg or (user_id in _get_bot_user_ids())
        if is_from_agent:
            count = _agent_interactions.get(thread_ts, 0)
            if count >= MAX_AGENT_INTERACTIONS:
                logger.warning(
                    "[%s] Ignoring agent message — limit of %d agent interactions "
                    "reached in thread %s",
                    self._config.name, MAX_AGENT_INTERACTIONS, thread_ts,
                )
                return
            _agent_interactions[thread_ts] = count + 1
            logger.info(
                "[%s] Agent-to-agent interaction %d/%d in thread %s",
                self._config.name, count + 1, MAX_AGENT_INTERACTIONS, thread_ts,
            )

        if user_id:
            user_name = resolve_user(client, user_id)
        elif is_bot_msg:
            user_name = event.get("username", "agent")
        else:
            user_name = "someone"
        channel = event["channel"]

        logger.info("[%s] Message from %s: %s", self._config.name, user_name, clean_text[:80])

        # Stash context for batch callback and add to accumulator
        acc_key = f"{self._config.agent_key}:{thread_ts}"
        with self._thread_context_lock:
            self._thread_context[acc_key] = {
                "say": say,
                "client": client,
                "channel": channel,
            }

        self._accumulator.add(
            self._config.agent_key,
            thread_ts,
            {"text": clean_text, "user_name": user_name, "channel": channel},
        )

    def _handle_dm(self, event: dict, say, client: WebClient) -> None:
        if event.get("subtype") or event.get("bot_id"):
            return

        text = event.get("text", "").strip()
        if not text:
            return

        user_name = resolve_user(client, event["user"])
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event.get("ts")
        context = get_channel_context(client, channel)

        logger.info("[%s] DM from %s: %s", self._config.name, user_name, text[:80])

        # DMs bypass accumulator — process immediately
        thread = threading.Thread(
            target=self._process_and_reply,
            args=(say, client, context, text, thread_ts, channel),
            kwargs={"user_name": user_name},
            daemon=True,
        )
        thread.start()

    def create_socket_handler(self) -> SocketModeHandler:
        app = App(token=self._config.bot_token)

        @app.event("app_mention")
        def handle_mention(event, say, client):
            self._handle_message(event, say, client)

        @app.event({"type": "message", "subtype": "bot_message"})
        def handle_bot_message(event, say, client):
            """Handle messages from other bots (agent-to-agent collaboration)."""
            self._handle_message(event, say, client)

        @app.event("message")
        def handle_message(event, say, client):
            if event.get("channel_type") == "im":
                self._handle_dm(event, say, client)
                return
            self._handle_message(event, say, client)

        logger.info("[%s] Handler created — listening for @mentions and DMs", self._config.name)
        return SocketModeHandler(app, self._config.app_token)
