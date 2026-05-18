#!/usr/bin/env python3
"""PostToolUse Bash hook — leak-shape detector.

Claude Code hooks cannot rewrite tool output that the model has already
seen, so true redaction must happen inside ``vault-run`` itself (which
filters its own stdout/stderr in real-time before the model reads).

This hook is a *detection* tripwire: it scans the tool response for
shapes that look like leaked secrets — Fernet ciphertext, Bearer
tokens, dense alphanumeric runs — and surfaces an advisory so the user
notices when redaction failed, when somebody bypassed vault-run, or
when an old credential dump is being echoed.
"""

from __future__ import annotations

import json
import re
import sys


_LEAK_PATTERNS = [
    # Fernet ciphertext starts with `gAAAAA`.
    (re.compile(r"\bgAAAAA[A-Za-z0-9_\-]{60,}={0,2}\b"), "fernet ciphertext"),
    # `Authorization: Bearer ...`
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-+/=]{12,}"), "bearer token"),
    # Labelled value assignment.
    (re.compile(r"(?i)\b(secret|password|token|api[_\-]?key|client[_\-]?secret)"
                r"\s*[:=]\s*\S{16,}"), "labelled secret"),
]


def _has_mixed_case_run(text: str) -> str | None:
    """Find a 32+ char alphanumeric run with mixed-case (excludes git
    hashes and other lowercase-only hex). UUIDs are already excluded
    by the dash structure.
    """
    for m in re.finditer(r"\b[A-Za-z0-9]{32,}\b", text):
        s = m.group(0)
        if any(c.isupper() for c in s) and any(c.islower() for c in s):
            return s
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    response = payload.get("tool_response") or {}
    text_parts: list[str] = []
    for key in ("stdout", "stderr", "output"):
        v = response.get(key)
        if isinstance(v, str):
            text_parts.append(v)
    text = "\n".join(text_parts)
    if not text:
        return 0

    hits: list[tuple[str, str]] = []
    for pat, label in _LEAK_PATTERNS:
        m = pat.search(text)
        if m:
            sample = m.group(0)
            preview = sample[:12] + "…" if len(sample) > 12 else sample
            hits.append((label, preview))

    mixed = _has_mixed_case_run(text)
    if mixed:
        preview = mixed[:12] + "…" if len(mixed) > 12 else mixed
        hits.append(("mixed-case dense run", preview))

    if not hits:
        return 0

    summary = "; ".join(f"{label}={preview}" for label, preview in hits)
    json.dump({
        "hookSpecificOutput": {
            "additionalContext": (
                f"vault advisory: tool output contains shapes that look like "
                f"leaked credentials ({summary}). If you used vault-run, "
                "this should already be redacted — investigate why it isn't. "
                "If this is intentional non-secret data (e.g. a UUID listing), "
                "ignore. Never paste these values into prompts or commits."
            )
        }
    }, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
