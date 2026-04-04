from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from bike_shop.accumulator import MAX_PARALLEL_AGENTS, MessageAccumulator
from bike_shop.agents import PROJECT_LEAD
from bike_shop.config import AgentConfig
from bike_shop.github_auth import GitHubAuth
from bike_shop.memory_agent import MemoryAgent
from bike_shop.model_switch import ModelSwitcher
from bike_shop.observability import Tracer
from bike_shop.project import ProjectConfig, ProjectRegistry, ProjectResolver
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
AGENT_INTERACTION_TTL = float(os.environ.get("AGENT_INTERACTION_TTL", "1800"))

_GC_THRESHOLD = 100


@dataclass
class InteractionState:
    """Tracks agent-to-agent interaction count and last activity time per thread."""

    count: int = 0
    last_activity: float = 0.0  # time.monotonic() timestamp; 0.0 means never seen


# Track agent-to-agent messages per thread: thread_ts -> InteractionState
_agent_interactions: dict[str, InteractionState] = {}
_interactions_lock = threading.Lock()

# Bot user IDs of all agents — resolved lazily
_bot_user_ids: set[str] | None = None


def _get_bot_user_ids() -> set[str]:
    global _bot_user_ids
    if _bot_user_ids is None:
        mentions = get_team_mentions()
        _bot_user_ids = set(mentions.values())
        logger.info("Bot user IDs resolved: %s", _bot_user_ids)
    return _bot_user_ids



def _check_and_update_interaction(thread_ts: str) -> tuple[bool, int]:
    """Check whether the next agent interaction is allowed and update the counter.

    Applies TTL expiry before checking the limit. Triggers lazy GC when the
    dict grows beyond _GC_THRESHOLD entries.

    Args:
        thread_ts: Slack thread timestamp used as the interaction key.

    Returns:
        A tuple of (allowed, current_count). allowed is True if the interaction
        is permitted, False if the limit is reached. current_count reflects the
        updated count after a permitted interaction, or the limit when blocked.
    """
    with _interactions_lock:
        if len(_agent_interactions) > _GC_THRESHOLD:
            _gc_interactions()

        now = time.monotonic()
        state = _agent_interactions.get(thread_ts)

        if state is not None and (now - state.last_activity) > AGENT_INTERACTION_TTL:
            # TTL expired — reset the counter for this thread
            state = None

        if state is None:
            _agent_interactions[thread_ts] = InteractionState(count=1, last_activity=now)
            return True, 1

        if state.count >= MAX_AGENT_INTERACTIONS:
            return False, state.count

        state.count += 1
        state.last_activity = now
        return True, state.count


def _reset_interaction(thread_ts: str) -> None:
    """Remove the interaction counter for a thread (called on human messages).

    Args:
        thread_ts: Slack thread timestamp to reset.
    """
    with _interactions_lock:
        _agent_interactions.pop(thread_ts, None)


def _gc_interactions() -> None:
    """Remove stale interaction entries older than 2 * AGENT_INTERACTION_TTL.

    This is a lazy garbage collector called only when the dict exceeds
    _GC_THRESHOLD entries to avoid unbounded memory growth.

    Note: Must be called with _interactions_lock held.
    """
    if len(_agent_interactions) <= _GC_THRESHOLD:
        return
    cutoff = time.monotonic() - (2 * AGENT_INTERACTION_TTL)
    stale_keys = [k for k, s in _agent_interactions.items() if s.last_activity < cutoff]
    for k in stale_keys:
        del _agent_interactions[k]


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_BASE_MCP_CONFIG = os.path.join(_PROJECT_ROOT, "mcp.json")


def _resolve_env_vars(obj: Any) -> Any:
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


def _build_prompt(config: AgentConfig, context: str, question: str,
                  github_token: str | None,
                  shared_memory: str = "") -> str:
    """Assemble the full prompt from system prompt + instructions + context."""
    parts = [config.system_prompt]

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
        "   -> Use the Agent tool to spawn one sub-agent per task in isolated worktrees\n"
        "   -> Each sub-agent runs in parallel with isolation: \"worktree\"\n"
        "   -> Collect results and respond with a consolidated summary\n\n"
        "2. **Dependent tasks** (task B needs output of task A):\n"
        "   -> Execute sequentially, in dependency order\n\n"
        "3. **Related tasks** (all part of the same feature):\n"
        "   -> Execute together in a single worktree\n\n"
        "Messages:\n"
    )
    for i, msg in enumerate(messages, 1):
        user = msg.get("user_name", "someone")
        text = msg.get("text", "")
        parts.append(f"  {i}. [{user}]: {text}\n")

    parts.append(
        f"\nIMPORTANT: Spawn at most {MAX_PARALLEL_AGENTS} sub-agents concurrently. "
        "If there are more tasks than the limit, process them in sequential rounds."
    )
    parts.append("\n--- END BATCH ---")

    return "".join(parts)


