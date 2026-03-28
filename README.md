# bike-shop

Multi-agent team platform powered by [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) and Slack. AI coding agents collaborate in Slack channels under a project lead's orchestration, with persistent memory, full observability via Langfuse, and access to external tools.

## How It Works

```
Project Lead (Slack)
    │
    ├── @Mr. Robot    ─┐
    ├── @Elliot Alderson ├── bike-shop (Python) ── Claude Code CLI ── LLM
    └── @Tyrell Wellick ─┘         │                    │
                                   │               MCP Servers
                               Langfuse          (Notion, draw.io,
                            (observability)       Excalidraw, GitHub)
```

## Agents

Three equal coding agents. No personas, no hierarchy between them. The project lead orchestrates.

| Agent | Name | Slack Bot |
|-------|------|-----------|
| Mr. Robot | Coding agent | Separate Slack app |
| Elliot Alderson | Coding agent | Separate Slack app |
| Tyrell Wellick | Coding agent | Separate Slack app |

Each agent can invoke specialized sub-agents from `~/.claude/agents/` (architect, review-py, dev-py, sentinel, etc.) depending on the task.

## Key Features

- **Project lead orchestration** — Agents wait for instructions, ask before executing, tag the project lead for decisions
- **Agent-to-agent limit** — Max 5 interactions per thread, enforced in code (anti-loop)
- **JSON memory** — Auto-records messages, saves summaries every 10 messages via haiku
- **Session continuity** — Slack threads map to Claude sessions via `--resume` (24h TTL)
- **Model switching** — Sonnet by default, Opus on demand ("think deeply") or auto-escalation (`[DEEP_THINK]`)
- **Full observability** — Every LLM call traced to Langfuse: input, output, tokens, tools, thinking, errors
- **Workspace isolation** — Agents operate only within `AGENT_WORKSPACE` directory
- **GitHub App auth** — Each agent has its own GitHub identity (JWT tokens, auto-refresh)
- **PR code review** — Agents tag teammates for review when opening PRs
- **Non-substantive response suppression** — "No response requested" messages are silenced

## Architecture (SOLID)

```
src/bike_shop/
├── main.py                  # CLI entrypoint, process management
├── config.py                # AgentConfig, MODEL_MAP, env loading
├── agents.py                # Agent prompts and common rules
├── memory.py                # JSON-based memory (messages, decisions, summaries)
├── observability.py         # Langfuse tracer (traces, generations, spans)
├── github_auth.py           # GitHub App JWT auth
├── session.py               # Session tracking per Slack thread
├── model_switch.py          # Deep think triggers and escalation
├── handlers.py              # Backward-compatible wrapper
├── providers/
│   ├── __init__.py          # LLMProvider ABC (provider-agnostic)
│   └── claude.py            # ClaudeProvider (Claude CLI implementation)
└── slack/
    ├── context.py           # Thread context, mentions, user resolution
    └── handler.py           # SlackAgentHandler (orchestrates everything)
```

## Getting Started

### Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Python | 3.13+ | `brew install python@3.13` |
| uv | latest | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Claude Code CLI | latest | `npm install -g @anthropic-ai/claude-code` |
| Docker | latest | [Docker Desktop](https://www.docker.com/products/docker-desktop/) (for Langfuse) |

### 1. Clone and Install

```bash
git clone https://github.com/nelsonfrugeri-tech/bike-shop.git
cd bike-shop
uv tool install -e . --python 3.13
bike-shop --help
```

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env with your Slack tokens, GitHub App IDs, and Langfuse keys
```

### 3. Create Slack Apps

Each agent needs a separate Slack App. For each agent (Mr. Robot, Elliot Alderson, Tyrell Wellick):

1. [api.slack.com/apps](https://api.slack.com/apps) → Create New App → From scratch
2. Enable **Socket Mode** → generate App-Level Token (`xapp-...`)
3. **OAuth & Permissions** → Add scopes: `app_mentions:read`, `chat:write`, `channels:history`, `channels:read`, `groups:history`, `im:history`, `im:read`, `users:read`
4. Install to workspace → copy Bot Token (`xoxb-...`)
5. **Event Subscriptions** → Enable → subscribe to: `app_mention`, `message.channels`, `message.groups`, `message.im`
6. **App Home** → Enable Messages Tab

### 4. Create GitHub Apps (optional)

For each agent that needs GitHub access:

1. [github.com/settings/apps](https://github.com/settings/apps) → New GitHub App
2. Permissions: Contents (write), Issues (write), Pull requests (write), Pages (write), Metadata (read)
3. Generate private key → move to `~/.ssh/bike-shop-{agent}.pem`
4. Install on selected repositories

### 5. Start Langfuse (optional)

```bash
docker compose up -d
# Open http://localhost:3000, create account, create project, get API keys
# Add keys to .env: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY
```

### 6. Run

```bash
# Start a single agent
bike-shop agent:mr-robot

# Start all agents
bike-shop agent:all

# Check status
bike-shop --status

# Stop an agent
bike-shop --stop agent:mr-robot
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `{AGENT}_BOT_TOKEN` | Yes | Slack Bot OAuth token |
| `{AGENT}_APP_TOKEN` | Yes | Slack App-level token (Socket Mode) |
| `{AGENT}_GITHUB_APP_ID` | No | GitHub App ID |
| `{AGENT}_GITHUB_PEM_PATH` | No | Path to GitHub App private key |
| `{AGENT}_GITHUB_INSTALLATION_ID` | No | GitHub App installation ID |
| `PROJECT_LEAD_NAME` | No | Project lead's name (default: "the project lead") |
| `PROJECT_LEAD_SLACK_ID` | No | Project lead's Slack user ID (for @mentions) |
| `AGENT_WORKSPACE` | No | Directory agents operate in (default: $HOME) |
| `LANGFUSE_PUBLIC_KEY` | No | Langfuse public key |
| `LANGFUSE_SECRET_KEY` | No | Langfuse secret key |
| `LANGFUSE_HOST` | No | Langfuse host (default: http://localhost:3000) |
| `NOTION_API_KEY` | No | Notion integration token |

Where `{AGENT}` is one of: `MR_ROBOT`, `ELLIOT`, `TYRELL`.

## Observability

Every LLM call sends a full trace to Langfuse:

```
Trace: Mr. Robot/call
├── input: user message
├── output: agent response
├── tokens: input → output
├── model, duration, tags
│
└── Generation: claude-cli
    ├── Span: thinking-1 (chain of thought)
    ├── Span: tool/Bash (git checkout -b ...)
    ├── Span: tool/Write (src/schema.py)
    ├── Span: tool/Bash (pytest tests/)
    └── Span: error-1 (if any)
```

Use the `sentinel` agent (`claude --agent sentinel`) to query Langfuse interactively.

## Team Process

See [MANIFEST.md](MANIFEST.md) for the full team process. Summary:

1. **Discovery** — Project lead brings a problem, agents discuss and propose
2. **Documentation** — Write decisions on GitHub Pages
3. **Issues** — Create GitHub Issues from documentation
4. **Development** — Agents code, test, and open PRs
5. **Code Review** — All agents review every PR
6. **Validation** — Project lead tests as client

## License

MIT
