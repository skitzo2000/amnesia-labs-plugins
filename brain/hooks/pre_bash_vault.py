#!/usr/bin/env python3
"""PreToolUse Bash hook — vault placeholder safety net.

Reads the standard Claude Code hook input from stdin (JSON) and
emits a structured response on stdout. Three jobs:

  1. If the command contains a literal ``<<vault:...>>`` placeholder,
     block the tool call and tell the AI to use ``vault-run`` instead.
     Letting the placeholder reach the shell would leak the literal
     marker into argv and not resolve to anything anyway.

  2. If the command pipes/echoes a vault env-var verbosely (``set -x``,
     ``curl -v``, ``echo $X``), surface an advisory so the AI knows
     redaction is the only safety net for that path.

  3. Always allow ``vault-run`` and ``vault-unlock`` invocations —
     those are the safe path.

Output format follows the Claude Code hook contract: a JSON object on
stdout with optional ``decision``/``reason``/``additionalContext``.
"""

from __future__ import annotations

import json
import re
import sys


_PLACEHOLDER_RE = re.compile(r"<<vault:[^>]+>>")
_VERBOSE_PATTERNS = [
    re.compile(r"\bcurl\b[^|;&\n]*\s-v\b"),
    re.compile(r"\bset\s+-x\b"),
    re.compile(r"\becho\s+\$\w+"),
    re.compile(r"\bprintenv\b"),
    re.compile(r"\benv\b\s*$"),
]


def _emit(decision: str | None = None, *,
          reason: str | None = None,
          context: str | None = None) -> None:
    out: dict = {}
    if decision:
        out["decision"] = decision
    if reason:
        out["reason"] = reason
    if context:
        out.setdefault("hookSpecificOutput", {})["additionalContext"] = context
    if out:
        json.dump(out, sys.stdout)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") or ""
    if not isinstance(command, str) or not command:
        return 0

    # 1. Block raw placeholder usage — must go through vault-run.
    if _PLACEHOLDER_RE.search(command):
        _emit(
            decision="block",
            reason=(
                "vault: <<vault:...>> placeholders are not resolved by the "
                "shell. Use the safe wrapper:\n\n"
                "  vault-run <episode_id> [<id>...] -- <command using $ENV_VAR>\n\n"
                "Example:\n"
                "  vault-run secret:github:token -- "
                "curl -H \"Authorization: Bearer $GH_TOKEN\" https://api.github.com/user\n\n"
                "Run `vault-unlock` once per ~5 minutes to refresh the loa2 JWT."
            ),
        )
        return 0

    # 2. Advisory when the command will likely echo env vars verbosely.
    for pat in _VERBOSE_PATTERNS:
        if pat.search(command):
            _emit(context=(
                "vault advisory: this command may echo env-var values to "
                "stdout/stderr (matched: " + pat.pattern + "). vault-run "
                "redacts known values, but flags like `curl -v`, `set -x`, "
                "and `printenv` are dangerous if you're injecting secrets — "
                "prefer the non-verbose form, or wrap explicitly with "
                "vault-run."
            ))
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
