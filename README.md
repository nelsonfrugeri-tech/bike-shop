# bike-shop

Multi-agent team powered by [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) and Slack. Three AI personas collaborate in Slack channels with persistent memory, session continuity, and access to external tools (Notion, Trello, GitHub, draw.io, Excalidraw).

## Agents

| Agent | Role | Persona |
|-------|------|---------|
| **Mr. Robot** | Software Architect | Direct, blunt, questions every design decision. Zero tolerance for over-engineering. |
| **Elliot Alderson** | Developer | Obsesses over clean code and security. Prefers concrete code over abstract discussions. |
| **Tyrell Wellick** | Technical PM | Organized, strategic, obsessed with execution. Bullet points, priorities, deadlines. |

## Architecture

```
Slack (Socket Mode) → bike-shop Python app → Claude Code CLI (subprocess)
                                                    ↓
                                              MCP Servers (Notion, Trello, draw.io, Excalidraw)
                                                    ↓
                                              GitHub (via gh CLI + GitHub App tokens)
```

**Key features:**
- **Conversation memory** — each Slack thread maps to a Claude session via `--resume`, preserving context across messages (24h TTL)
- **Persistent knowledge** — each agent has a `MEMORY.md` file loaded via `--append-system-prompt-file`, where it stores decisions, patterns, and learnings
- **Resilience** — agents save progress before long operations and recover from tool failures without losing context
- **Async processing** — messages are handled in background threads so the bot stays responsive
- **Team mentions** — agents use proper Slack `<@USER_ID>` mentions to notify each other
- **No timeout** — Claude CLI runs without time limit, agents work until done

## Getting Started (Step-by-Step)

### 1. Prerequisites

Install these before anything else:

