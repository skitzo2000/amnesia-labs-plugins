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
import re
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
        # The bundled .mcp.json stores the url parameterised as
        # `${BRAIN_URL:-<default>}/mcp/`. Claude Code expands that when it
        # loads the server, but this hook reads the file off disk and sees
        # the literal placeholder — so expand it here too, or every fresh
        # install silently falls through to the localhost default and never
        # learns an update exists.
        url = _expand_shell_default(url)
        if url:
            return url.rstrip("/").removesuffix("/mcp")
    except Exception:
        pass
    return _DEFAULT_BRAIN_URL


def _expand_shell_default(value: str) -> str:
    """Resolve `${VAR:-fallback}` / `${VAR}` occurrences against the env."""
    def _sub(match: "re.Match[str]") -> str:
        name, _, fallback = match.group(1).partition(":-")
        return os.environ.get(name) or fallback

    return re.sub(r"\$\{([^}]*)\}", _sub, value)


def _remote_manifest() -> dict | None:
    """Fetch the public version manifest. Returns None on any failure."""
    base = _brain_url()
    url = f"{base}/api/v1/api/plugin/latest/version"
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None


def _parse_version(v: str) -> tuple[int, ...] | None:
    """Parse MAJOR.MINOR.PATCH into a comparable tuple, or None if malformed."""
    parts = v.strip().split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def _is_newer(candidate: str, current: str) -> bool:
    """True only when *candidate* is a strictly higher semver than *current*.

    A plain `!=` would announce an "update" whenever the two differ — including
    when the local plugin is AHEAD of the store, which happens routinely between
    a version bump and the server republishing it. Suggesting a downgrade is the
    same bug already fixed on the publish side (#178); the check that reads the
    published version has to agree with it.
    """
    a, b = _parse_version(candidate), _parse_version(current)
    if a is None or b is None:
        return False  # fail quiet — never nag on a version we cannot compare
    return a > b


def main() -> int:
    local = _local_version()
    remote = _remote_manifest()
    if not local or not remote:
        return 0  # silent no-op
    latest = remote.get("version")
    if not latest or not _is_newer(latest, local):
        return 0  # already current (or ahead), no message
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
