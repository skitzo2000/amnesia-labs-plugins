---
name: brain-update
description: Refresh the Brain plugin against the public marketplace. Thin wrapper around Claude Code's built-in update mechanism.
---

The Brain plugin is distributed via the public marketplace `skitzo2000/amnesia-labs-plugins`. Claude Code **already auto-updates plugins in the background on session start** — this command is for forcing an immediate check.

Two ways to refresh:

1. **In Claude Code (this REPL):** run `/plugin marketplace update` to fetch the latest catalog, then `/plugin install brain@amnesia-labs` to reinstall if a newer version is available. Restart Claude Code so hooks and commands re-register.

2. **From a shell** (works without entering the REPL):
   ```
   claude plugin marketplace update
   ```

Tell the user which option they want and run the appropriate slash command, or print the shell command for them to copy.

Do **not** call `a(action="get_plugin", ...)` — that pulls from the Brain server's bundled store, which may diverge from the marketplace's current release.
