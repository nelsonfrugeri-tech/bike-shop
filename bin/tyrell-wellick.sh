#!/bin/bash
cd /Users/nelson.frugeri

PROMPT=$(cat ~/.claude/agents/tyrell-wellick.md | sed '1,/^---$/{ /^---$/,/^---$/d }')

exec claude \
  --dangerously-skip-permissions \
  --append-system-prompt "$PROMPT" \
  -p "Start: use ToolSearch com query '+tyrell_wellick' para carregar as MCP tools, depois liste canais e entre no loop de polling do Slack."
