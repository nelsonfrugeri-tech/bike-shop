from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Maps CLI agent name → (env prefix, persona key)
AGENT_REGISTRY: dict[str, tuple[str, str]] = {
    "mr-robot": ("MR_ROBOT", "mr_robot"),
    "elliot": ("ELLIOT", "elliot"),
    "tyrell": ("TYRELL", "tyrell"),
}


@dataclass(frozen=True)
class AgentConfig:
    name: str
    role: str
    bot_token: str
    app_token: str
    system_prompt: str
    agent_key: str = ""
    bot_user_id: str = ""
    github_app_id: str = ""
    github_pem_path: str = ""
    github_installation_id: str = ""


def load_config(agent_name: str) -> AgentConfig:
    """Load config for a single agent by CLI name."""
    load_dotenv()

    if agent_name not in AGENT_REGISTRY:
        available = ", ".join(sorted(AGENT_REGISTRY))
        raise SystemExit(f"Unknown agent '{agent_name}'. Available: {available}")

    env_prefix, persona_key = AGENT_REGISTRY[agent_name]

    from bike_shop.agents import PERSONAS
    from slack_sdk import WebClient

    bot_token = os.environ.get(f"{env_prefix}_BOT_TOKEN", "")
    app_token = os.environ.get(f"{env_prefix}_APP_TOKEN", "")
    if not bot_token or not app_token:
        raise SystemExit(
            f"Missing tokens for {agent_name}. "
            f"Set {env_prefix}_BOT_TOKEN and {env_prefix}_APP_TOKEN in .env"
        )

    try:
        client = WebClient(token=bot_token)
        auth = client.auth_test()
        bot_user_id = auth.get("user_id", "")
    except Exception as e:
        raise SystemExit(f"Failed to authenticate {agent_name}: {e}")

    persona = PERSONAS[persona_key]

    github_app_id = os.environ.get(f"{env_prefix}_GITHUB_APP_ID", "")
    github_pem_path = os.path.expanduser(os.environ.get(f"{env_prefix}_GITHUB_PEM_PATH", ""))
    github_installation_id = os.environ.get(f"{env_prefix}_GITHUB_INSTALLATION_ID", "")

    return AgentConfig(
        name=persona["name"],
        role=persona["role"],
        bot_token=bot_token,
        app_token=app_token,
        system_prompt=persona["system_prompt"],
        agent_key=agent_name,
        bot_user_id=bot_user_id,
        github_app_id=github_app_id,
        github_pem_path=github_pem_path,
        github_installation_id=github_installation_id,
    )


def load_configs() -> list[AgentConfig]:
    """Load all configured agents."""
    configs = []
    for agent_name in AGENT_REGISTRY:
        try:
            configs.append(load_config(agent_name))
        except SystemExit:
            continue
    return configs


def resolve_team_mentions() -> dict[str, str]:
    """Resolve all agent bot_user_ids and return a name→user_id map.

    Returns e.g. {"Elliot Alderson": "U0AP10P0GNM", "Mr. Robot": "U0AN6S94SNT", ...}
    """
    load_dotenv()
    from bike_shop.agents import PERSONAS
    from slack_sdk import WebClient

    mentions: dict[str, str] = {}
    for agent_name, (env_prefix, persona_key) in AGENT_REGISTRY.items():
        bot_token = os.environ.get(f"{env_prefix}_BOT_TOKEN", "")
        if not bot_token:
            continue
        try:
            client = WebClient(token=bot_token)
            auth = client.auth_test()
            user_id = auth.get("user_id", "")
            if user_id:
                persona_name = PERSONAS[persona_key]["name"]
                mentions[persona_name] = user_id
        except Exception:
            continue
    return mentions
