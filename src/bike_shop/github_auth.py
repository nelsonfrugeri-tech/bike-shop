from __future__ import annotations

import json
import logging
import time
import urllib.request

from bike_shop.config import AgentConfig

logger = logging.getLogger(__name__)

# Cache: agent_name -> (token, expires_at)
_token_cache: dict[str, tuple[str, float]] = {}


class GitHubAuth:
    """Handles GitHub App authentication via JWT → installation token."""

    def __init__(self, config: AgentConfig) -> None:
        self._config = config

    def get_token(self) -> str | None:
        """Generate a GitHub installation token. Returns None if not configured."""
        config = self._config
        if not config.github_app_id or not config.github_pem_path:
            return None

        try:
            import jwt  # noqa: F811
        except ImportError:
            logger.warning("PyJWT not installed — GitHub App auth disabled")
            return None

        cached = _token_cache.get(config.name)
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

            install_id = config.github_installation_id
            if not install_id:
                req = urllib.request.Request(
                    "https://api.github.com/app/installations",
                    headers={
                        "Authorization": f"Bearer {jwt_token}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                with urllib.request.urlopen(req) as resp:
                    installations = json.loads(resp.read())
                    install_id = str(installations[0]["id"])

            req = urllib.request.Request(
                f"https://api.github.com/app/installations/{install_id}/access_tokens",
                method="POST",
                headers={
                    "Authorization": f"Bearer {jwt_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read())
                token = data["token"]

            _token_cache[config.name] = (token, time.time() + 3500)
            logger.info("[%s] GitHub App token refreshed (install=%s)", config.name, install_id)
            return token
        except Exception as e:
            logger.error("[%s] Failed to get GitHub token: %s", config.name, e)
            return None
