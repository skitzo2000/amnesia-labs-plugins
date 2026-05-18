---
name: brain-update
description: Check for and install Brain plugin updates from the server.
---

1. Read current version from this plugin's `plugin.json` (two directories up from this command file).

2. Check for updates: `a(action="get_plugin", data={"plugin": "brain"})`
   - If error "not published": report "No plugin updates available on server." Stop.
   - Compare server version against local version.

3. If versions match: "Brain plugin v{version} is up to date." Stop.

4. If update available, show:
   > Brain plugin update available: v{current} → v{latest}
   > Changes: {changelog}
   >
   > This will update SKILL.md, commands, hooks, and bin/ helpers in your local plugin directory.
   > Update now?

5. On approval: `a(action="get_plugin", data={"plugin": "brain", "include_files": true})`

6. Write each file from `file_contents` to the plugin directory.
   Plugin root is the directory containing `.claude-plugin/plugin.json` (two levels up from this command).
   Preserve directory structure (e.g., `skills/brain/SKILL.md` → `{root}/skills/brain/SKILL.md`).

7. **Restore execute bits.** Some files in the plugin are shebang scripts that the
   server stores as plain text. After writing, mark these executable so the
   user (and the hooks runner) can invoke them:
   - `chmod +x {root}/bin/*` — every helper under `bin/` is a CLI (vault-unlock,
     vault-drop, vault-resolve, vault-run, vault-put, vault-paste, vault-rotate,
     vault-list, vault-audit, etc.).
   - `chmod +x {root}/hooks/*.py` — Python hook scripts referenced from
     hooks.json (pre_bash_vault.py, post_bash_vault_redact.py).
   Skip on Windows (no exec bit semantics).

8. Report:
   > Brain plugin updated to v{latest}
   > Updated files: {list}
   >
   > Restart your Claude Code session for hook/command changes to take effect.
   > If this update added vault helpers, also run `vault-unlock` once to enroll WebAuthn and warm the data-key cache.
