from __future__ import annotations

import json as _json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BASE_MCP_CONFIG = os.path.join(_PROJECT_ROOT, "mcp.json")

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from bike_shop.agents import PROJECT_LEAD
from bike_shop.config import AgentConfig, resolve_team_mentions

logger = logging.getLogger(__name__)

SESSIONS_DIR = os.path.join(tempfile.gettempdir(), "bike-shop")

# Resolved at first use: {"Elliot Alderson": "U0AP10P0GNM", ...}
_team_mentions: dict[str, str] | None = None


def _get_team_mentions() -> dict[str, str]:
    global _team_mentions
    if _team_mentions is None:
        _team_mentions = resolve_team_mentions()
        logger.info("Team mentions resolved: %s", _team_mentions)
    return _team_mentions


def _mention_instruction(agent_name: str) -> str:
    """Build instruction telling the agent how to @mention teammates."""
    mentions = _get_team_mentions()
    if not mentions:
        return ""

    lines = [
        "\n\n--- TEAM MENTIONS ---",
        "When referring to a teammate, ALWAYS use their Slack mention so they get notified.",
        "Use the exact format <@USER_ID> — never just their name.",
        "Team members:",
    ]
    for name, uid in mentions.items():
        if name == agent_name:
            lines.append(f"- {name} (you): <@{uid}>")
        else:
            lines.append(f"- {name}: <@{uid}>")
    lines.append("Example: instead of writing 'Tyrell should handle this', write '<@UXXXXX> should handle this'.")
    return "\n".join(lines)
MEMORY_BASE = os.path.expanduser("~/.claude/workspace/bike-shop/memory")
SESSION_TTL = 86400  # 24h

DEEP_THINK_MARKER = "[DEEP_THINK]"
DEEP_THINK_TRIGGERS = {"pensem profundamente", "pensem com calma", "analisem com calma",
                        "analisem profundamente", "think deeply", "analyze carefully"}
MAX_OPUS_ESCALATIONS = 2

# Cache: agent_name -> (token, expires_at, mcp_config_path)
_github_token_cache: dict[str, tuple[str, float, str]] = {}

# Track opus escalations per thread: thread_ts -> count
_opus_escalations: dict[str, int] = {}


# ---------------------------------------------------------------------------
# Session tracking (thread_ts -> session_id)
# ---------------------------------------------------------------------------

def _sessions_path(agent_key: str) -> str:
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    return os.path.join(SESSIONS_DIR, f"sessions-{agent_key}.json")


def _load_sessions(agent_key: str) -> dict:
    path = _sessions_path(agent_key)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return _json.load(f)
    except (ValueError, OSError):
        return {}


def _save_sessions(agent_key: str, sessions: dict) -> None:
    path = _sessions_path(agent_key)
    with open(path, "w") as f:
        _json.dump(sessions, f)


def _get_session_id(agent_key: str, thread_ts: str) -> str | None:
    sessions = _load_sessions(agent_key)
    entry = sessions.get(thread_ts)
    if not entry:
        return None
    if time.time() - entry.get("ts", 0) > SESSION_TTL:
        sessions.pop(thread_ts, None)
        _save_sessions(agent_key, sessions)
        return None
    return entry.get("session_id")


def _store_session_id(agent_key: str, thread_ts: str, session_id: str) -> None:
    sessions = _load_sessions(agent_key)
    sessions[thread_ts] = {"session_id": session_id, "ts": time.time()}
    _save_sessions(agent_key, sessions)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

