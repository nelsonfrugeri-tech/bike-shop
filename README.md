<div align="center">

# 🏍️ Bike Shop

### AI-Powered Multi-Agent Software Engineering Team

[![Python 3.13+](https://img.shields.io/badge/Python-3.13+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org)
[![Claude Code](https://img.shields.io/badge/Claude_Code-CLI-CC785C?style=for-the-badge&logo=anthropic&logoColor=white)](https://docs.anthropic.com/en/docs/claude-code)
[![Slack](https://img.shields.io/badge/Slack-Bot_Platform-4A154B?style=for-the-badge&logo=slack&logoColor=white)](https://api.slack.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)

**Three AI coding agents collaborate in Slack to build software — from discovery to deployment.**
**You orchestrate. They code, test, review, and ship.**

[How It Works](#-how-it-works) · [Architecture](#-architecture) · [The Team](#-the-team) · [Getting Started](#-getting-started) · [Observability](#-observability)

</div>

---

## 🎯 What is Bike Shop?

Bike Shop is a **multi-agent platform** where AI software engineers collaborate in Slack channels to build real software. You act as the project lead — directing, deciding, and validating — while the agents code, test, review PRs, and ship features autonomously.

This project builds on top of [**claude-code**](https://github.com/nelsonfrugeri-tech/claude-code) — a foundation layer that provides reusable **experts** and **skills** that Bike Shop agents dynamically adopt based on the task context. The experts and skills live in `~/.claude/agents/experts/` and `~/.claude/skills/` and are provider-agnostic — they can be used by any application, not just Bike Shop.

---

## 👥 The Team

<div align="center">
<table>
<tr>
<td align="center" width="33%">
<img src="assets/team/mr_robot.jpg" width="150" height="150" style="border-radius: 50%;" alt="Mr. Robot"/>
<br/>
<strong>Mr. Robot</strong>
<br/>
<em>Software Engineer</em>
</td>
<td align="center" width="33%">
<img src="assets/team/elliot.jpg" width="150" height="150" style="border-radius: 50%;" alt="Elliot Alderson"/>
<br/>
<strong>Elliot Alderson</strong>
<br/>
<em>Software Engineer</em>
</td>
<td align="center" width="33%">
<img src="assets/team/tyrell.jpg" width="150" height="150" style="border-radius: 50%;" alt="Tyrell Wellick"/>
<br/>
<strong>Tyrell Wellick</strong>
<br/>
<em>Software Engineer</em>
</td>
</tr>
</table>
</div>

All three are **equal software engineers** — no fixed roles, no personalities. They think backwards from delivery: _How will the project lead test this? → How do I prove it works? → What's the simplest implementation?_ — then they code.

The **Semantic Router** dynamically assigns them specialized experts (architect, reviewer, debater, etc.) based on the task at hand.

---

## 🔄 How It Works

### The Full Message Flow

```
1. You type @Mr. Robot in Slack: "let's design the notification system"

2. Slack sends the event via WebSocket (Socket Mode) to the bike-shop Python process

3. SlackAgentHandler receives the event:
   a. Checks if the bot was @mentioned
   b. Fetches thread context (last 20 messages) from Slack API

4. Semantic Router classifies the message:
   → agent + model selection
   → memory intent: decides if cross-thread memory is needed
   → { agent: "architect", model: "opus", reason: "...", memory: [{query: "...", scopes: [...], types: [...]}] }

5. Memory recall (two modes):
   a. New thread (no session): full recall — searches all 3 Mem0 scopes (agent, project, team)
   b. Router-driven: filtered recall — targeted searches by scope + type (decision, procedure, etc.)
   c. Existing thread with --resume and no memory request: skip (--resume has full history)

6. Prompt is assembled:
   System prompt + Project manifest + Memory context + Thread context + User message

7. Claude Code CLI is called:
   claude -p "{prompt}" --agent architect --model opus --resume {session_id} --dangerously-skip-permissions

8. Response is parsed from stream-json:
   - Text response extracted
   - Tool uses captured (Bash, Write, Read, etc.)
   - Thinking blocks captured
   - Token usage extracted
   - Session ID stored for thread continuity (--resume on next message)

9. Full trace sent to Langfuse:
   Trace → Generation → Thinking spans → Tool spans → Error spans

10. MemoryAgent.observe() extracts facts in background (fire-and-forget daemon thread)
    → Haiku classifies: type (decision/fact/preference/procedure/outcome) + scope (team/project/agent)
    → Stores in Mem0/Qdrant with metadata for filtered retrieval

11. Response posted back to Slack thread
```

### Semantic Router — The Decision Brain

Every incoming message passes through the Semantic Router before reaching the LLM. It classifies agent, model, and memory intent:

```json
{
  "agent": "architect",
  "model": "opus",
  "reason": "System design with multiple components requires deep architectural thinking",
  "memory": [
    {"query": "architecture decisions", "scopes": ["project"], "types": ["decision"]},
    {"query": "deployment procedures", "scopes": ["team"], "types": ["procedure"]}
  ]
}
```

The router **dynamically discovers experts** at boot by scanning `~/.claude/agents/experts/*.md` and parsing each file's frontmatter (`name` + `description`). Adding a new expert is zero-code: drop a `.md` file in the experts directory and restart. Example routing decisions:

| Task Type | Expert | Model | Why |
|-----------|---------------|-------|-----|
| Architecture, system design | `architect` | opus | Deep thinking, trade-offs |
| Code review, PR review | `review-py` | sonnet | Standard analysis |
| Comparing approaches, trade-offs | `debater` | sonnet/opus | Depends on depth |
| Exploring existing codebase | `explorer` | sonnet | Code navigation |
| Heavy Python implementation | `dev-py` | sonnet | Standard coding |
| Business analysis, product | `tech-pm` | sonnet | Standard analysis |
| Infrastructure setup | `builder` | sonnet | Standard execution |
| Simple question, confirmation | (none) | haiku | Quick and cheap |

### Experts & Agents — The Architecture

This project follows an **experts & agents** architecture inspired by the [claude-code](https://github.com/nelsonfrugeri-tech/claude-code) foundation:

- **Experts** (`~/.claude/agents/experts/`) — Agnostic, reusable capabilities. They don't know about Slack, Bike Shop, or any specific project. They're pure expertise: architecture, code review, SRE, coding, etc.

- **Agents** (Mr. Robot, Elliot, Tyrell) — The Slack bots that receive messages and invoke experts based on context. The Semantic Router decides which expert an agent assumes for each task.

```
~/.claude/agents/experts/  ← Experts (from claude-code project)
  architect.md             ← Knows architecture
  review-py.md             ← Knows code review
  debater.md               ← Knows how to debate trade-offs
  dev-py.md                ← Knows Python implementation
  tech-pm.md               ← Knows product management
  explorer.md              ← Knows codebase exploration
  builder.md               ← Knows infrastructure setup
  memory-agent.md          ← Knows fact extraction

~/.claude/agents/founds/   ← Foundational agents (ecosystem-only)
  oracle.md                ← Ecosystem manager
  sentinel.md              ← SRE/observability

~/.claude/skills/          ← Skills (from claude-code project)
  arch-py.md               ← Python architecture patterns
  review-py.md             ← Code review checklists
  sre-observability.md     ← SRE principles (Google SRE book)
  ai-engineer.md           ← LLM/RAG/Agent patterns
  product-manager.md       ← Product management practices

bike-shop/                 ← Agents (this project)
  Mr. Robot                ← Slack bot that uses experts
  Elliot Alderson          ← Slack bot that uses experts
  Tyrell Wellick           ← Slack bot that uses experts
```

### Slack Integration — Socket Mode

Each agent runs as a **separate Slack App** connected via **Socket Mode** (WebSocket). This means:

- **No public URL needed** — everything runs locally
- **Real-time** — messages arrive instantly via WebSocket
- **Multiple connections** — each agent has its own WebSocket connection
- **Thread tracking** — each Slack thread maps to a Claude session via `--resume`

```
Slack Workspace
  ├── #project-channel
  │     ├── @Mr. Robot (Socket Mode, WebSocket)
  │     ├── @Elliot Alderson (Socket Mode, WebSocket)
  │     └── @Tyrell Wellick (Socket Mode, WebSocket)
  │
  └── Each bot listens for:
        ├── app_mention (someone @mentioned the bot)
        ├── message.channels (channel messages for bot-to-bot interaction)
        ├── message.groups (private channels)
        └── message.im (direct messages)
```

**How threading works:**
1. You send a message mentioning `@Mr. Robot` → creates a thread
2. Mr. Robot responds in the thread
3. You reply in the same thread → the handler fetches thread context (last 20 msgs)
4. The Claude CLI session is resumed via `--resume {session_id}` (24h TTL)
5. Context is maintained across the entire thread conversation

### Memory System — Mem0

All agents share long-term memory powered by [Mem0](https://github.com/mem0ai/mem0), with two recall modes:

```
┌─────────────────────────────────────────────────────────────┐
│                    Mem0 / Qdrant (Shared)                   │
│                                                             │
│  Scopes:          Types:                                    │
│  ├── team         ├── decision   ("chose Qdrant")           │
│  ├── project      ├── fact       ("Python 3.12, Next.js")   │
│  └── agent        ├── preference ("team prefers TDD")       │
│                   ├── procedure  ("deploy via make deploy")  │
│                   └── outcome    ("dashboard deployed OK")   │
│                                                             │
│  ┌──── Qdrant (vectors) ────┐                               │
│  │  nomic-embed-text (768d) │                               │
│  │  via Ollama (local GPU)  │                               │
│  └──────────────────────────┘                               │
└─────────────────────────────────────────────────────────────┘
   ▲ observe()                    │ recall (2 modes)
   │ (fire-and-forget)            │
   │                   ┌──────────┴──────────┐
   │                   │                     │
   │            Full recall            Filtered recall
   │          (new threads)         (router-driven)
   │          3 scopes, all       specific scope + type
   │                   │                     │
   │                   └──────────┬──────────┘
   │                              ▼
   ┌─────────┐  ┌─────────┐  ┌─────────┐
   │Mr. Robot │  │ Elliot  │  │ Tyrell  │
   └─────────┘  └─────────┘  └─────────┘
```

**How it works:**
1. **New thread** → `recall()` searches all 3 Mem0 scopes for relevant context
2. **Existing thread** → `--resume` handles in-thread continuity; router may request targeted memory lookups
3. **Router-driven recall** → Semantic Router classifies memory intent and requests filtered searches by scope + type
4. **After each response** → `observe()` runs extraction in a background thread (fire-and-forget), Haiku classifies type + scope, stores in Qdrant
5. All agents read from the same memory — cross-thread, cross-agent knowledge

**Domain schema** (`memory_schema.py`): single source of truth for scopes and types — adding a new scope or type automatically propagates to extraction and router recall.

**Stack:** Qdrant (vector DB) + Ollama (nomic-embed-text, 768 dimensions, local GPU, zero API cost)

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🧠 **Semantic Router** | Classifies every message → selects agent + model + memory intent |
| 🧬 **Shared Memory** | Mem0 with semantic search — all agents share the same project memory |
| 🌿 **Worktree Isolation** | Each agent works in an isolated git worktree — no PR cross-contamination |
| ⚡ **Message Batching** | Rapid-fire messages are buffered and processed as a single consolidated batch |
| 📊 **Full Observability** | Langfuse traces: input, output, tokens, tools, thinking, errors, router decisions |
| 🔄 **Model Switching** | Automatic (router), manual ("think deeply"), self-escalation (`[DEEP_THINK]`) |
| 🤝 **Smart Collaboration** | Agents tag teammates for PR reviews and blockers — anti-loop (max 5/thread) |
| 🔐 **GitHub Identity** | Each agent has its own GitHub App with JWT auth |
| **Experts Architecture** | Agents dynamically assume specialized experts from [claude-code](https://github.com/nelsonfrugeri-tech/claude-code) |
| 👁️ **Sentinel** | SRE agent for querying Langfuse: tokens, costs, errors, health |

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    PROJECT LEAD (Slack)                  │
└────────────────────────┬────────────────────────────────┘
                         │
              ┌──────────▼──────────┐
              │   Semantic Router   │  ← haiku classifies
              │  (agent + model)    │     every message
              └──────────┬──────────┘
                         │
         ┌───────────────┼───────────────┐
         ▼               ▼               ▼
   ┌──────────┐   ┌──────────┐   ┌──────────┐
   │ Mr. Robot │   │  Elliot  │   │  Tyrell  │
   └─────┬────┘   └─────┬────┘   └─────┬────┘
         │               │               │
         └───────────────┼───────────────┘
                         │
              ┌──────────▼──────────┐
              │   Claude Code CLI   │  ← --agent {expert}
              │   --model {model}   │     --model {opus|sonnet|haiku}
              └──────────┬──────────┘
                         │
         ┌───────────────┼───────────────┐
         ▼               ▼               ▼
   ┌──────────┐   ┌──────────┐   ┌──────────┐
   │  Mem0     │   │ Langfuse │   │  GitHub  │
   │ (memory)  │   │ (traces) │   │ (code)   │
   └──────────┘   └──────────┘   └──────────┘
```

---

## 🛠️ Tech Stack

<div align="center">

| Layer | Technology | Purpose |
|-------|-----------|---------|
| ![Claude](https://img.shields.io/badge/Claude-CC785C?style=flat-square&logo=anthropic&logoColor=white) | Claude Code CLI | LLM backbone (Opus / Sonnet / Haiku) |
| ![Python](https://img.shields.io/badge/Python-3776AB?style=flat-square&logo=python&logoColor=white) | Python 3.13+ | Core platform |
| ![Slack](https://img.shields.io/badge/Slack-4A154B?style=flat-square&logo=slack&logoColor=white) | Slack Bolt (Socket Mode) | Communication interface |
| ![GitHub](https://img.shields.io/badge/GitHub-181717?style=flat-square&logo=github&logoColor=white) | GitHub Apps + gh CLI | Code, PRs, Issues |
| ![Langfuse](https://img.shields.io/badge/Langfuse-000000?style=flat-square&logoColor=white) | Langfuse v2 | Observability & tracing |
| ![Qdrant](https://img.shields.io/badge/Qdrant-DC382D?style=flat-square&logoColor=white) | Qdrant | Vector DB for Mem0 |
| ![Ollama](https://img.shields.io/badge/Ollama-000000?style=flat-square&logoColor=white) | Ollama (nomic-embed-text) | Local embeddings (zero cost) |
| ![Docker](https://img.shields.io/badge/Docker-2496ED?style=flat-square&logo=docker&logoColor=white) | Docker Compose | Infrastructure |
| ![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?style=flat-square&logo=postgresql&logoColor=white) | PostgreSQL 16 | Langfuse backend |
| ![Mem0](https://img.shields.io/badge/Mem0-FF6B6B?style=flat-square&logoColor=white) | Mem0 | Shared semantic memory |

</div>

---

## 📁 Project Structure

```
bike-shop/
├── src/bike_shop/
│   ├── main.py                  # CLI: bike-shop agent:all, --status, --stop
│   ├── config.py                # AgentConfig, MODEL_MAP, env loading
│   ├── agents.py                # Agent prompts (common rules, no personality)
│   ├── router.py                # Semantic Router (haiku → agent + model, dynamic expert discovery)
│   ├── memory_agent.py          # MemoryAgent (Mem0: recall, recall_filtered, observe)
│   ├── memory_schema.py         # Unified memory domain (scopes + types)
│   ├── worktree.py              # Git worktree isolation per agent/task
│   ├── accumulator.py           # Message batching (buffer window + batch flush)
│   ├── observability.py         # Langfuse tracer (traces, spans, errors)
│   ├── github_auth.py           # GitHub App JWT → installation token
│   ├── session.py               # Session tracking per Slack thread (24h TTL)
│   ├── model_switch.py          # Deep think triggers, [DEEP_THINK] escalation
│   ├── handlers.py              # Entry point — wires config to SlackAgentHandler
│   ├── providers/
│   │   ├── __init__.py          # LLMProvider ABC (provider-agnostic)
│   │   └── claude.py            # ClaudeProvider (Claude CLI + full stream-json parsing)
│   └── slack/
│       ├── context.py           # Thread context, mentions, user resolution
│       └── handler.py           # SlackAgentHandler (orchestrates the full flow)
├── assets/team/                 # Agent avatars
├── docker-compose.yml           # Langfuse + Postgres + Qdrant + Ollama
├── mcp.json                     # MCP servers (Notion, draw.io, Excalidraw, memory-keeper)
├── MANIFEST.md                  # Team process definition
├── CHANGELOG.md                 # Release history
├── pyproject.toml               # Dependencies and build config
└── .env.example                 # All configuration variables
```

---

## 🚀 Getting Started

### Prerequisites

| Tool | Install |
|------|---------|
| ![Python](https://img.shields.io/badge/Python_3.13+-3776AB?style=flat-square&logo=python&logoColor=white) | `brew install python@3.13` |
| ![uv](https://img.shields.io/badge/uv-DE5FE9?style=flat-square&logoColor=white) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| ![Claude](https://img.shields.io/badge/Claude_Code-CC785C?style=flat-square&logo=anthropic&logoColor=white) | `npm install -g @anthropic-ai/claude-code` then `claude auth login` |
| ![Docker](https://img.shields.io/badge/Docker-2496ED?style=flat-square&logo=docker&logoColor=white) | [Docker Desktop](https://docker.com/products/docker-desktop/) |
| ![gh](https://img.shields.io/badge/gh_CLI-181717?style=flat-square&logo=github&logoColor=white) | `brew install gh` then `gh auth login` |

### Step 1: Clone & Install

```bash
git clone https://github.com/nelsonfrugeri-tech/bike-shop.git
cd bike-shop
uv tool install -e . --python 3.13
bike-shop --help
```

### Step 2: Start Infrastructure

```bash
# Start all services
docker compose up -d
# Starts: Langfuse (localhost:3000), Postgres, Qdrant (localhost:6333), Ollama (localhost:11434)

# Pull the embedding model for Mem0 (~274MB)
docker exec bike_shop-ollama-1 ollama pull nomic-embed-text

# Setup Langfuse: open localhost:3000, create account, create project, copy API keys
```

### Step 3: Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your values:

```bash
# Slack tokens (one per agent)
MR_ROBOT_BOT_TOKEN=xoxb-...
MR_ROBOT_APP_TOKEN=xapp-...
ELLIOT_BOT_TOKEN=xoxb-...
ELLIOT_APP_TOKEN=xapp-...
TYRELL_BOT_TOKEN=xoxb-...
TYRELL_APP_TOKEN=xapp-...

# GitHub Apps (optional, for git operations)
MR_ROBOT_GITHUB_APP_ID=...
MR_ROBOT_GITHUB_PEM_PATH=~/.ssh/bike-shop-mr-robot.pem
MR_ROBOT_GITHUB_INSTALLATION_ID=...

# Project
PROJECT_LEAD_NAME=YourName
PROJECT_LEAD_SLACK_ID=U0XXXXXXX

# Observability
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...

# Memory
ANTHROPIC_API_KEY=sk-ant-...  # For Mem0 fact extraction via haiku
```

### Step 4: Create Slack Apps

Each agent needs its own Slack App. Repeat for Mr. Robot, Elliot Alderson, and Tyrell Wellick:

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. **Socket Mode** → Enable → generate App-Level Token (`xapp-...`)
3. **OAuth & Permissions** → Bot Token Scopes:
   - `app_mentions:read`, `chat:write`, `channels:history`, `channels:read`
   - `groups:history`, `im:history`, `im:read`, `users:read`
4. **Install to Workspace** → copy Bot Token (`xoxb-...`)
5. **Event Subscriptions** → Enable → subscribe to:
   - `app_mention`, `message.channels`, `message.groups`, `message.im`
6. **App Home** → Enable Messages Tab
7. Invite to channels: `/invite @Mr. Robot`

### Step 5: Create GitHub Apps (Optional)

For agents that need to create PRs, issues, and comments:

1. Go to [github.com/settings/apps](https://github.com/settings/apps) → **New GitHub App**
2. Permissions: Contents (write), Issues (write), Pull requests (write), Pages (write), Metadata (read)
3. Generate private key → `mv ~/Downloads/*.pem ~/.ssh/bike-shop-{agent}.pem && chmod 600 ~/.ssh/bike-shop-{agent}.pem`
4. Install on selected repositories

### Step 6: Install Foundation (claude-code)

The experts and skills come from the [claude-code](https://github.com/nelsonfrugeri-tech/claude-code) project:

```bash
# Clone the foundation layer (if not already set up)
git clone https://github.com/nelsonfrugeri-tech/claude-code.git ~/.claude
```

This provides `~/.claude/agents/experts/` (experts) and `~/.claude/skills/` that the Semantic Router uses.

### Step 7: Run

```bash
# Start all agents
bike-shop agent:all

# Start a single agent
bike-shop agent:mr-robot

# Check who's running
bike-shop --status

# Stop an agent
bike-shop --stop agent:mr-robot
```

### Step 8: Test

1. Go to your Slack workspace
2. Mention a bot: `@Mr. Robot hello, what project are we working on?`
3. The bot should respond using shared memory from Mem0
4. Check Langfuse (localhost:3000) for the trace

---

## 📊 Observability

### Langfuse Dashboard (localhost:3000)

Every Slack message produces a **hierarchical real-time trace** — spans are created as events stream from Claude CLI, not batched after completion:

```
Trace: "mr-robot/slack-message"
├── Span: "message.receive"
├── Span: "router.classify"
│   └── Generation: "router.llm" (sonnet, with tokens)
├── Span: "memory.recall"
│   ├── Span: "mem0.search" scope=agent
│   ├── Span: "mem0.search" scope=project
│   └── Span: "mem0.search" scope=team
├── Span: "prompt.build"
├── Span: "llm.call"
│   └── Generation: "claude-cli"
│       ├── Span: "thinking.1"
│       ├── Span: "tool.Bash" (with input/output)
│       ├── Span: "tool.Write"
│       └── Span: "thinking.2"
├── Span: "memory.observe" (async)
│   ├── Generation: "extraction.haiku"
│   └── Span: "mem0.store"
└── Span: "slack.reply"
```

**Configuration:**

| Variable | Default | Description |
|----------|---------|-------------|
| `LANGFUSE_FLUSH_INTERVAL_MS` | `500` | Micro-batch flush interval (ms) |
| `LANGFUSE_TRACE_DETAIL` | `full` | `full` (all spans), `basic` (trace + generation only), `off` (disabled) |
| `LANGFUSE_STREAM_ENABLED` | `true` | `true` = Popen streaming, `false` = subprocess.run batch |

### Sentinel Agent

Query your system interactively:

```bash
claude --agent sentinel
```

Ask questions like:
- "How many tokens did Mr. Robot use today?"
- "Show me the last 5 calls from Elliot"
- "Which agent is spending the most?"
- "Show me errors from the last hour"
- "What model did Tyrell use for the last task?"

### Langfuse MCP Tool

Available to all agents via MCP:
- `langfuse_list_traces` — List traces by agent
- `langfuse_get_trace` — Get trace details
- `langfuse_list_generations` — List LLM calls with tokens
- `langfuse_get_token_usage` — Aggregated usage summary
- `langfuse_get_errors` — Recent errors

---

## 📋 Team Process

See [MANIFEST.md](MANIFEST.md) for the full process definition.

| Phase | What happens | Who decides |
|-------|-------------|-------------|
| 1. **Discovery** | Problem is discussed, approaches proposed | Project lead decides |
| 2. **Documentation** | Decisions written on GitHub Pages | Project lead assigns who writes |
| 3. **Issues** | GitHub Issues created from documentation | Project lead assigns who creates |
| 4. **Development** | Agents code, test, open PRs | Agents execute autonomously |
| 5. **Code Review** | All agents review every PR | All agents participate |
| 6. **Validation** | Project lead tests as client | Project lead validates |

---

## ⚙️ Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `{AGENT}_BOT_TOKEN` | ✅ | Slack Bot OAuth token (`xoxb-...`) |
| `{AGENT}_APP_TOKEN` | ✅ | Slack App-level token for Socket Mode (`xapp-...`) |
| `{AGENT}_GITHUB_APP_ID` | | GitHub App ID for git operations |
| `{AGENT}_GITHUB_PEM_PATH` | | Path to GitHub App private key (`.pem`) |
| `{AGENT}_GITHUB_INSTALLATION_ID` | | GitHub App installation ID |
| `PROJECT_LEAD_NAME` | | Project lead's display name (default: "the project lead") |
| `PROJECT_LEAD_SLACK_ID` | | Project lead's Slack user ID for @mentions |
| `AGENT_WORKSPACE` | ✅ | Main repo directory (read-only reference for git commands) |
| `AGENT_WORKTREE_DIR` | ✅ | Directory where agent worktrees are created (separate from main repo) |
| `MSG_BUFFER_WINDOW` | | Seconds to buffer rapid-fire messages (default: `3.0`) |
| `MAX_BATCH_SIZE` | | Max messages per batch before immediate flush (default: `10`) |
| `CLAUDE_TIMEOUT_SMALL` | | Timeout for small prompts <8k tokens (default: `180`s) |
| `CLAUDE_TIMEOUT_MEDIUM` | | Timeout for medium prompts 8k-32k tokens (default: `300`s) |
| `CLAUDE_TIMEOUT_LARGE` | | Timeout for large prompts >32k tokens (default: `600`s) |
| `LANGFUSE_PUBLIC_KEY` | | Langfuse public key for tracing |
| `LANGFUSE_SECRET_KEY` | | Langfuse secret key for tracing |
| `LANGFUSE_HOST` | | Langfuse URL (default: `http://localhost:3000`) |
| `LANGFUSE_FLUSH_INTERVAL_MS` | | Micro-batch flush interval in ms (default: `500`) |
| `LANGFUSE_TRACE_DETAIL` | | Trace detail level: `full`, `basic`, or `off` (default: `full`) |
| `LANGFUSE_STREAM_ENABLED` | | Enable streaming mode for real-time spans (default: `true`) |
| `QDRANT_HOST` | | Qdrant host for Mem0 (default: `localhost`) |
| `QDRANT_PORT` | | Qdrant port (default: `6333`) |
| `OLLAMA_URL` | | Ollama URL for embeddings (default: `http://localhost:11434`) |
| `ANTHROPIC_API_KEY` | | Anthropic API key for Mem0 fact extraction (haiku) |
| `NOTION_API_KEY` | | Notion integration token |

Where `{AGENT}` is one of: `MR_ROBOT`, `ELLIOT`, `TYRELL`.

---

## 📄 License

MIT — use it, fork it, build your own team.

---

<div align="center">

**Built with** ❤️ **by humans orchestrating AI agents**

[![Claude Code](https://img.shields.io/badge/Powered_by-Claude_Code-CC785C?style=for-the-badge&logo=anthropic&logoColor=white)](https://docs.anthropic.com/en/docs/claude-code)
[![Foundation](https://img.shields.io/badge/Foundation-claude--code-181717?style=for-the-badge&logo=github&logoColor=white)](https://github.com/nelsonfrugeri-tech/claude-code)

</div>
