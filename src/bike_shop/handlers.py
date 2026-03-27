"""Backward-compatible entry point. Use bike_shop.slack.handler directly."""

from bike_shop.providers.claude import ClaudeProvider
from bike_shop.slack.handler import SlackAgentHandler
from bike_shop.config import AgentConfig

from slack_bolt.adapter.socket_mode import SocketModeHandler

_provider = ClaudeProvider()


def create_handler(config: AgentConfig) -> SocketModeHandler:
    """Create a Socket Mode handler for the given agent config."""
    agent = SlackAgentHandler(config, _provider)
    return agent.create_socket_handler()
