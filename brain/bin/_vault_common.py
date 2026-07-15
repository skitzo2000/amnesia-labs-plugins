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
    """Anything the CLI surfaces to the user via stderr + exit code."""


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


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


def _decode_jwt_email(jwt: str) -> Optional[str]:
    """Decode the JWT payload (no signature check — we just authenticated
    against the issuer) and return the email claim.
    """
    try:
        payload_b64 = jwt.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode())
        return payload.get("email") or payload.get("preferred_username")
    except Exception:
        return None


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
    _secret_tool_store(
        label, value,
        service=_SCHEMA_SERVICE,
        kind=kind, account=account, exp=str(expires_at),
    )


def _read_with_expiry(*, kind: str, account: str, min_remaining: int = 30) -> Optional[str]:
    token = _secret_tool_lookup(
        service=_SCHEMA_SERVICE, kind=kind, account=account,
    )
    if not token:
        return None
    try:
        proc = subprocess.run(
            ["secret-tool", "search", "--all",
             "service", _SCHEMA_SERVICE, "kind", kind, "account", account],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return None
    # `secret-tool search` writes the matched item's label + secret to
    # stdout but the attribute lines (`attribute.exp = …`) go to stderr.
    # Parsing stdout silently dropped the expiry, leaving exp=0 → return
    # None → "no fresh JWT" even for a valid cached token. Read stderr.
    exp = 0
    for line in (proc.stdout + "\n" + proc.stderr).splitlines():
        line = line.strip()
        if line.startswith("attribute.exp ="):
            try:
                exp = int(line.split("=", 1)[1].strip())
            except ValueError:
                exp = 0
            break
    if exp == 0 or time.time() >= (exp - min_remaining):
        return None
    return token


def store_jwt(account: str, access_token: str, expires_at: int) -> None:
    _store_with_expiry(
        kind="jwt", account=account, value=access_token,
        expires_at=expires_at,
        label=f"Brain vault JWT (exp:{expires_at})",
    )


def get_cached_jwt(account: str) -> Optional[str]:
    return _read_with_expiry(kind="jwt", account=account)


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


def mint_loa3_jwt(*, prompt: bool = True, ttl: int = 180) -> tuple[str, int]:
    """Mint a fresh JWT with acr=loa3 via authorization-code + PKCE on
    a loopback redirect. Auto-discovers everything; uses the shared
    public ``brain-plugin-client``.
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
            "prompt": "login",
        }
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
    """Return a usable JWT — cached if fresh, otherwise minted (or error)."""
    brain_url = discover_brain_url()
    cached = get_cached_jwt(brain_url)
    if cached:
        return cached
    if not prompt_password:
        die(
            "no fresh loa3 JWT in cache. Run `vault-unlock` to complete MFA. "
            "Cached tokens are valid for ~5 min (loa3 freshness window, server default 300s)."
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
            f"HTTP {e.code} from {method} {path}: {parsed or body_text or e.reason}"
        )
    except Exception as e:
        raise VaultCliError(f"{method} {path} failed: {e}")
