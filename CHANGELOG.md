# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Unified memory schema** (`memory_schema.py`) — single source of truth for memory scopes and types, shared between extraction (write) and router recall (read)
- **Router-driven memory recall** — Semantic Router analyzes message intent and requests targeted Mem0 lookups filtered by scope (team, project, agent) and type (decision, fact, preference, procedure, outcome)
- **`recall_filtered()`** — parallel Mem0 searches with scope + type metadata filtering

### Changed
- **Memory architecture simplified** — removed Redis short-term, `--resume` handles in-thread continuity
  - `--resume` already loads full conversation history within a Slack thread — Redis was duplicating context
  - Mem0 full recall only on **new threads** (no existing `session_id`)
  - Router-driven filtered recall works on **any thread** — agents can access cross-thread decisions even mid-conversation
- **Semantic Router** now receives Slack thread context and classifies memory intent alongside agent + model selection
- **Memory extraction is fire-and-forget** — `observe()` runs Haiku extraction in a background daemon thread, freeing the handler immediately after Slack reply
- `bot_id` added to `AgentConfig` for accurate bot message detection

### Removed
- `short_term.py` — Redis short-term memory (redundant with `--resume`)
- `redis_client.py` — Redis connection singleton
- `summarizer.py` — TTL summarization cron (depended on Redis)
- `redis==5.2.1` dependency
- Redis service from Docker Compose

## [v0.2.0] - 2026-03-31

### Added
- **Semantic Router** — Haiku-powered classifier that selects expert agent + model per message
  - Automatic model selection: opus (deep thinking), sonnet (standard), haiku (simple)
  - Router reason logged in Langfuse traces for full traceability
  - Manual override preserved ("think deeply" → forces opus)
- **Dynamic expert discovery** — router scans `~/.claude/agents/experts/*.md` at boot, parses frontmatter, and builds routing prompt dynamically. Adding a new expert is zero-code: drop a `.md` file and restart
  - Symlink traversal guard (defense-in-depth)
  - Name format validation (`^[a-z][a-z0-9-]*$`)
  - Quoted frontmatter values stripped automatically
  - `EXPERTS_DIR` configurable via environment variable
  - First-sentence extraction via `re.split(r"\.\s", ...)` — preserves version numbers
- **Memory Agent with Mem0** — shared semantic memory replaces per-agent JSON files
  - Qdrant (vector DB) + Ollama (nomic-embed-text, 768 dims, local, zero API cost)
  - `observe()` after every response — Mem0 auto-extracts facts
  - `recall()` before every LLM call — semantic search for relevant context
  - All agents share the same memory pool
- **19 router tests** — frontmatter parsing, discovery, routing delegation, fallbacks, edge cases
- **Sentinel agent** with SRE skill (Google SRE book, observability principles)
- **Langfuse MCP tool** — 5 query tools for all agents (traces, generations, tokens, errors)

### Changed
- Agent personas removed — all three are equal software engineers, no fixed roles
- Think backwards workflow: delivery → test → implementation
- Agents have autonomy + tag teammates for PR reviews, opinions, blockers
- Experts & agents architecture: agents in `~/.claude/agents/` are agnostic experts, Slack bots invoke them dynamically
- Team photos updated — high-quality images with consistent red background theme, standardized to .jpg
- Dead code cleanup: unused deps, broken refs, aligned experts terminology

### Infrastructure
- Docker Compose: added Qdrant + Ollama services alongside Langfuse + Postgres
- Oracle GitHub App for automated PR creation

## [v0.1.0] - 2026-03-28

### Added
- **Multi-agent Slack team** — Mr. Robot, Elliot Alderson, Tyrell Wellick via Socket Mode
- **Claude Code CLI** as LLM backend via subprocess
- **Provider-agnostic architecture** with `LLMProvider` ABC (ready for Codex, OpenAI, etc.)
- **Strict orchestration** — project lead commands, agents execute
- **Anti-loop** — max 5 agent-to-agent interactions per thread
- **Model switching** — Sonnet default, Opus on demand (manual trigger + auto-escalation via `[DEEP_THINK]`)
- **JSON-based memory** per agent — messages, summaries, decisions
- **Full Langfuse observability** — traces, generations, thinking spans, tool spans, error spans
- **Per-agent GitHub App authentication** — JWT → installation token, auto-refresh
- **MANIFEST.md** — team process definition (Discovery → Documentation → Issues → Development → Review → Validation)
- **README.md** — complete setup guide with Slack Apps, GitHub Apps, Langfuse