def _memory_file(agent_key: str) -> str:
    path = os.path.join(MEMORY_BASE, agent_key, "MEMORY.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _ensure_memory_file(agent_key: str) -> str:
    path = _memory_file(agent_key)
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(f"# {agent_key} - Memory\n\n")
    return path


def _memory_instruction(agent_key: str) -> str:
    mem_path = _memory_file(agent_key)
    return (
        f"\n\nYou have persistent memory at {mem_path}. "
        "When you learn something important (decisions, patterns, user preferences), "
        "save it by appending to that file using Bash: echo '- ...' >> " + mem_path + ". "
        "Consult your memory file at the start of complex tasks."
        "\n\n--- RESILIENCE RULES ---\n"
        "You are an autonomous agent. External tools (Slack, Notion, Trello, GitHub) may timeout or fail.\n"
        "BEFORE starting any long operation or multi-step task:\n"
        "1. Save your current plan and progress to your MEMORY.md file\n"
        "2. Break work into small checkpoints — save after each one\n"
        "3. If a tool call fails or times out, DO NOT stop. Save what you have, note the failure in memory, and continue with the next step\n"
        "4. When resuming work, ALWAYS read your MEMORY.md first to recover context\n"
        "5. After completing a task, save a summary of what was done and any pending items\n"
        "Format for memory entries: `## [YYYY-MM-DD HH:MM] Topic` followed by bullet points.\n"
        "This ensures zero gap in memory — you can always pick up where you left off."
    )


# ---------------------------------------------------------------------------
# GitHub App auth
# ---------------------------------------------------------------------------

def _get_github_token(config: AgentConfig) -> str | None:
    """Generate a GitHub installation token from the App's private key."""
    if not config.github_app_id or not config.github_pem_path:
        return None

    try:
        import jwt
    except ImportError:
        logger.warning("PyJWT not installed — GitHub App auth disabled")
        return None

    cached = _github_token_cache.get(config.name)
    if cached and cached[1] > time.time() + 300:
        return cached[0]

    try:
        with open(config.github_pem_path) as f:
            private_key = f.read()

        payload = {
            "iat": int(time.time()) - 60,
            "exp": int(time.time()) + 600,
            "iss": config.github_app_id,
        }
        jwt_token = jwt.encode(payload, private_key, algorithm="RS256")

        import urllib.request

        install_id = config.github_installation_id
        if not install_id:
            req = urllib.request.Request(
                "https://api.github.com/app/installations",
                headers={"Authorization": f"Bearer {jwt_token}", "Accept": "application/vnd.github+json"},
            )
            with urllib.request.urlopen(req) as resp:
                installations = _json.loads(resp.read())
                install_id = str(installations[0]["id"])

        req = urllib.request.Request(
            f"https://api.github.com/app/installations/{install_id}/access_tokens",
            method="POST",
            headers={"Authorization": f"Bearer {jwt_token}", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req) as resp:
            data = _json.loads(resp.read())
            token = data["token"]
            expires_at = time.time() + 3500

        _github_token_cache[config.name] = (token, expires_at, "")
        logger.info("[%s] GitHub App token refreshed (install=%s)", config.name, install_id)
        return token
    except Exception as e:
        logger.error("[%s] Failed to get GitHub token: %s", config.name, e)
        return None


def _resolve_env_vars(obj):
    """Recursively resolve ${VAR} placeholders in dicts/lists/strings."""
    if isinstance(obj, str) and obj.startswith("${") and obj.endswith("}"):
        var_name = obj[2:-1]
        return os.environ.get(var_name, "")
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars(v) for v in obj]
    return obj


def _build_mcp_config(config: AgentConfig) -> str:
    """Build per-agent mcp.json with env vars resolved. Returns path."""
    with open(_BASE_MCP_CONFIG) as f:
        mcp = _json.load(f)

    mcp = _resolve_env_vars(mcp)

    path = os.path.join(tempfile.gettempdir(), f"bike-shop-mcp-{config.name.lower().replace(' ', '-')}.json")
    with open(path, "w") as f:
        _json.dump(mcp, f)
    return path


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------

def _get_thread_context(client: WebClient, channel: str, thread_ts: str, limit: int = 20) -> str:
    try:
        result = client.conversations_replies(channel=channel, ts=thread_ts, limit=limit)
        messages = result.get("messages", [])
    except Exception:
        return ""

    lines = []
    for msg in messages:
        user = msg.get("user", "bot")
        text = msg.get("text", "")
        lines.append(f"<{user}>: {text}")
    return "\n".join(lines)


def _get_channel_context(client: WebClient, channel: str, limit: int = 10) -> str:
    try:
        result = client.conversations_history(channel=channel, limit=limit)
        messages = result.get("messages", [])
    except Exception:
        return ""

    lines = []
    for msg in reversed(messages):
        user = msg.get("user", "bot")
        text = msg.get("text", "")
        lines.append(f"<{user}>: {text}")
    return "\n".join(lines)


def _resolve_user(client: WebClient, user_id: str) -> str:
    try:
        info = client.users_info(user=user_id)
        profile = info["user"]["profile"]
        return profile.get("display_name") or profile.get("real_name") or user_id
    except Exception:
        return user_id


def _strip_mention(text: str) -> str:
    return re.sub(r"<@[A-Z0-9]+>", "", text).strip()


def _is_mentioned(text: str, bot_user_id: str) -> bool:
    return f"<@{bot_user_id}>" in text


# ---------------------------------------------------------------------------
# Claude CLI call — no timeout, with session tracking + memory
# ---------------------------------------------------------------------------

def _call_claude(config: AgentConfig, context: str, question: str,
                  thread_ts: str | None = None, model_override: str | None = None) -> str:
    """Call claude CLI with session tracking and persistent memory. No timeout."""
    agent_key = config.agent_key
    mem_file = _ensure_memory_file(agent_key)

    mcp_config = _build_mcp_config(config)
    github_token = _get_github_token(config)
    model_id = model_override or config.model_id

    tool_instructions = ""
    if github_token:
        tool_instructions = (
            "\n\nIMPORTANT: For ALL GitHub operations (issues, PRs, comments, etc.) "
            "use the gh CLI via Bash. The GH_TOKEN environment variable is already set "
            "with your unique bot identity. Do NOT use 'gh auth login'. "
            "Example: gh api repos/OWNER/REPO/issues -f title='...' -f body='...'"
        )

    memory_instr = _memory_instruction(agent_key)
    mention_instr = _mention_instruction(config.name)
    prompt = f"{config.system_prompt}{tool_instructions}{memory_instr}{mention_instr}\n\n--- CONVERSATION CONTEXT ---\n{context}\n\n--- NEW MESSAGE TO RESPOND ---\n{question}"

    # Check for existing session
    session_id = _get_session_id(agent_key, thread_ts) if thread_ts else None

    cmd = [
        "claude", "-p", prompt,
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--verbose",
        "--model", model_id,
        "--mcp-config", mcp_config,
    ]

    if session_id:
        cmd.extend(["--resume", session_id])
        logger.debug("[%s] Resuming session %s for thread %s", config.name, session_id, thread_ts)

    if os.path.exists(mem_file):
        cmd.extend(["--append-system-prompt-file", mem_file])

    env = os.environ.copy()
    if github_token:
        env["GH_TOKEN"] = github_token

    logger.debug("[%s] Calling Claude CLI (no timeout)...", config.name)
    try:
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=None,
            cwd=os.path.expanduser("~"),
            env=env,
        )
        if result.returncode != 0:
            logger.error("Claude CLI failed (rc=%d): %s", result.returncode, result.stderr.strip())

        # Parse session_id and store it
        response = ""
        for line in result.stdout.splitlines():
            try:
                event = _json.loads(line)
                # Capture session_id
                if event.get("type") == "system" and event.get("session_id") and thread_ts:
                    _store_session_id(agent_key, thread_ts, event["session_id"])
                    logger.debug("[%s] Stored session %s for thread %s", config.name, event["session_id"], thread_ts)
                # Extract assistant text
                if event.get("type") == "assistant":
                    content = event.get("message", {}).get("content", [])
                    texts = [c["text"] for c in content if c.get("type") == "text"]
                    if texts:
                        response = "\n".join(texts).strip()
            except (ValueError, KeyError):
                continue

        if not response:
            logger.warning("Claude CLI returned empty response. stderr: %s", result.stderr.strip())
            response = "..."
        return response

    except Exception as e:
        logger.error("Claude CLI error: %s", e)
        return "(error)"


