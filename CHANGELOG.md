# Changelog

All notable changes to this project will be documented in this file.

## [v0.1.0] - 2026-03-28

### First release — Multi-agent team platform

#### Core
- Multi-agent Slack team with Socket Mode (Mr. Robot, Elliot Alderson, Tyrell Wellick)
- Claude Code CLI as LLM backend via subprocess
- Provider-agnostic architecture with `LLMProvider` ABC (ready for Codex, OpenAI, etc.)
- SOLID module structure: providers/, slack/, session, memory, observability, github_auth, model_switch

#### Agent Behavior
- Strict orchestration: project lead commands, agents execute
- Agents ask permission before executing (tag project lead via Slack)
- Mandatory PR code review workflow between agents
- Anti-loop: max 5 agent-to-agent interactions per thread (enforced in code)
- Non-substantive response suppression ("No response requested" silenced)
- Specialized sub-agent invocation via Agent tool (architect, review-py, debater, explorer, dev-py, tech-pm, builder)

#### Model Switching
- Sonnet as default, Opus on demand
- Manual trigger: project lead says "think deeply" → forces Opus
- Auto-escalation: agents detect failure and self-escalate via `[DEEP_THINK]` marker
- Max 2 Opus escalations per thread, auto-return to Sonnet

#### Memory
- JSON-based memory per agent (messages, summaries, decisions)
- Auto-records incoming messages and outgoing responses
- Auto-summarizes every 10 messages via haiku
- Recent context (last 10 msgs + summaries + decisions) injected into every prompt

#### Observability (Langfuse)
- Full trace capture: input, output, tokens, model, duration, tools, thinking, errors
- Nested spans: trace → generation → thinking/tool_use/error spans
- Docker Compose for local Langfuse + Postgres
- Langfuse MCP tool for querying traces programmatically
- Sentinel agent (spirit) for SRE/observability queries

#### GitHub Integration
- Per-agent GitHub App authentication (JWT → installation token, auto-refresh)
- Agents create issues, PRs, comments with their own identity
- Workspace isolation via `AGENT_WORKSPACE` env var

#### Configuration
- All settings via environment variables (`.env`)
- `PROJECT_LEAD_NAME` and `PROJECT_LEAD_SLACK_ID` for configurable orchestration
- Provider-agnostic bin scripts: `bin/claude/` (ready for `bin/codex/`, etc.)
- MCP servers: Notion, draw.io, Excalidraw, memory-keeper, Langfuse

#### Documentation
- MANIFEST.md: team process (Discovery → Documentation → Issues → Development → Review → Validation)
- README.md: complete setup guide with Slack Apps, GitHub Apps, Langfuse
- SRE Observability skill for Sentinel agent

### Planned (v0.2.0)
- Semantic Router: auto-select agent + model per message context ([#3](https://github.com/nelsonfrugeri-tech/bike-shop/issues/3))
- Memory Agent with Mem0: shared memory, decision extraction, semantic search ([#4](https://github.com/nelsonfrugeri-tech/bike-shop/issues/4))
