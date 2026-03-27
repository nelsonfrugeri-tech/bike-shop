from __future__ import annotations

import logging
import re

from slack_sdk import WebClient

from bike_shop.config import resolve_team_mentions

logger = logging.getLogger(__name__)

# Resolved lazily at first use
_team_mentions: dict[str, str] | None = None


def get_team_mentions() -> dict[str, str]:
    global _team_mentions
    if _team_mentions is None:
        _team_mentions = resolve_team_mentions()
        logger.info("Team mentions resolved: %s", _team_mentions)
    return _team_mentions


def build_mention_instruction(agent_name: str) -> str:
    """Build instruction telling the agent how to @mention teammates."""
    mentions = get_team_mentions()
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
    lines.append(
        "Example: instead of writing 'Tyrell should handle this', "
        "write '<@UXXXXX> should handle this'."
    )
    return "\n".join(lines)


def get_thread_context(client: WebClient, channel: str, thread_ts: str, limit: int = 20) -> str:
    try:
        result = client.conversations_replies(channel=channel, ts=thread_ts, limit=limit)
        messages = result.get("messages", [])
    except Exception:
        return ""

    return "\n".join(f"<{m.get('user', 'bot')}>: {m.get('text', '')}" for m in messages)


def get_channel_context(client: WebClient, channel: str, limit: int = 10) -> str:
    try:
        result = client.conversations_history(channel=channel, limit=limit)
        messages = result.get("messages", [])
    except Exception:
        return ""

    return "\n".join(
        f"<{m.get('user', 'bot')}>: {m.get('text', '')}" for m in reversed(messages)
    )


def resolve_user(client: WebClient, user_id: str) -> str:
    try:
        info = client.users_info(user=user_id)
        profile = info["user"]["profile"]
        return profile.get("display_name") or profile.get("real_name") or user_id
    except Exception:
        return user_id


def strip_mention(text: str) -> str:
    return re.sub(r"<@[A-Z0-9]+>", "", text).strip()


def is_mentioned(text: str, bot_user_id: str) -> bool:
    return f"<@{bot_user_id}>" in text