class SlackAgentHandler:
    """Wires a single agent to Slack via Socket Mode."""

    def __init__(
        self,
        config: AgentConfig,
        provider: LLMProvider,
        project_registry: ProjectRegistry | None = None,
    ) -> None:
        self._config = config
        self._provider = provider
        self._session = SessionStore(config.agent_key)
        self._github = GitHubAuth(config)
        self._switcher = ModelSwitcher()
        self._router = SemanticRouter()
        self._project_registry = project_registry

        # Default memory agent and tracer (used when no project is resolved)
        self._memory_agent = MemoryAgent(agent_key=config.agent_key)
        self._tracer = Tracer(config.name)
        self._accumulator = MessageAccumulator(flush_callback=self._on_batch_flush)

        # Per-project memory agents and tracers (lazy-created)
        self._memory_agents: dict[str, MemoryAgent] = {}
        self._tracers: dict[str, Tracer] = {}

        # Project resolver (lazy-created when registry is available)
        self._resolver: ProjectResolver | None = None
        if project_registry:
            self._resolver = ProjectResolver(project_registry, self._session)

        # Stash say/client per thread for batch callback
        self._thread_context: dict[str, dict[str, Any]] = {}
        self._thread_context_lock = threading.Lock()

    def _resolve_project(self, channel: str, thread_ts: str | None = None) -> ProjectConfig | None:
        """Resolve channel/thread to a ProjectConfig, or None if no registry."""
        if not self._resolver:
            return None
        try:
            return self._resolver.resolve(channel, thread_ts)
        except ValueError:
            logger.warning("[%s] Failed to resolve project for channel=%s", self._config.name, channel)
            return None

    def _get_memory_agent(self, project: ProjectConfig | None = None) -> MemoryAgent:
        """Get a MemoryAgent for the given project, or the default one."""
        if not project:
            return self._memory_agent
        pid = project.project_id
        if pid not in self._memory_agents:
            self._memory_agents[pid] = MemoryAgent(
                agent_key=self._config.agent_key,
                project_id=pid,
                mem0_collection=project.mem0_collection,
            )
        return self._memory_agents[pid]

    def _get_tracer(self, project: ProjectConfig | None = None) -> Tracer:
        """Get a Tracer for the given project, or the default one."""
        if not project:
            return self._tracer
        pid = project.project_id
        if pid not in self._tracers:
            self._tracers[pid] = Tracer(
                self._config.name,
                langfuse_public_key=project.langfuse_public_key,
                langfuse_secret_key=project.langfuse_secret_key,
            )
        return self._tracers[pid]

    def _get_workspace(
        self,
        task_id: str | None = None,
        project: ProjectConfig | None = None,
    ) -> str:
        """Get or create an isolated worktree for this agent.

        Args:
            task_id: Optional task suffix for the worktree name.
            project: ProjectConfig with repo_path/worktree_dir overrides.

        Raises:
            RuntimeError: If worktree creation fails. Worktrees are mandatory;
                          there is no fallback to a shared directory.
        """
        return ensure_worktree(
            self._config.agent_key,
            task_id=task_id,
            repo_path=project.repo_path if project else None,
            worktree_dir=project.worktree_dir if project else None,
        )

    @staticmethod
    def _capture_worktree_diff(
        workspace: str | None,
        trace_id: str | None,
        tracer: Any,
    ) -> None:
        """Capture git diff --stat and add as a Langfuse span. Never raises."""
        if not workspace or not trace_id:
            return
        try:
            diff_result = subprocess.run(
                ["git", "diff", "--stat", "HEAD"],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=5,
            )
            diff_stat = diff_result.stdout.strip()
            if diff_stat:
                diff_span = tracer.start_span(
                    "worktree.diff",
                    trace_id=trace_id,
                    input={"workspace": workspace},
                    metadata={"type": "worktree_diff", "has_changes": True},
                )
                tracer.end_span(
                    diff_span,
                    trace_id=trace_id,
                    output={"diff_stat": diff_stat},
                )
        except Exception:
            pass

    def _call_llm(self, context: str, question: str, thread_ts: str,
                  model_override: str | None = None, agent_override: str | None = None,
                  router_meta: dict | None = None,
                  channel: str = "",
                  memory_requests: list[Any] | None = None,
                  workspace: str | None = None,
                  trace_id: str | None = None,
                  parent_span_id: str | None = None,
                  project: ProjectConfig | None = None) -> str:
        """Call the LLM provider and handle session tracking."""
        config = self._config
        tracer = self._get_tracer(project)
        memory_agent = self._get_memory_agent(project)
        mcp_config = _build_mcp_config(config)
        github_token = self._github.get_token()
        session_id = self._session.get(
            thread_ts,
            project_id=project.project_id if project else None,
        )

        # Memory recall: full recall on new threads + router-driven filtered recall
        memory_span_id = tracer.start_span(
            "memory.recall", trace_id=trace_id, parent_id=parent_span_id,
            input={"question": question[:300]},
        ) if trace_id else None

        shared_memory = memory_agent.recall(
            question, has_session=session_id is not None,
            trace_id=trace_id, parent_span_id=memory_span_id,
        )
        if memory_requests:
            filtered = memory_agent.recall_filtered(
                memory_requests,
                trace_id=trace_id, parent_span_id=memory_span_id,
            )
            shared_memory = (shared_memory + filtered) if shared_memory else filtered

        if memory_span_id and trace_id:
            tracer.end_span(memory_span_id, trace_id=trace_id,
                            output=f"{len(shared_memory)} chars" if shared_memory else "empty")

        # Build prompt
        prompt_span_id = tracer.start_span(
            "prompt.build", trace_id=trace_id, parent_id=parent_span_id,
            input={"question_length": len(question)},
        ) if trace_id else None

        prompt = _build_prompt(config, context, question, github_token, shared_memory)

        if prompt_span_id and trace_id:
            tracer.end_span(prompt_span_id, trace_id=trace_id,
                            output={"prompt_length": len(prompt),
                                    "has_memory": bool(shared_memory)})

        # Get worktree workspace if not provided
        if workspace is None:
            workspace = self._get_workspace(project=project)

        # LLM call span
        llm_span_id = tracer.start_span(
            "llm.call", trace_id=trace_id, parent_id=parent_span_id,
            input={"question": question[:500]},
        ) if trace_id else None

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
            trace_id=trace_id,
            parent_span_id=llm_span_id,
            tracer=tracer,
        )

        if llm_span_id and trace_id:
            tracer.end_span(llm_span_id, trace_id=trace_id,
                            output=response[:200] if response else "")

        if new_session_id and thread_ts:
            self._session.store(
                thread_ts, new_session_id,
                project_id=project.project_id if project else None,
            )

        return response

    def _call_llm_batch(self, context: str, messages: list[dict[str, Any]],
                        thread_ts: str, workspace: str | None = None,
                        trace_id: str | None = None,
                        project: ProjectConfig | None = None) -> str:
        """Call LLM with a batch of messages."""
        config = self._config
        tracer = self._get_tracer(project)
        memory_agent = self._get_memory_agent(project)
        mcp_config = _build_mcp_config(config)
        github_token = self._github.get_token()
        session_id = self._session.get(
            thread_ts,
            project_id=project.project_id if project else None,
        )

        # Combine message texts for memory lookup
        combined_text = " ".join(m.get("text", "") for m in messages)
        shared_memory = memory_agent.recall(combined_text, has_session=session_id is not None)

        prompt = _build_batch_prompt(config, context, messages, github_token, shared_memory)

        if workspace is None:
            workspace = self._get_workspace(project=project)

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
            trace_id=trace_id,
            tracer=tracer,
        )

        if new_session_id and thread_ts:
            self._session.store(
                thread_ts, new_session_id,
                project_id=project.project_id if project else None,
            )

        return response

    def _process_and_reply(self, say: Any, client: WebClient,
                           context: str, question: str, thread_ts: str,
                           channel: str = "", user_name: str = "") -> None:
        """Process single message LLM call in background thread and reply when done."""
        config = self._config
        project = self._resolve_project(channel, thread_ts)
        tracer = self._get_tracer(project)
        memory_agent = self._get_memory_agent(project)
        try:
            # Start top-level trace for this message
            trace_id = tracer.start_trace(
                f"{config.name}/slack-message",
                input=question,
                session_id=thread_ts,
                metadata={"channel": channel, "user": user_name},
            )

            # message.receive span
            receive_span = tracer.start_span(
                "message.receive", trace_id=trace_id,
                input=question,
                metadata={"user": user_name, "channel": channel},
            )
            tracer.end_span(receive_span, trace_id=trace_id,
                            output={"cleaned_text": question[:200]})

            # Semantic Router — decide agent + model + memory (with Slack thread context)
            router_span = tracer.start_span("router.classify", trace_id=trace_id,
                                               input={"question": question[:300]})
            route = self._router.route(
                question, thread_context=context,
                trace_id=trace_id, parent_span_id=router_span,
            )
            agent_override = route.get("agent")
            model_override = route.get("model")
            router_model_name = route.get("model_name", "sonnet")
            router_reason = route.get("reason", "")
            memory_requests = route.get("memory", [])
            tracer.end_span(router_span, trace_id=trace_id,
                            output=json.dumps({"agent": agent_override, "model": router_model_name}),
                            metadata={"reason": router_reason})

            logger.info("[%s] Router: agent=%s model=%s memory_lookups=%d reason=%s",
                        config.name, agent_override or "direct",
                        router_model_name, len(memory_requests), router_reason)

            # Manual trigger overrides router's model choice
            force_opus = self._switcher.is_manual_trigger(question)
            if force_opus:
                model_override = config.opus_model_id
                router_model_name = "opus (manual override)"
                logger.info("[%s] Project lead override -> Opus", config.name)

            # Resolve workspace once — reuse for LLM call and worktree diff
            workspace = self._get_workspace(project=project)

            reply = self._call_llm(context, question, thread_ts,
                                   model_override=model_override,
                                   agent_override=agent_override,
                                   router_meta={"model_name": router_model_name,
                                                "reason": router_reason,
                                                "agent": agent_override},
                                   channel=channel,
                                   memory_requests=memory_requests,
                                   workspace=workspace,
                                   trace_id=trace_id,
                                   project=project)

            if self._switcher.has_marker(reply):
                if not self._switcher.should_escalate(thread_ts):
                    reply = self._switcher.strip_marker(reply)
                    reply += f"\n\n_Atingi o limite de escalacoes -- preciso da sua decisao, {PROJECT_LEAD}._"
                else:
                    self._switcher.record_escalation(thread_ts)
                    say("_(pensando mais profundamente...)_", thread_ts=thread_ts)

                    reply = self._call_llm(context, question, thread_ts,
                                           model_override=config.opus_model_id,
                                           channel=channel,
                                           trace_id=trace_id,
                                           project=project)
                    reply = self._switcher.strip_marker(reply)

            logger.info("[%s] Replied (%d chars): %s", config.name, len(reply), reply[:80])

            self._capture_worktree_diff(workspace, trace_id, tracer)

            # Fix #6: Memory observe — pass observe_span_id to background
            # thread which will close it when the work finishes.
            observe_span = tracer.start_span("memory.observe", trace_id=trace_id,
                                                input={"question": question[:200],
                                                       "reply": reply[:200]})
            memory_agent.observe(
                config.name, question, reply,
                trace_id=trace_id,
                parent_span_id=observe_span,
                observe_span_id=observe_span,
            )
            # observe_span is closed inside _observe_sync, not here

            self._post_reply(say, reply, thread_ts, tracer=tracer, trace_id=trace_id)

            # Update trace and flush
            tracer.update_trace(trace_id, output=reply[:500])
            tracer.flush()

        except Exception as e:
            logger.error("[%s] Background processing error: %s", config.name, e)
            say("(error processing -- I saved my progress and will pick up next time)", thread_ts=thread_ts)

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

        if ctx:
            say = ctx["say"]
            client = ctx["client"]
            channel = ctx["channel"]
        else:
            # Recover from missing context — reconstruct from messages and handler state
            channel = messages[0].get("channel", "") if messages else ""
            if not channel:
                logger.error("[%s] No context and no channel for batch key %s — dropping", self._config.name, key)
                return
            logger.warning("[%s] Recovering context for batch key %s from channel=%s", self._config.name, key, channel)
            client = WebClient(token=self._config.bot_token)

            def say(text: str, thread_ts: str = thread_ts, **kwargs: Any) -> None:
                try:
                    client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)
                except Exception as e:
                    logger.error("[%s] Failed to post recovered message: %s", self._config.name, e)

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

    def _process_batch(self, say: Any, client: WebClient,
                       messages: list[dict[str, Any]],
                       thread_ts: str, channel: str) -> None:
        """Process a batch of messages with a single consolidated LLM call."""
        config = self._config
        project = self._resolve_project(channel, thread_ts)
        tracer = self._get_tracer(project)
        memory_agent = self._get_memory_agent(project)
        try:
            context = get_thread_context(client, channel, thread_ts)
            if not context:
                context = get_channel_context(client, channel)

            logger.info("[%s] Processing batch of %d messages", config.name, len(messages))

            trace_id = tracer.start_trace(
                f"{config.name}/slack-batch",
                input=f"{len(messages)} messages",
                session_id=thread_ts,
            )

            say(f"_(Processing {len(messages)} tasks...)_", thread_ts=thread_ts)

            workspace = self._get_workspace(project=project)

            reply = self._call_llm_batch(
                context, messages, thread_ts,
                workspace=workspace, trace_id=trace_id, project=project,
            )

            logger.info("[%s] Batch replied (%d chars): %s", config.name, len(reply), reply[:80])

            self._capture_worktree_diff(workspace, trace_id, tracer)

            # Observe combined exchange
            combined = " | ".join(m.get("text", "") for m in messages)
            memory_agent.observe(config.name, combined, reply)

            self._post_reply(say, reply, thread_ts, tracer=tracer, trace_id=trace_id)

            tracer.update_trace(trace_id, output=reply[:500])
            tracer.flush()

        except Exception as e:
            logger.error("[%s] Batch processing error: %s", config.name, e)
            say("(error processing batch -- I saved my progress and will pick up next time)", thread_ts=thread_ts)

    def _post_reply(self, say: Any, reply: str, thread_ts: str,
                    tracer: Tracer | None = None,
                    trace_id: str | None = None) -> None:
        """Post reply to Slack, handling suppression and mention formatting."""
        # Strip markdown bold/italic wrapping mentions — Slack won't generate
        # app_mention events if <@USER_ID> is inside **bold** or *italic*
        reply = re.sub(r'\*{1,2}(<@[A-Z0-9]+>)\*{1,2}', r'\1', reply)

        # Suppress empty/no-action responses
        skip_phrases = {"no response requested", "no action needed", "nothing to do", "..."}
        if reply.strip().lower().rstrip(".!") in skip_phrases or len(reply.strip()) < 5:
            logger.info("[%s] Suppressed non-substantive response", self._config.name)
        else:
            reply_span = None
            if tracer and trace_id:
                reply_span = tracer.start_span("slack.reply", trace_id=trace_id,
                                               input={"reply_length": len(reply)})
            say(reply, thread_ts=thread_ts)
            if tracer and trace_id and reply_span:
                tracer.end_span(reply_span, trace_id=trace_id,
                                output={"status": "sent"})

    def _handle_message(self, event: dict[str, Any], say: Any, client: WebClient) -> None:
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
            allowed, current_count = _check_and_update_interaction(thread_ts)
            if not allowed:
                logger.warning(
                    "[%s] Ignoring agent message -- limit of %d agent interactions "
                    "reached in thread %s",
                    self._config.name, MAX_AGENT_INTERACTIONS, thread_ts,
                )
                return
            logger.info(
                "[%s] Agent-to-agent interaction %d/%d in thread %s",
                self._config.name, current_count, MAX_AGENT_INTERACTIONS, thread_ts,
            )
        else:
            # Human message — reset the interaction counter for this thread
            _reset_interaction(thread_ts)

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

    def _handle_dm(self, event: dict[str, Any], say: Any, client: WebClient) -> None:
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
        def handle_mention(event: dict[str, Any], say: Any, client: WebClient) -> None:
            self._handle_message(event, say, client)

        @app.event({"type": "message", "subtype": "bot_message"})
        def handle_bot_message(event: dict[str, Any], say: Any, client: WebClient) -> None:
            """Handle messages from other bots (agent-to-agent collaboration)."""
            self._handle_message(event, say, client)

        @app.event("message")
        def handle_message(event: dict[str, Any], say: Any, client: WebClient) -> None:
            if event.get("channel_type") == "im":
                self._handle_dm(event, say, client)
                return
            self._handle_message(event, say, client)

        logger.info("[%s] Handler created -- listening for @mentions and DMs", self._config.name)
        return SocketModeHandler(app, self._config.app_token)