| Tool | Version | Install |
|------|---------|---------|
| **Python** | 3.11+ | `brew install python@3.12` or [pyenv](https://github.com/pyenv/pyenv) |
| **uv** | latest | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **Claude Code CLI** | latest | `npm install -g @anthropic-ai/claude-code` then `claude auth login` |
| **gh CLI** | latest | `brew install gh` then `gh auth login` (needed for GitHub App integration) |

### 2. Create Slack Apps (one per agent)

Each agent is a **separate Slack App** with its own identity, avatar, and tokens. This is what allows them to appear as different "people" in Slack and mention each other.

You need to create **3 apps**. Repeat the steps below for each:

| App Name | Env Prefix | Description |
|----------|------------|-------------|
| `Mr. Robot` | `MR_ROBOT_` | Software Architect persona |
| `Elliot Alderson` | `ELLIOT_` | Developer persona |
| `Tyrell Wellick` | `TYRELL_` | Technical PM persona |

#### 2.1 Create the App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. **App Name**: use the agent name exactly (e.g., `Elliot Alderson`)
3. **Workspace**: select your Slack workspace
4. Go to **Basic Information** → **Display Information**:
   - Set the app name, description, and optionally an avatar icon
   - This is what users see in Slack — give each agent a distinct avatar

#### 2.2 Enable Socket Mode

Socket Mode allows the app to receive events via WebSocket (no public URL needed).

1. Go to **Socket Mode** (left sidebar) → Toggle **Enable Socket Mode** ON
2. You'll be prompted to create an **App-Level Token**:
   - Token name: `socket` (or anything)
   - Scope: `connections:write`
   - Click **Generate**
3. Copy the `xapp-1-...` token → this is your `{AGENT}_APP_TOKEN`

#### 2.3 Configure OAuth & Permissions

1. Go to **OAuth & Permissions** (left sidebar)
2. Scroll to **Scopes** → **Bot Token Scopes** → Add these:

| Scope | Why |
|-------|-----|
| `app_mentions:read` | Receive @mentions in channels |
| `chat:write` | Send messages and replies |
| `channels:history` | Read channel message history (for context) |
| `channels:read` | List channels |
| `groups:history` | Read private channel history |
| `im:history` | Read DM history |
| `im:read` | Read DM metadata |
| `im:write` | Open and send DMs |
| `users:read` | Resolve user IDs to display names |

3. Scroll up → **Install App to Workspace** → **Allow**
4. Copy the `xoxb-...` **Bot User OAuth Token** → this is your `{AGENT}_BOT_TOKEN`

#### 2.4 Subscribe to Events

1. Go to **Event Subscriptions** (left sidebar) → Toggle **Enable Events** ON
2. Under **Subscribe to bot events**, add:
   - `app_mention` — triggers when someone @mentions the bot
   - `message.channels` — triggers on channel messages (for bot-to-bot interaction)
   - `message.groups` — same for private channels
   - `message.im` — triggers on direct messages

3. Click **Save Changes** at the bottom

#### 2.5 Enable App Home (for DMs)

1. Go to **App Home** (left sidebar)
2. Under **Show Tabs**, enable **Messages Tab**
3. Check **Allow users to send Slash commands and messages from the messages tab**

This lets users DM the bot directly.

#### 2.6 Invite to Channels

After starting the agent, invite it to channels where you want it active:

```
/invite @Elliot Alderson
/invite @Mr. Robot
/invite @Tyrell Wellick
```

The agents only respond when **@mentioned** in channels. In DMs they respond to every message.

### 3. Create GitHub Apps (optional, for GitHub integration)

If you want agents to interact with GitHub (create issues, PRs, comment on code), each agent gets its own GitHub App identity. This way commits and comments show the agent's name.

Only create GitHub Apps for agents that need git access (e.g., Mr. Robot and Elliot — Tyrell as PM may not need one).

#### 3.1 Create the App

1. Go to [github.com/settings/apps](https://github.com/settings/apps) → **New GitHub App**
2. Fill in:
   - **GitHub App name**: `bike-shop-elliot` (must be globally unique on GitHub)
   - **Homepage URL**: your repo URL or `https://github.com/your-org`
   - **Webhook**: uncheck **Active** (we don't need webhooks, agents use polling)
3. **Permissions** → **Repository permissions**:
   - `Contents`: Read & write (read code, create branches)
   - `Issues`: Read & write (create/comment issues)
   - `Pull requests`: Read & write (create/review PRs)
   - `Metadata`: Read-only (always required)
4. **Where can this app be installed?** → **Only on this account**
5. Click **Create GitHub App**
6. Note the **App ID** (shown at the top of the app settings page) → this is `{AGENT}_GITHUB_APP_ID`

#### 3.2 Generate Private Key

1. On the app settings page, scroll to **Private keys**
2. Click **Generate a private key** — a `.pem` file will be downloaded
3. Move it to a secure location:

```bash
mv ~/Downloads/your-app.2026-03-26.private-key.pem ~/.ssh/bike-shop-elliot-alderson.pem
chmod 600 ~/.ssh/bike-shop-elliot-alderson.pem
```

4. Set the path in `.env`: `ELLIOT_GITHUB_PEM_PATH=~/.ssh/bike-shop-elliot-alderson.pem`

#### 3.3 Install the App

1. Go to your app settings → **Install App** (left sidebar)
2. Click **Install** → select your organization or personal account
3. Choose **Only select repositories** → pick the repos the agent should access
4. Click **Install**
5. After installing, the URL will contain the **Installation ID** (the number at the end of the URL: `https://github.com/settings/installations/119203607`) → this is `{AGENT}_GITHUB_INSTALLATION_ID`

#### 3.4 How it works at runtime

The app generates short-lived tokens (10min) using JWT + the private key. The token is passed as `GH_TOKEN` env var to the Claude CLI subprocess, so the agent uses `gh` CLI commands authenticated as itself. Token refresh is automatic.

### 4. Clone and Install

```bash
git clone https://github.com/nelsonfrugeri-tech/bike-shop.git
cd bike-shop

# Install as editable package via uv
uv tool install -e .

# Verify installation
bike-shop --help
```

### 5. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your actual values:

```bash
# Slack tokens (from step 2)
MR_ROBOT_BOT_TOKEN=xoxb-your-actual-token
MR_ROBOT_APP_TOKEN=xapp-your-actual-token
# ... same for ELLIOT_ and TYRELL_

# GitHub App (from step 3, optional)
MR_ROBOT_GITHUB_APP_ID=your-app-id
MR_ROBOT_GITHUB_PEM_PATH=~/.ssh/bike-shop-mr-robot.pem
MR_ROBOT_GITHUB_INSTALLATION_ID=your-installation-id

# MCP service keys (optional)
NOTION_API_KEY=your-notion-integration-token
TRELLO_API_KEY=your-trello-api-key
TRELLO_TOKEN=your-trello-token
```

### 6. Install MCP Servers

The agents use MCP (Model Context Protocol) to interact with external tools. Install the servers you need:

```bash
# Required for Notion integration
uv tool install mcp-notion

# Required for Trello integration
uv tool install mcp-trello

# Required for architecture diagrams
uv tool install drawio-mcp
uv tool install excalidraw-mcp
```

The MCP config (`mcp.json`) uses `${VAR}` placeholders that are resolved from your `.env` at runtime.

### 7. Create Memory Directories

Each agent has persistent memory stored in markdown files:

```bash
mkdir -p ~/.claude/workspace/bike-shop/memory/{elliot,mr-robot,tyrell}

# Create initial memory files
echo "# elliot - Memory" > ~/.claude/workspace/bike-shop/memory/elliot/MEMORY.md
echo "# mr-robot - Memory" > ~/.claude/workspace/bike-shop/memory/mr-robot/MEMORY.md
echo "# tyrell - Memory" > ~/.claude/workspace/bike-shop/memory/tyrell/MEMORY.md
```

These files are loaded into every Claude CLI call and the agents append their learnings to them.

### 8. Run

```bash
# Start a single agent
bike-shop agent:elliot

# Start all agents at once
bike-shop agent:all

# Check who's running
bike-shop --status

# Stop an agent
bike-shop --stop agent:elliot
```

### 9. Test

1. Go to your Slack workspace
2. Invite one of the bots to a channel (e.g., `/invite @Elliot Alderson`)
3. Mention the bot: `@Elliot Alderson hello, can you hear me?`
4. The bot should respond in the thread
5. Send another message in the **same thread** — the bot remembers context (via `--resume`)
6. Ask the bot to save something to memory — check `~/.claude/workspace/bike-shop/memory/elliot/MEMORY.md`

## How It Works

### Message Flow

1. **Slack event** arrives via Socket Mode (mention or DM)
2. **Handler** spawns a background thread (non-blocking)
3. Thread context is fetched from Slack API
4. Session ID is looked up for the Slack thread (`/tmp/bike-shop/sessions-{agent}.json`)
5. **Claude CLI** is called with: system prompt + memory instruction + resilience rules + team mentions + conversation context
   - If resuming: `--resume <session_id>` is added
   - Memory file: `--append-system-prompt-file MEMORY.md`
   - MCP tools: `--mcp-config` with resolved env vars
   - Output format: `--output-format stream-json` (to parse session_id)
6. Session ID from Claude's response is stored for future thread messages
7. Response is sent back to Slack thread

### Memory System

**Layer 1: Conversation Memory** — `--resume` maintains Claude session state per Slack thread (24h TTL)

**Layer 2: Persistent Knowledge** — `MEMORY.md` files survive across sessions. Agents are instructed to:
- Save decisions, patterns, and learnings
- Write checkpoints before long operations
- Read memory when resuming work
- Format entries with timestamps: `## [YYYY-MM-DD HH:MM] Topic`

### Resilience

Agents are instructed to handle tool failures gracefully:
- Save progress to `MEMORY.md` before long operations
- If a tool times out, note the failure and continue with the next step
- Always read `MEMORY.md` when resuming to recover context
- No subprocess timeout — Claude works until done

## Project Structure

```
bike-shop/
├── src/bike_shop/
│   ├── main.py           # CLI entrypoint, process management, PID tracking
│   ├── config.py          # Agent config, env loading, team mention resolution
│   ├── handlers.py        # Slack events, Claude CLI calls, session tracking, memory
│   ├── agents.py          # Persona definitions (system prompts)
│   └── tools/             # Tool integrations (Slack, Notion placeholders)
├── bin/                    # Legacy shell scripts (deprecated, kept for reference)
├── mcp.json               # MCP server config (${VAR} placeholders resolved at runtime)
├── .env.example           # Environment variable template
├── .gitignore
├── pyproject.toml
└── README.md
```

## Environment Variables

See [`.env.example`](.env.example) for the full list. All secrets are loaded from `.env` at runtime — nothing is hardcoded in source files.

| Variable | Required | Description |
|----------|----------|-------------|
| `{AGENT}_BOT_TOKEN` | Yes | Slack Bot OAuth token (`xoxb-...`) |
| `{AGENT}_APP_TOKEN` | Yes | Slack App-level token for Socket Mode (`xapp-...`) |
| `{AGENT}_GITHUB_APP_ID` | No | GitHub App ID for git operations |
| `{AGENT}_GITHUB_PEM_PATH` | No | Path to GitHub App private key (`.pem`) |
| `{AGENT}_GITHUB_INSTALLATION_ID` | No | GitHub App installation ID |
| `NOTION_API_KEY` | No | Notion integration token |
| `TRELLO_API_KEY` | No | Trello API key |
| `TRELLO_TOKEN` | No | Trello user token |

Where `{AGENT}` is one of: `MR_ROBOT`, `ELLIOT`, `TYRELL`.

## License

Private project.
