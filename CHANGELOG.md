# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Changed
- Team photos updated — new high-quality images with consistent red background theme
- Standardized image format to .jpg (was mixed .png/.jpeg)
- README references updated to match new filenames
- **Semantic Router: dynamic expert discovery** — experts are no longer hardcoded in `router.py`. On boot, the router scans `~/.claude/agents/experts/*.md`, parses frontmatter, and builds the routing prompt dynamically. Adding a new expert is zero-code: drop the `.md` file and restart
- Router hardening: symlink traversal guard, name format validation (`^[a-z][a-z0-9-]*$`), quoted frontmatter values stripped
- 9 new tests for router: quoted names, invalid name format, symlink outside dir, route delegation, unknown expert fallback, timeout fallback, version numbers in descriptions, period-space split, no trailing period (19 total)
- `EXPERTS_DIR` configurable via env var with default fallback
- First-sentence extraction uses `re.split(r"\.\s", ...)` — splits on ". " (period+space), preserves version numbers like "v2.0"

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

## [v0.2.0] - 2026-03-29 (unreleased)

### Semantic Router - Haiku-powered classifier that decides agent (spirit) + model per message
- Automatic model selection: opus (deep thinking), sonnet (standard), haiku (simple)
- Router reason logged in Langfuse traces for full traceability
- Manual override still works ("think deeply" → forces opus)

### Memory Agent with [Mem0](https://github.com/mem0ai/mem0)
- Shared semantic memory via Mem0 (replaces per-agent JSON files)
- [Qdrant](https://qdrant.tech/) (vector DB) + [Ollama](https://ollama.com/) ([nomic-embed-text](https://ollama.com/library/nomic-embed-text), 768 dims, local, zero API cost)
- `observe()` after every response — Mem0 auto-extracts facts
- `recall()` before every LLM call — semantic search for relevant context
- All agents read/write to the same memory — no more silos
- Migrated all previous agent memories into Mem0

### Agent Behavior
- Removed all agent personalities — pure software engineers
- Think backwards: delivery → test → implementation
- Agents have autonomy + tag teammates for PR reviews, opinions, blockers
- Spirits & Bodies architecture: agents in `~/.claude/agents/` are agnostic spirits, Slack bots are bodies

### Observability
- Router decisions in Langfuse: selected_agent, router_model, router_reason
- Sentinel agent with SRE skill (Google SRE book, observability principles)
- Langfuse MCP tool (5 query tools for all agents)

### Infrastructure
- Docker Compose: added Qdrant + Ollama services
- Oracle GitHub App for automated PR creation
