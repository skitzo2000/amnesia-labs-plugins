# Amnesia Labs — Claude Code Plugins

Public marketplace for Amnesia Labs Claude Code plugins.

## Install

```
/plugin marketplace add skitzo2000/amnesia-labs-plugins
/plugin install brain@amnesia-labs
```

## Plugins

### brain

Persistent memory for AI agents. 4-store architecture (sensory / episodic / semantic / chronological), intelligent routing, knowledge-driven research loop, and a server-encrypted vault for secrets the AI can use without ever seeing the value.

The plugin auto-wires an MCP server at `${BRAIN_URL:-http://localhost:8002}/mcp/`. Set `BRAIN_URL` in your shell before launching Claude Code to point at your Brain deployment.
