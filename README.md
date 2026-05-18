# Amnesia Labs — Claude Code Plugins

Public marketplace for Amnesia Labs Claude Code plugins.

Full guide: **https://docs.amnesia-labs.com**

## Install

```bash
export BRAIN_URL=https://brain.amnesia-labs.com
```

In Claude Code:

```
/plugin marketplace add skitzo2000/amnesia-labs-plugins
/plugin install brain@amnesia-labs
```

Restart Claude Code. A browser opens for OIDC login against `auth.amnesia-labs.com` — that's it.

## Plugins

### brain

Persistent memory for AI agents. 4-store architecture (sensory / episodic / semantic / chronological), intelligent routing, knowledge-driven research loop, and a server-encrypted vault for secrets the AI can use without ever seeing the value.

The plugin auto-wires an MCP server at `${BRAIN_URL:-http://localhost:8002}/mcp/`. Point `BRAIN_URL` at any Brain deployment — production is `https://brain.amnesia-labs.com`.
