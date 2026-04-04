"""Backward-compatible entry point. Use bike_shop.slack.handler directly."""

from bike_shop.providers.claude import ClaudeProvider
from bike_shop.slack.handler import SlackAgentHandler
from bike_shop.config import AgentConfig
from bike_shop.project import ProjectRegistry

from slack_bolt.adapter.socket_mode import SocketModeHandler

_provider = ClaudeProvider()


def create_handler(
    config: AgentConfig,
    project_registry: ProjectRegistry | None = None,
) -> SocketModeHandler:
    """Create a Socket Mode handler for the given agent config."""
    agent = SlackAgentHandler(config, _provider, project_registry=project_registry)
    return agent.create_socket_handler()
