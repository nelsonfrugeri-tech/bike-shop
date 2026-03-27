from __future__ import annotations

import json
import logging
import os
import tempfile
import threading

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from bike_shop.agents import PROJECT_LEAD
from bike_shop.config import AgentConfig
from bike_shop.github_auth import GitHubAuth
from bike_shop.memory import MemoryStore
from bike_shop.model_switch import ModelSwitcher
from bike_shop.providers import LLMProvider
from bike_shop.session import SessionStore
from bike_shop.slack.context import (
    build_mention_instruction,
    get_channel_context,
    get_thread_context,
    is_mentioned,
    resolve_user,
    strip_mention,
)

logger = logging.getLogger(__name__)

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

    path = os.path.join(
        tempfile.gettempdir(),
        f"bike-shop-mcp-{config.name.lower().replace(' ', '-')}.json",
    )
    with open(path, "w") as f:
        json.dump(mcp, f)
    return path


def _build_prompt(config: AgentConfig, context: str, question: str,
                  memory: MemoryStore, github_token: str | None) -> str:
    """Assemble the full prompt from system prompt + instructions + context."""
    parts = [config.system_prompt]

    if github_token:
        parts.append(
            "\n\nIMPORTANT: For ALL GitHub operations (issues, PRs, comments, etc.) "
            "use the gh CLI via Bash. The GH_TOKEN environment variable is already set "
            "with your unique bot identity. Do NOT use 'gh auth login'. "
            "Example: gh api repos/OWNER/REPO/issues -f title='...' -f body='...'"
        )

    parts.append(memory.build_instruction())
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
        self._memory = MemoryStore(config.agent_key)
        self._github = GitHubAuth(config)
        self._switcher = ModelSwitcher()

    def _call_llm(self, context: str, question: str, thread_ts: str,
                  model_override: str | None = None) -> str:
        """Call the LLM provider and handle session tracking."""
        config = self._config
        mem_file = self._memory.ensure()
        mcp_config = _build_mcp_config(config)
        github_token = self._github.get_token()
        session_id = self._session.get(thread_ts)

        prompt = _build_prompt(config, context, question, self._memory, github_token)

        response, new_session_id = self._provider.call(
            config,
            prompt,
            model_override=model_override,
            session_id=session_id,
            memory_file=mem_file if self._memory.exists() else None,
            mcp_config=mcp_config,
            github_token=github_token,
        )

        if new_session_id and thread_ts:
            self._session.store(thread_ts, new_session_id)

        return response

    def _process_and_reply(self, say, client: WebClient,
                           context: str, question: str, thread_ts: str) -> None:
        """Process LLM call in background thread and reply when done."""
        config = self._config
        try:
            force_opus = self._switcher.is_manual_trigger(question)
            model_override = config.opus_model_id if force_opus else None

            if force_opus:
                logger.info("[%s] Project lead triggered deep thinking — using Opus", config.name)

            reply = self._call_llm(context, question, thread_ts, model_override=model_override)

            if self._switcher.has_marker(reply):
                if not self._switcher.should_escalate(thread_ts):
                    reply = self._switcher.strip_marker(reply)
                    reply += f"\n\n⚠️ _Atingi o limite de escalações — preciso da sua decisão, {PROJECT_LEAD}._"
                else:
                    self._switcher.record_escalation(thread_ts)
                    say("_(pensando mais profundamente...)_", thread_ts=thread_ts)

                    reply = self._call_llm(context, question, thread_ts,
                                           model_override=config.opus_model_id)
                    reply = self._switcher.strip_marker(reply)

            logger.info("[%s] Replied (%d chars): %s", config.name, len(reply), reply[:80])
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

        user_name = resolve_user(client, user_id) if user_id else "someone"
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event.get("ts")

        context = get_thread_context(client, channel, thread_ts)
        if not context:
            context = get_channel_context(client, channel)

        logger.info("[%s] Message from %s: %s", self._config.name, user_name, clean_text[:80])

        thread = threading.Thread(
            target=self._process_and_reply,
            args=(say, client, context, f"{user_name}: {clean_text}", thread_ts),
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
            args=(say, client, context, f"{user_name}: {text}", thread_ts),
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
