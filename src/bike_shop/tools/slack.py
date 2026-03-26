"""Slack tool — Socket Mode handler (delegates to handlers.py)."""

from bike_shop.config import AgentConfig
from bike_shop.handlers import create_handler

__all__ = ["create_handler", "AgentConfig"]
