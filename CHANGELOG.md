# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Removed
- **MANIFEST.md removed** — agent skill weights and team process no longer injected into prompts; `_read_project_context()` function deleted from handler

### Fixed
- **Tool result parsing for Langfuse spans** — `_handle_event()` now parses `type: "user"` events containing `tool_result` content blocks (the actual Claude CLI stream-json format), fixing tool spans showing `output=NULL` in Langfuse. Legacy `type: "result"` format kept for backwards compatibility.
- **Per-project Tracer propagation to provider** — `_call_llm_batch` now resolves the per-project `Tracer` and passes it to `provider.call()`, fixing a `NameError` where the undefined `tracer` variable was referenced (#33)
- **Cross-project session resume** — `SessionStore.get()` now validates `project_id` before resuming, preventing Claude CLI crash (rc=1) when agents switch between projects

### Added
- **Worktree git diff in Langfuse traces** — after each LLM call, a `worktree.diff` span captures `git diff --stat HEAD` from the agent's worktree, providing visibility into file changes per interaction (#34)
- **Multi-project support** — agents can work on multiple repos from a single platform
  - `projects.yaml` config file maps Slack channels to projects with per-project repo paths, worktree dirs, Mem0 collections, and Langfuse keys
  - `ProjectConfig` (frozen dataclass) carries all project-specific settings
  - `ProjectRegistry` loads and indexes projects by ID and Slack channel
  - `ProjectResolver` resolves channel/thread to project: channel mapping -> thread inheritance -> default fallback
  - `SessionStore` tracks `project_id` per thread for cross-message continuity
  - `get_mem0()` supports multiple Qdrant collections (dict of singletons)
  - `MemoryAgent` accepts `mem0_collection` parameter for per-project memory isolation
  - `Tracer` accepts per-project Langfuse keys via constructor
  - `ensure_worktree()` and `create_worktree()` accept `repo_path`/`worktree_dir` overrides with env var fallback
  - Handler creates per-project `MemoryAgent` and `Tracer` instances lazily
  - 21 new tests covering registry, resolver, session project_id, multi-collection Mem0, and worktree overrides
  - Full backwards compatibility: all changes use optional params with env var fallbacks

### Removed
- `memory-keeper` MCP server — replaced by Mem0 (Qdrant + Ollama) since v0.2.0

### Fixed
- **Langfuse spans input/output null** — Langfuse REST API expects JSON objects, not plain strings
  - Added `_ensure_json_object()` helper that wraps strings/scalars as `{"value": ...}`
  - Applied to all Tracer methods: `start_trace`, `update_trace`, `start_span`, `end_span`, `start_generation`, `end_generation`
  - Added meaningful `input=`/`output=` to all handler spans: `message.receive`, `router.classify`, `memory.recall`, `prompt.build`, `llm.call`, `memory.observe`, `slack.reply`

### Changed
- **Idle-based watchdog** replaces static timeout tiers for Claude CLI batch mode
  - Monitors stdout activity: kills process only when idle for `CLAUDE_IDLE_TIMEOUT` (default 300s)
  - Absolute safety net via `CLAUDE_MAX_TIMEOUT` (default 1800s)
  - Graceful shutdown: SIGTERM -> wait -> SIGKILL to entire process group
  - Uses `time.monotonic()` for accurate elapsed-time measurement
  - Dedicated `_stderr_reader` thread for incremental stderr collection
  - Removes `CLAUDE_TIMEOUT_SMALL/MEDIUM/LARGE` and `_select_timeout()`

### Added
- **Hierarchical observability** — real-time tracing with nested spans via Langfuse REST API
  - `Tracer` rewritten with `start_trace()`, `start_span()`, `end_span()`, `start_generation()`, `end_generation()` API
  - Micro-batch flushing (configurable via `LANGFUSE_FLUSH_INTERVAL_MS`, default 500ms)
  - Trace detail levels via `LANGFUSE_TRACE_DETAIL` (full/basic/off)
  - Backwards-compatible `trace_call()` and `trace_error()` still work
  - `atexit` flush ensures buffer drains on shutdown
- **Streaming provider** — `subprocess.Popen` replaces `subprocess.run` for real-time span creation
  - Tool, thinking, and error spans created as events stream from Claude CLI
  - Fallback to batch mode via `LANGFUSE_STREAM_ENABLED=false` (read at call time, not import time)
  - Streaming mode uses worktree `workspace` param (not env var) for isolation
- **Full-stack instrumentation** — every layer creates hierarchical spans:
  - `message.receive` -> `router.classify` -> `memory.recall` -> `prompt.build` -> `llm.call` -> `memory.observe` -> `slack.reply`
  - Router creates `router.llm` generation child span
  - Memory agent creates `mem0.search` spans per scope and `extraction.haiku` + `mem0.store` spans during observe
  - `memory.observe` span closed in background thread after sub-spans complete
- **Shared event parser** (`_handle_event` + `_ParseState`) — eliminates ~80 lines of duplicated parsing logic between batch and streaming modes
- **26 new tests** — observability tracer (batch buffer, spans, generations, detail levels, backwards compat), streaming parser, provider mode selection

### Previously added
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
