"""Shared helpers for vault-* CLI scripts.

All cryptography lives server-side now. The plugin's job is:

  1. Discover the Brain URL (from this plugin's ``.mcp.json`` or
     ``$BRAIN_URL``) and the Keycloak OAuth endpoints from Brain's
     RFC 9728 / 8414 discovery docs.
  2. Mint a loa3 (MFA-fresh) JWT via authorization-code + PKCE on a
     loopback redirect, using the shared public ``brain-plugin-client``
     Brain already advertises.
  3. Cache the JWT in libsecret with its expiry.
  4. Call Brain REST endpoints (plaintext value in/out over TLS).

Brain encrypts/decrypts values with each user's ``vault_dk`` Keycloak
attribute on the server side — the plugin never touches a key.

Configuration is zero env vars by default — discovery handles it. Only
fallback: ``BRAIN_URL`` if ``.mcp.json`` isn't readable.

  Libsecret items        Schema attributes
  ---------------        --------------------------------------------
  jwt_cache              service=brain-vault, kind=jwt,
                         account=<brain_url> (attrs exp=<ts>, email=<x>)
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class VaultCliError(RuntimeError):
    """Anything the CLI surfaces to the user via stderr + exit code.

    *status* and *payload* carry the HTTP status and decoded JSON body when
    the failure came from Brain, so callers can diagnose a rejection rather
    than pattern-matching the rendered message.
    """

    def __init__(self, message: str, *, status: Optional[int] = None,
                 payload: Optional[dict] = None) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def explain_denial(err: VaultCliError) -> Optional[str]:
    """Turn a Brain auth rejection into an accurate, actionable sentence.

    Every 403 used to be reported as "vault is locked. run `vault-unlock`
    to refresh." — which describes LOCAL cache state and is wrong for every
    server-side rejection. When the real cause was a stale ``auth_time``,
    running vault-unlock changed nothing (it saw an unexpired token and
    declined), so the advice sent people round a loop that could not
    terminate (issue #192). Returns None when this is not an auth denial.
    """
    payload = err.payload or {}
    if err.status not in (401, 403) and payload.get("error") != "step_up_required":
        return None

    if payload.get("error") != "step_up_required":
        return "brain rejected the token (not a step-up denial): " + str(err)

    reason = payload.get("reason")
    current = payload.get("current_acr")
    required = payload.get("required_acr", "loa3")

    if reason == "stale_auth_time":
        age = payload.get("auth_age_seconds")
        window = payload.get("max_age_seconds")
        return (
            f"MFA is too old: last completed {age}s ago, server accepts "
            f"{window}s. Run `vault-unlock` to re-authenticate."
        )
    if reason == "missing_auth_time":
        return (
            "token carries acr but no auth_time claim, so the server cannot "
            "confirm freshness. Run `vault-unlock --force`."
        )
    if not current:
        return (
            f"token carries no acr claim at all, so it can never satisfy "
            f"{required}. This is the wrong token for vault access — it is "
            "not the one `vault-unlock` caches. Run `vault-unlock`."
        )
    return (
        f"token is {current}, vault values require {required}. "
        "Run `vault-unlock` to complete MFA."
    )


# ---------------------------------------------------------------------------
# Discovery — figure out Brain URL, Keycloak issuer, OAuth endpoints
# ---------------------------------------------------------------------------

# Shared public OAuth client registered in each Brain-backed Keycloak realm.
# PKCE-friendly, no client_secret. Piggybacks on the same client Claude Code
# uses for MCP OAuth.
SHARED_CLIENT_ID = "brain-plugin-client"


def _plugin_dir() -> Optional[Path]:
    """Return the plugin root if this file lives under a Claude Code plugin."""
    p = Path(__file__).resolve().parent.parent
    if (p / ".claude-plugin" / "plugin.json").exists():
        return p
    return None


_MCP_TEMPLATE_RE = __import__("re").compile(r"\$\{BRAIN_URL(?::-([^}]*))?\}")


def _resolve_mcp_template(url: str) -> str:
    """Resolve ${BRAIN_URL:-default}/mcp template substitutions in
    .mcp.json (handles direct-rsync installs where server-side
    substitution didn't run)."""
    def _sub(m):
        fallback = m.group(1) or ""
        return os.environ.get("BRAIN_URL") or fallback
    return _MCP_TEMPLATE_RE.sub(_sub, url)


def discover_brain_url() -> str:
    """Resolve the Brain MCP base URL. $BRAIN_URL wins, else plugin
    .mcp.json. Returns a base URL with no trailing /mcp.
    """
    explicit = os.environ.get("BRAIN_URL")
    if explicit:
        return explicit.rstrip("/").removesuffix("/mcp")
    root = _plugin_dir()
    if root is not None:
        try:
            mcp = json.loads((root / ".mcp.json").read_text())
            url = mcp.get("mcpServers", {}).get("b", {}).get("url", "")
            if url:
                url = _resolve_mcp_template(url)
                return url.rstrip("/").removesuffix("/mcp")
        except Exception:
            pass
    die("cannot discover Brain URL: set BRAIN_URL or install plugin so .mcp.json is readable")


_oauth_meta_cache: dict[str, dict] = {}


def discover_oauth_metadata(brain_url: str) -> dict[str, str]:
    """RFC 9728 → RFC 8414. Returns {issuer, authorization_endpoint,
    token_endpoint, userinfo_endpoint}. Cached.
    """
    if brain_url in _oauth_meta_cache:
        return _oauth_meta_cache[brain_url]
    try:
        with urllib.request.urlopen(
            urllib.request.Request(
                f"{brain_url}/.well-known/oauth-protected-resource/mcp/",
                headers={"Accept": "application/json"},
            ),
            timeout=10,
        ) as resp:
            prm = json.loads(resp.read().decode())
    except Exception as e:
        die(f"OAuth protected-resource discovery failed: {e}")
    servers = prm.get("authorization_servers") or []
    if not servers:
        die("Brain's protected-resource doc has no authorization_servers")
    issuer = servers[0].rstrip("/")
    try:
        with urllib.request.urlopen(
            urllib.request.Request(
                f"{issuer}/.well-known/openid-configuration",
                headers={"Accept": "application/json"},
            ),
            timeout=10,
        ) as resp:
            oid = json.loads(resp.read().decode())
    except Exception as e:
        die(f"OpenID configuration discovery failed at {issuer}: {e}")
    meta = {
        "issuer": issuer,
        "authorization_endpoint": oid["authorization_endpoint"],
        "token_endpoint": oid["token_endpoint"],
        "userinfo_endpoint": oid.get("userinfo_endpoint", ""),
    }
    _oauth_meta_cache[brain_url] = meta
    return meta


def decode_jwt_claims(jwt: str) -> dict:
    """Decode the JWT payload (no signature check — we just authenticated
    against the issuer). Returns {} on any malformed input.
    """
    try:
        payload_b64 = jwt.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64).decode())
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def _decode_jwt_email(jwt: str) -> Optional[str]:
    """Return the JWT's email claim (or preferred_username)."""
    claims = decode_jwt_claims(jwt)
    return claims.get("email") or claims.get("preferred_username")


