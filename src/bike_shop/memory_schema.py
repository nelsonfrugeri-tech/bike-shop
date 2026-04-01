"""Memory domain schema — single source of truth for scopes and types.

Used by both extraction (write) and router recall (read).
Adding a new scope or type here automatically propagates to both systems.
"""

MEMORY_SCOPES: dict[str, str] = {
    "team": "global team preferences, procedures, conventions",
    "project": "project-specific decisions, facts, outcomes",
    "agent": "agent's own implementation details and patterns",
}

MEMORY_TYPES: dict[str, str] = {
    "decision": "choices made by the team or agent",
    "fact": "technical facts, stack info, architecture details",
    "preference": "how the team likes to work, coding style, tools",
    "procedure": "workflows, processes, step-by-step procedures",
    "outcome": "results of actions, deployments, experiments",
}


def scopes_description() -> str:
    """Format scopes for LLM prompts."""
    return " | ".join(f"{k} ({v})" for k, v in MEMORY_SCOPES.items())


def types_description() -> str:
    """Format types for LLM prompts."""
    return " | ".join(f"{k} ({v})" for k, v in MEMORY_TYPES.items())


def valid_scopes() -> set[str]:
    return set(MEMORY_SCOPES.keys())


def valid_types() -> set[str]:
    return set(MEMORY_TYPES.keys())
