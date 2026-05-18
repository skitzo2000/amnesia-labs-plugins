#!/usr/bin/env python3
"""SessionStart hook — surface Brain plugin updates to the AI/user.

Reads the local plugin.json version, calls Brain's public version
endpoint, and emits a Claude Code hookSpecificOutput with
additionalContext when an update is available. The AI relays the
prompt to the user.

Fails silently on any error (Brain unreachable, plugin.json missing,
malformed response) — the hook must never block session start.

Stdout JSON contract (consumed by Claude Code):

    {
      "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": "<text shown to the AI>"
      }
    }

Inputs (env):
    BRAIN_URL         — Brain API base, e.g. http://localhost:8002
    CLAUDE_PLUGIN_ROOT — set by Claude Code to the plugin root dir.
                         Falls back to the path resolved from this file.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


_TIMEOUT_S = 3
_DEFAULT_BRAIN_URL = "http://localhost:8002"


def _emit(context: str) -> None:
    """Print the standard Claude Code SessionStart additionalContext payload."""
    json.dump({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }, sys.stdout)


def _plugin_root() -> Path:
    """Resolve the plugin root dir from $CLAUDE_PLUGIN_ROOT or this file's path."""
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root:
        return Path(root)
    return Path(__file__).resolve().parent.parent


def _local_version() -> str | None:
    """Read version from this plugin's plugin.json, or None if missing."""
    manifest = _plugin_root() / ".claude-plugin" / "plugin.json"
    if not manifest.exists():
        return None
    try:
        return json.loads(manifest.read_text())["version"]
    except Exception:
        return None


def _brain_url() -> str:
    """Resolve the Brain URL the same way _vault_common does:
    $BRAIN_URL env wins, then the plugin's .mcp.json (server-resolved),
    then a localhost default."""
    explicit = os.environ.get("BRAIN_URL")
    if explicit:
        return explicit.rstrip("/").removesuffix("/mcp")
    try:
        mcp = json.loads((_plugin_root() / ".mcp.json").read_text())
        url = mcp.get("mcpServers", {}).get("b", {}).get("url", "")
        if url:
            return url.rstrip("/").removesuffix("/mcp")
    except Exception:
        pass
    return _DEFAULT_BRAIN_URL


def _remote_manifest() -> dict | None:
    """Fetch the public version manifest. Returns None on any failure."""
    base = _brain_url()
    url = f"{base}/api/v1/api/plugin/latest/version"
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None


def main() -> int:
    local = _local_version()
    remote = _remote_manifest()
    if not local or not remote:
        return 0  # silent no-op
    latest = remote.get("version")
    if not latest or latest == local:
        return 0  # already current, no message
    changelog = (remote.get("changelog") or "").strip()
    msg_lines = [
        f"Brain plugin update available: v{local} → v{latest}.",
        "Run `/brain-update` to install.",
    ]
    if changelog:
        # Trim long changelogs to keep the SessionStart context tight.
        if len(changelog) > 400:
            changelog = changelog[:397] + "..."
        msg_lines.append("")
        msg_lines.append(f"Changes: {changelog}")
    _emit("\n".join(msg_lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