def jwt_auth_age(jwt: str) -> Optional[int]:
    """Seconds since the user last entered credentials, per ``auth_time``.

    ``None`` when the claim is absent — callers must treat that as "cannot
    prove freshness" and re-authenticate rather than assume the token is
    good. The server does the same (``_check_required_acr`` rejects an
    acr-bearing token with no auth_time).
    """
    raw = decode_jwt_claims(jwt).get("auth_time")
    try:
        auth_time = int(raw) if raw else 0
    except (TypeError, ValueError):
        return None
    if auth_time <= 0:
        return None
    return int(time.time()) - auth_time


# The loa3 freshness window is a SERVER policy (ACR_LOA3_MAX_AGE_SECONDS).
# This is only the fallback for a server too old to serve /vault/policy —
# it matches src/config.py's default. Deliberately not tuned by hand: a
# client guess that disagrees with the server is what produced the unlock
# loop in the first place (see fetch_loa3_window).
_LOA3_WINDOW_FALLBACK = 300

# Re-authenticating costs a browser round-trip, so leave a margin: a token
# with 3 seconds of window left is not worth handing to vault-run.
_LOA3_WINDOW_MARGIN = 15

_loa3_window_cache: dict[str, int] = {}


def fetch_loa3_window(jwt: Optional[str] = None) -> int:
    """Return the server's loa3 freshness window in seconds.

    ``vault-unlock`` previously called a cached JWT "unlocked" for as long
    as the token was unexpired (~15 min), while the server rejects a value
    read once ``auth_time`` exceeds this window. On a deployment with the
    window at 120s that left ten minutes in which vault-unlock said
    "already unlocked" and vault-run said "vault is locked" — and since
    vault-unlock refused to re-auth while a token was cached, no sequence
    of commands could recover (issue #192). Asking the server removes the
    disagreement instead of papering over it with a second guess.

    Falls back to the documented default when the endpoint is missing or
    unreachable — an older server is a reason to be conservative, not to
    fail the unlock.
    """
    url = discover_brain_url()
    if url in _loa3_window_cache:
        return _loa3_window_cache[url]

    window = _LOA3_WINDOW_FALLBACK
    if jwt:
        try:
            policy = brain_request("GET", "/api/v1/vault/policy", jwt=jwt)
            candidate = int(policy.get("loa3_max_age_seconds") or 0)
            if candidate > 0:
                window = candidate
        except Exception:
            pass
    _loa3_window_cache[url] = window
    return window


