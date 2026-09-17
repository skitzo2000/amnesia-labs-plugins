# Amnesia Labs Plugins

Private alpha. Access by invite only — contact Amnesia Labs.

## Install

```
/plugin marketplace add skitzo2000/amnesia-labs-plugins
/plugin install brain@amnesia-labs
```

Defaults to `https://brain.amnesia-labs.com`. Override with `export BRAIN_URL=...` if you're running a self-hosted Brain.

## Authentication

Nothing to configure. After installing, run `/mcp`, pick `plugin:brain:b`, and authenticate in the browser. The plugin's session hooks reuse that same login, so session context loads on start and sessions save automatically.

If the start-of-session message says the hooks couldn't use your login, run `/mcp` and re-authenticate the brain server.
