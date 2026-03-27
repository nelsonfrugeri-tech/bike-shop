#!/bin/bash
cd /Users/nelson.frugeri

MODEL="${1:-sonnet}"
AGENT_FILE="tyrell-wellick-${MODEL}.md"

if [ ! -f ~/.claude/agents/"$AGENT_FILE" ]; then
  echo "Error: agent file ~/.claude/agents/$AGENT_FILE not found"
  echo "Usage: $0 [opus|sonnet]"
  exit 1
fi

PROMPT=$(cat ~/.claude/agents/"$AGENT_FILE" | sed '1,/^---$/{ /^---$/,/^---$/d }')

exec claude \
  --dangerously-skip-permissions \
  --model "claude-${MODEL}-4-20250514" \
  --append-system-prompt "$PROMPT" \
  -p "Start: use ToolSearch com query '+tyrell_wellick' para carregar as MCP tools, depois liste canais e entre no loop de polling do Slack."