def _sanitize_email(s: str) -> str:
    """Mirror of src/auth/models.py:sanitize_email — lowercase, @ and . → -."""
    return s.strip().lower().replace("@", "-").replace(".", "-")


def vault_namespace() -> str:
    """Return the user's vault namespace, derived from cached JWT email.

    Order:
      1. ``BRAIN_VAULT_NAMESPACE`` env var (escape hatch)
      2. cached JWT's email → ``<sanitized-email>:vault``
      3. bare ``vault`` (admin fallback)
    """
    explicit = os.environ.get("BRAIN_VAULT_NAMESPACE")
    if explicit:
        return explicit
    brain_url = discover_brain_url()
    cached = _read_with_expiry(kind="jwt", account=brain_url, min_remaining=0)
    if cached:
        email = _decode_jwt_email(cached)
        if email:
            return f"{_sanitize_email(email)}:vault"
    return "vault"


# ---------------------------------------------------------------------------
# Libsecret wrappers (JWT cache only — no DK/cipher caching anymore)
# ---------------------------------------------------------------------------

_SCHEMA_SERVICE = "brain-vault"


def _secret_tool_lookup(**attrs: str) -> Optional[str]:
    args = ["secret-tool", "lookup"]
    for k, v in attrs.items():
        args.extend([k, v])
    try:
        out = subprocess.run(args, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.rstrip("\n") or None


def _secret_tool_store(label: str, value: str, **attrs: str) -> None:
    args = ["secret-tool", "store", "--label", label]
    for k, v in attrs.items():
        args.extend([k, v])
    try:
        proc = subprocess.run(args, input=value, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        die("secret-tool is not installed (libsecret on Linux). The plugin "
            "uses it to cache the loa3 JWT between commands. Install via your "
            "package manager (e.g. `nix-shell -p libsecret` or apt install "
            "libsecret-tools).")
    if proc.returncode != 0:
        die(f"secret-tool store failed: {proc.stderr.strip()}")


def _secret_tool_clear(**attrs: str) -> None:
    args = ["secret-tool", "clear"]
    for k, v in attrs.items():
        args.extend([k, v])
    subprocess.run(args, capture_output=True, text=True, check=False)


def _store_with_expiry(*, kind: str, account: str, value: str,
                       expires_at: int, label: str) -> None:
    # Single-item invariant: `exp` is part of the attribute set, so
    # secret-tool store never REPLACES an earlier token (different exp
    # = different item). Without this clear, items accumulate across
    # unlocks and reads match the OLDEST (expired) one — vault-run then
    # reports "no fresh loa3 JWT" while a valid token sits in the store.
    _secret_tool_clear(service=_SCHEMA_SERVICE, kind=kind, account=account)
    _secret_tool_store(
        label, value,
        service=_SCHEMA_SERVICE,
        kind=kind, account=account, exp=str(expires_at),
    )


def _read_with_expiry(*, kind: str, account: str, min_remaining: int = 30) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["secret-tool", "search", "--all",
             "service", _SCHEMA_SERVICE, "kind", kind, "account", account],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return None
    # `secret-tool search` writes each matched item's label + secret to
    # stdout and its attribute lines (`attribute.exp = …`) to stderr, in
    # the same item order on both streams. Installs that predate the
    # store-time clear accumulated one item PER UNLOCK, and taking the
    # first exp matched the oldest (expired) token — vault-run reported
    # "no fresh loa3 JWT" while a valid one sat in the store. Zip the
    # streams and take the freshest pair instead.
    secrets_in_order = [
        line.split("=", 1)[1].strip()
        for line in proc.stdout.splitlines()
        if line.strip().startswith("secret =")
    ]
    exps_in_order = []
    for line in proc.stderr.splitlines():
        line = line.strip()
        if line.startswith("attribute.exp ="):
            try:
                exps_in_order.append(int(line.split("=", 1)[1].strip()))
            except ValueError:
                exps_in_order.append(0)
    pairs = list(zip(secrets_in_order, exps_in_order))
    if not pairs:
        return None
    token, exp = max(pairs, key=lambda p: p[1])
    if not token or exp == 0 or time.time() >= (exp - min_remaining):
        return None
    return token


def store_jwt(account: str, access_token: str, expires_at: int) -> None:
    _store_with_expiry(
        kind="jwt", account=account, value=access_token,
        expires_at=expires_at,
        label=f"Brain vault JWT (exp:{expires_at})",
    )


def get_cached_jwt(account: str, *, max_auth_age: Optional[int] = None) -> Optional[str]:
    """Return the cached JWT, or None if it is not usable for a value read.

    Two independent expiries have to hold, and only the first used to be
    checked:

      * ``exp`` — the token's own lifetime (~15 min from Keycloak).
      * ``auth_time`` age vs *max_auth_age* — the server's loa3 freshness
        window (~300 s). A token can be minutes from expiry and already
        too stale for ``/vault/value/get``.

    Pass *max_auth_age* (from :func:`fetch_loa3_window`) wherever the
    answer feeds a value read, so "unlocked" means the same thing on both
    ends of the wire. Omit it for identity-only uses such as deriving the
    vault namespace.
    """
    token = _read_with_expiry(kind="jwt", account=account)
    if token is None or max_auth_age is None:
        return token
    age = jwt_auth_age(token)
    if age is None or age > max(0, max_auth_age - _LOA3_WINDOW_MARGIN):
        return None
    return token


def clear_cached_jwt(account: str) -> None:
    _secret_tool_clear(service=_SCHEMA_SERVICE, kind="jwt", account=account)


# ---------------------------------------------------------------------------
# Keycloak token mint (Authorization Code + PKCE on loopback) for loa3
# ---------------------------------------------------------------------------

import hashlib
import http.server as _http_server
import socketserver as _socketserver
import threading as _threading
import webbrowser as _webbrowser


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


class _CodeCapture:
    __slots__ = ("code", "state", "error", "error_description")
    def __init__(self) -> None:
        self.code: Optional[str] = None
        self.state: Optional[str] = None
        self.error: Optional[str] = None
        self.error_description: Optional[str] = None


def _build_code_handler(captured: _CodeCapture, expected_state: str,
                        shutdown_event: _threading.Event):
    class Handler(_http_server.BaseHTTPRequestHandler):
        def log_message(self, *a, **k):
            return
        def _ok(self, body: str):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            data = body.encode("utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404); self.end_headers(); return
            params = dict(urllib.parse.parse_qsl(parsed.query))
            if params.get("state") != expected_state:
                captured.error = "state_mismatch"
                self._ok(_callback_html(success=False, message="state mismatch"))
                shutdown_event.set(); return
            if "error" in params:
                captured.error = params.get("error")
                captured.error_description = params.get("error_description")
                self._ok(_callback_html(success=False,
                                        message=params.get("error_description")
                                                or params.get("error", "")))
                shutdown_event.set(); return
            captured.code = params.get("code")
            captured.state = params.get("state")
            self._ok(_callback_html(success=True))
            shutdown_event.set()
    return Handler


def _callback_html(*, success: bool, message: str = "") -> str:
    if success:
        return ("<!DOCTYPE html><meta charset='utf-8'><title>Vault unlocked</title>"
                "<body style='font-family:ui-sans-serif;background:#0b0b14;color:#e8e6f0;"
                "display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0'>"
                "<div style='text-align:center'><div style='font-size:3rem;color:#8b6dff'>✓</div>"
                "<h1>Vault unlocked</h1><p style='color:#a89dd0'>You can close this tab.</p>"
                "</div></body>")
    return ("<!DOCTYPE html><meta charset='utf-8'><title>Unlock failed</title>"
            "<body style='font-family:ui-sans-serif;background:#0b0b14;color:#e8e6f0;"
            "display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0'>"
            "<div style='text-align:center'><div style='font-size:3rem;color:#ff6d8b'>!</div>"
            f"<h1>Unlock failed</h1><p style='color:#a89dd0'>{message or 'authorization rejected'}</p>"
            "</div></body>")


def _osc8_link(url: str, label: Optional[str] = None) -> str:
    return f"\033]8;;{url}\033\\{label or url}\033]8;;\033\\"


#: OIDC parameters that force Keycloak to re-run the WHOLE browser flow —
#: password and all — rather than stepping up only the expired factor. None
#: of these may appear on a routine unlock.
FULL_REAUTH_PARAMS = ("max_age", "prompt")


def build_authorize_params(
    *, redirect_uri: str, challenge: str, state: str, nonce: str,
    force_full_login: bool = False,
) -> dict:
    """Build the authorization-request params for a loa3 step-up.

    **loa3 expires; loa1 does not.** Stepping up means re-running the factor
    that went stale, not the whole login. `acr_values=loa3` alone lets
    Keycloak's Conditional-LoA flow see that the live SSO session already
    satisfies loa1/loa2 and run only the loa3 execution.

    Two previous attempts got this wrong in the same way, which is why
    there is a test pinning the behaviour rather than the spelling:

    * v0.9.2 sent ``prompt=login`` — an explicit full re-authentication.
    * v0.9.3 "fixed" it with ``max_age=0``, on the reasoning that it would
      force a fresh ``auth_time`` without a full login form. It does not.
      OIDC ``max_age=0`` asserts the user authenticated no more than zero
      seconds ago, so *every* level is stale and Keycloak re-runs the entire
      flow — password included. Same symptom, different parameter.

    Nor can we send ``max_age=<brain's window>``: an SSO session hours old
    satisfies loa1 perfectly legitimately, and any ``max_age`` marks that
    stale too and demands the password again. Per-level freshness is a
    **realm** concern — the ``max-age`` configured on the loa3 conditional
    subflow — and that is the only thing that should decide whether the
    second factor is re-challenged.

    *force_full_login* adds ``prompt=login`` deliberately, as an escape
    hatch for a wedged session. Never the default.
    """
    params = {
        "client_id": SHARED_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": "openid email profile",
        "acr_values": "loa3",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
    }
    if force_full_login:
        params["prompt"] = "login"
    return params


def mint_loa3_jwt(
    *, prompt: bool = True, ttl: int = 180, force_full_login: bool = False
) -> tuple[str, int]:
    """Mint a fresh JWT with acr=loa3 via authorization-code + PKCE on
    a loopback redirect. Auto-discovers everything; uses the shared
    public ``brain-plugin-client``.

    Requests a step-up only: the existing SSO session keeps satisfying
    loa1/loa2 and Keycloak re-runs just the loa3 factor. Pass
    *force_full_login* to add ``prompt=login`` and re-authenticate from
    scratch — an escape hatch for a wedged session, never the default.
    """
    if not prompt:
        die("loa3 JWT not cached and prompt=False; run `vault-unlock`")

    brain_url = discover_brain_url()
    meta = discover_oauth_metadata(brain_url)

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    captured = _CodeCapture()
    shutdown_event = _threading.Event()

    handler_cls = _build_code_handler(captured, state, shutdown_event)

    with _socketserver.TCPServer(("127.0.0.1", 0), handler_cls) as srv:
        srv.allow_reuse_address = True
        host, port = srv.server_address
        redirect_uri = f"http://{host}:{port}/callback"

        params = build_authorize_params(
            redirect_uri=redirect_uri, challenge=challenge,
            state=state, nonce=nonce, force_full_login=force_full_login,
        )
        authorize = f"{meta['authorization_endpoint']}?" + urllib.parse.urlencode(params)

        sys.stderr.write(
            f"vault: complete MFA in your browser within {ttl}s\n"
            f"  {_osc8_link(authorize)}\n"
        )
        sys.stderr.flush()

        try:
            _webbrowser.open(authorize, new=2)
        except Exception:
            pass

        server_thread = _threading.Thread(target=srv.serve_forever, daemon=True)
        server_thread.start()
        try:
            shutdown_event.wait(timeout=ttl)
        finally:
            srv.shutdown()
            server_thread.join(timeout=2)

    if captured.error:
        die(f"keycloak rejected: {captured.error}"
            f"{(' — ' + captured.error_description) if captured.error_description else ''}")
    if not captured.code:
        die("unlock timed out — no auth code received within the TTL", code=2)

    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": SHARED_CLIENT_ID,
        "code": captured.code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }).encode()

    req = urllib.request.Request(
        meta["token_endpoint"], data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode()
        except Exception:
            pass
        die(f"token exchange failed ({e.code}): {body_text or e.reason}")
    except Exception as e:
        die(f"token exchange request failed: {e}")

    token = payload.get("access_token")
    expires_in = int(payload.get("expires_in", 0))
    if not token or expires_in < 30:
        die("keycloak returned no usable access_token")

    expires_at = int(time.time()) + expires_in
    store_jwt(account=brain_url, access_token=token, expires_at=expires_at)
    return token, expires_at


def get_jwt_or_die(*, prompt_password: bool) -> str:
    """Return a JWT the server will accept for a value read, or exit.

    The unexpired cached token is good enough to ASK the server for its
    policy (``/vault/policy`` is catalog-grade — no acr required), so it
    is used for that even when it is too stale to spend on a value read.
    """
    brain_url = discover_brain_url()
    unexpired = _read_with_expiry(kind="jwt", account=brain_url)
    window = fetch_loa3_window(unexpired)

    cached = get_cached_jwt(brain_url, max_auth_age=window)
    if cached:
        return cached

    if not prompt_password:
        age = jwt_auth_age(unexpired) if unexpired else None
        if age is not None:
            die(
                f"cached loa3 JWT is stale: last MFA was {age}s ago, server "
                f"accepts {window}s. Run `vault-unlock` to re-authenticate."
            )
        die(
            "no fresh loa3 JWT in cache. Run `vault-unlock` to complete MFA. "
            f"Cached tokens are usable for {window}s after MFA (the server's "
            "loa3 freshness window)."
        )
    token, _ = mint_loa3_jwt(prompt=True)
    return token


# ---------------------------------------------------------------------------
# Brain HTTP client
# ---------------------------------------------------------------------------

def _brain_url(path: str) -> str:
    base = discover_brain_url()
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def brain_request(
    method: str,
    path: str,
    *,
    body: Optional[dict] = None,
    query: Optional[dict] = None,
    jwt: str,
) -> dict[str, Any]:
    url = _brain_url(path)
    if query:
        url += ("?" + urllib.parse.urlencode(
            {k: v for k, v in query.items() if v is not None}
        ))
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": f"Bearer {jwt}",
        "Accept": "application/json",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode()
        except Exception:
            pass
        try:
            parsed = json.loads(body_text)
        except Exception:
            parsed = None
        raise VaultCliError(
            f"HTTP {e.code} from {method} {path}: {parsed or body_text or e.reason}",
            status=e.code,
            payload=parsed if isinstance(parsed, dict) else None,
        )
    except Exception as e:
        raise VaultCliError(f"{method} {path} failed: {e}")
