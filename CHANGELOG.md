# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Two-tier memory architecture** — Redis (short-term) + Mem0/Qdrant (long-term)
  - Short-term: per-agent, per-project, per-thread conversation buffers in Redis (24h TTL)
  - Long-term: three scopes — team (global), project (shared), agent (private)
  - Selective extraction via Haiku — only facts, decisions, and outcomes enter long-term memory
  - TTL summarization cron — conversations are summarized before Redis eviction, stored in Mem0
  - Route decision tracking — every message records which expert and model the router selected
- Redis service added to Docker Compose (redis:7.4-alpine)
- `redis==5.2.1` dependency added
- `python -m bike_shop.summarizer` — standalone cron entry point for TTL summarization

### Changed
- `MemoryAgent` refactored: constructor takes `agent_key`, recall does 5 parallel lookups (3 Mem0 + 2 Redis)
- `observe()` now runs selective extraction instead of storing raw exchanges
- Memory scoped by team/project/agent instead of single shared pool

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