# ---------------------------------------------------------------------------
# Async processing — run Claude in background thread, reply when done
# ---------------------------------------------------------------------------

def _check_deep_think_trigger(question: str) -> bool:
    """Check if the user message contains a manual trigger for Opus."""
    q_lower = question.lower()
    return any(trigger in q_lower for trigger in DEEP_THINK_TRIGGERS)


def _process_and_reply(config: AgentConfig, say, client: WebClient,
                       context: str, question: str, thread_ts: str) -> None:
    """Process Claude call in background thread and reply when done."""
    try:
        # Check if project lead manually triggered deep thinking
        force_opus = _check_deep_think_trigger(question)
        model_override = config.opus_model_id if force_opus else None

        if force_opus:
            logger.info("[%s] Project lead triggered deep thinking — using Opus", config.name)

        reply = _call_claude(config, context, question, thread_ts, model_override=model_override)

        # Check if agent self-escalated via [DEEP_THINK]
        if DEEP_THINK_MARKER in reply:
            escalation_count = _opus_escalations.get(thread_ts, 0)

            if escalation_count >= MAX_OPUS_ESCALATIONS:
                logger.warning("[%s] Max Opus escalations reached (%d) for thread %s",
                               config.name, MAX_OPUS_ESCALATIONS, thread_ts)
                reply = reply.replace(DEEP_THINK_MARKER, "").strip()
                reply += f"\n\n⚠️ _Atingi o limite de escalações — preciso da sua decisão, {PROJECT_LEAD}._"
            else:
                _opus_escalations[thread_ts] = escalation_count + 1
                logger.info("[%s] Self-escalating to Opus (escalation %d/%d) for thread %s",
                            config.name, escalation_count + 1, MAX_OPUS_ESCALATIONS, thread_ts)
                say("_(pensando mais profundamente...)_", thread_ts=thread_ts)

                # Re-run with Opus
                reply = _call_claude(config, context, question, thread_ts,
                                     model_override=config.opus_model_id)
                reply = reply.replace(DEEP_THINK_MARKER, "").strip()

        logger.info("[%s] Replied (%d chars): %s", config.name, len(reply), reply[:80])
        say(reply, thread_ts=thread_ts)
    except Exception as e:
        logger.error("[%s] Background processing error: %s", config.name, e)
        say("(error processing — I saved my progress and will pick up next time)", thread_ts=thread_ts)


# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------

def _handle_message(config: AgentConfig, event: dict, say, client: WebClient) -> None:
    text = event.get("text", "").strip()
    if not text:
        return

    user_id = event.get("user", "")
    if user_id == config.bot_user_id:
        return

    if not _is_mentioned(text, config.bot_user_id):
        return

    clean_text = _strip_mention(text)
    if not clean_text:
        return

    user_name = _resolve_user(client, user_id) if user_id else "someone"
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event.get("ts")

    context = _get_thread_context(client, channel, thread_ts)
    if not context:
        context = _get_channel_context(client, channel)

    logger.info("[%s] Message from %s: %s", config.name, user_name, clean_text[:80])
    logger.debug("[%s] Channel: %s | Thread: %s", config.name, channel, thread_ts)

    thread = threading.Thread(
        target=_process_and_reply,
        args=(config, say, client, context, f"{user_name}: {clean_text}", thread_ts),
        daemon=True,
    )
    thread.start()


def create_handler(config: AgentConfig) -> SocketModeHandler:
    app = App(token=config.bot_token)

    @app.event("app_mention")
    def handle_mention(event, say, client):
        _handle_message(config, event, say, client)

    @app.event("message")
    def handle_message(event, say, client):
        if event.get("channel_type") == "im":
            if event.get("subtype"):
                return
            if event.get("bot_id"):
                return

            text = event.get("text", "").strip()
            if not text:
                return

            user_name = _resolve_user(client, event["user"])
            channel = event["channel"]
            thread_ts = event.get("thread_ts") or event.get("ts")
            context = _get_channel_context(client, channel)

            logger.info("[%s] DM from %s: %s", config.name, user_name, text[:80])
            thread = threading.Thread(
                target=_process_and_reply,
                args=(config, say, client, context, f"{user_name}: {text}", thread_ts),
                daemon=True,
            )
            thread.start()
            return

        _handle_message(config, event, say, client)

    logger.info("[%s] Handler created — listening for @mentions and DMs", config.name)
    return SocketModeHandler(app, config.app_token)
