from __future__ import annotations

import json
import logging
import os
import threading

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

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

logger = logging.getLogger(__name__)

MAX_AGENT_INTERACTIONS = 5

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

    def _call_llm(self, context: str, question: str, thread_ts: str,
                  model_override: str | None = None, agent_override: str | None = None,
                  router_meta: dict | None = None,
                  channel: str = "") -> str:
        """Call the LLM provider and handle session tracking."""
        config = self._config
        mcp_config = _build_mcp_config(config)
        github_token = self._github.get_token()
        session_id = self._session.get(thread_ts)

        # Recall relevant memories (Redis short-term + Mem0 long-term)
        shared_memory = self._memory_agent.recall(question, channel=channel, thread_ts=thread_ts)
        prompt = _build_prompt(config, context, question, github_token, shared_memory)

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
        )

        if new_session_id and thread_ts:
            self._session.store(thread_ts, new_session_id)

        return response

    def _process_and_reply(self, say, client: WebClient,
                           context: str, question: str, thread_ts: str,
                           channel: str = "", user_name: str = "") -> None:
        """Process LLM call in background thread and reply when done."""
        config = self._config
        try:
            # Push user message to Redis short-term BEFORE LLM call
            self._memory_agent.push_user_message(user_name, question, channel, thread_ts)

            # Semantic Router — decide agent + model
            route = self._router.route(question)
            agent_override = route.get("agent")
            model_override = route.get("model")
            router_model_name = route.get("model_name", "sonnet")
            router_reason = route.get("reason", "")

            logger.info("[%s] Router: agent=%s model=%s reason=%s",
                        config.name, agent_override or "direct",
                        router_model_name, router_reason)

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
                                   channel=channel)

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

            # Memory Agent — observe the exchange for selective extraction
            route_decision = {
                "agent": agent_override,
                "model": model_override or config.model_id,
                "model_name": router_model_name,
                "reason": router_reason,
            }
            self._memory_agent.observe(
                config.name, question, reply,
                channel=channel, thread_ts=thread_ts,
                route_decision=route_decision,
                user_name=user_name,
            )

            # Suppress empty/no-action responses — don't waste Slack messages
            skip_phrases = {"no response requested", "no action needed", "nothing to do", "..."}
            if reply.strip().lower().rstrip(".!") in skip_phrases or len(reply.strip()) < 5:
                logger.info("[%s] Suppressed non-substantive response", config.name)
            else:
                say(reply, thread_ts=thread_ts)

        except Exception as e:
            logger.error("[%s] Background processing error: %s", config.name, e)
            say("(error processing — I saved my progress and will pick up next time)", thread_ts=thread_ts)

    def _handle_message(self, event: dict, say, client: WebClient) -> None:
        text = event.get("text", "").strip()
        if not text:
            return

        user_id = event.get("user", "")
        if user_id == self._config.bot_user_id:
            return

        if not is_mentioned(text, self._config.bot_user_id):
            return

        clean_text = strip_mention(text)
        if not clean_text:
            return

        thread_ts = event.get("thread_ts") or event.get("ts")

        # Track agent-to-agent interactions and enforce limit
        is_from_agent = user_id in _get_bot_user_ids()
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

        user_name = resolve_user(client, user_id) if user_id else "someone"
        channel = event["channel"]

        context = get_thread_context(client, channel, thread_ts)
        if not context:
            context = get_channel_context(client, channel)

        logger.info("[%s] Message from %s: %s", self._config.name, user_name, clean_text[:80])

        thread = threading.Thread(
            target=self._process_and_reply,
            args=(say, client, context, clean_text, thread_ts, channel),
            kwargs={"user_name": user_name},
            daemon=True,
        )
        thread.start()

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

        @app.event("message")
        def handle_message(event, say, client):
            if event.get("channel_type") == "im":
                self._handle_dm(event, say, client)
                return
            self._handle_message(event, say, client)

        logger.info("[%s] Handler created — listening for @mentions and DMs", self._config.name)
        return SocketModeHandler(app, self._config.app_token)
