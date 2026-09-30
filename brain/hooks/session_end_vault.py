#!/usr/bin/env python3
"""SessionEnd hook — close this Claude session's vault read grant.

``vault-unlock`` spends one fresh MFA on a grant bound to this Claude
Code session, so ``vault-run`` keeps working for the rest of it. This
closes that grant when the session ends. Closing needs only a valid
token, not a fresh MFA, so the cached (or refreshed) one is enough.

Silent and best-effort: no cached token, no session id, an older Brain
or a network failure all mean there is nothing to close or nothing we
can do — the grant also dies with the Keycloak session and on a Brain
restart. Never blocks the session from ending.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    session = str(payload.get("session_id") or "").strip()
    if not session:
        return 0
    # The hook input is authoritative for which session is ending.
    os.environ["BRAIN_CLAUDE_SESSION"] = session

    try:
        from _vault_common import (
            close_session_grant, discover_brain_url, session_jwt, vault_namespace,
        )
        jwt = session_jwt(discover_brain_url())
        if jwt:
            close_session_grant(vault_namespace(), jwt)
    except (Exception, SystemExit):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
