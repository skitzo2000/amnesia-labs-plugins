"""Approving a Claude session's vault access in the portal (brain#253).

The CLI asks Brain for an approval request, prints the code, opens the
portal link (signing in first, in the same tab, only when nothing is
cached), and waits for the outcome.

Run: python -m pytest tests/  (needs only pytest; the server is mocked)
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "brain" / "bin"
sys.path.insert(0, str(BIN))

import _vault_common as vc  # noqa: E402

SESSION = "11111111-1111-4111-8111-111111111111"
ACCOUNT = "https://brain.example"


def _load(name: str):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(BIN / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class FakeBrain:
    """Stands in for brain_request: policy, approval requests, grants."""

    def __init__(self, approvals=True):
        self.calls = []
        self.approvals = approvals
        self.statuses = ["pending", "approved"]
        self.grant_open = False
        self.grant_left = 0
        self.request_error = None

    def __call__(self, method, path, *, body=None, query=None, jwt):
        self.calls.append((method, path, dict(body or {}), jwt))
        if path == "/api/v1/vault/policy":
            policy = {"session_grant": True, "loa3_max_age_seconds": 300}
            if self.approvals:
                policy.update({"grant_ttl_seconds": 43200, "reauth": "acr"})
            return policy
        if path == "/api/v1/vault/session/request":
            if self.request_error:
                raise self.request_error
            return {"request_id": "req-1", "user_code": "ABCD-EFGH",
                    "approve_url": "https://portal.example/vault/approve?request=req-1",
                    "expires_in": 300, "interval": 2}
        if path.startswith("/api/v1/vault/session/request/"):
            status = self.statuses.pop(0) if self.statuses else "approved"
            if status == "approved":
                self.grant_open, self.grant_left = True, 43200
            return {"status": status}
        if path == "/api/v1/vault/session":
            return {"open": self.grant_open, "expires_in": self.grant_left}
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture
def env(monkeypatch, capsys):
    fake = FakeBrain()
    vc._policy_cache.clear()
    monkeypatch.setenv(vc.SESSION_ENV, SESSION)
    # Portal approval is opt-in from v0.11.1; the one-tap unlock is the
    # default (tests/test_one_tap_unlock.py).
    monkeypatch.setenv(vc.APPROVE_ENV, "1")
    monkeypatch.setattr(vc, "brain_request", fake)
    monkeypatch.setattr(vc, "discover_brain_url", lambda: ACCOUNT)
    monkeypatch.setattr(vc, "vault_namespace", lambda: "alice:vault")
    opened = []
    monkeypatch.setattr(vc, "open_in_browser", lambda url, label: opened.append(url) or True)
    tokens = {"jwt": "cached.jwt"}
    monkeypatch.setattr(vc, "session_jwt", lambda account: tokens["jwt"])
    monkeypatch.setattr(vc.time, "sleep", lambda s: None)
    yield fake, opened, tokens
    vc._policy_cache.clear()


# ---------------------------------------------------------------------------
# approve_session
# ---------------------------------------------------------------------------

def test_cached_token_requests_shows_code_opens_link_and_waits(env, capsys):
    fake, opened, _ = env
    outcome, tok = vc.approve_session(ACCOUNT)
    assert outcome == "approved" and tok == "cached.jwt"
    assert opened == ["https://portal.example/vault/approve?request=req-1"]
    err = capsys.readouterr().err
    assert "ABCD-EFGH" in err
    req = next(c for c in fake.calls if c[1] == "/api/v1/vault/session/request")
    assert req[2]["namespace"] == "alice:vault" and req[2]["label"]


@pytest.mark.parametrize("final", ["denied", "expired"])
def test_denied_or_expired_is_reported(env, final):
    fake, _, _ = env
    fake.statuses = ["pending", final]
    outcome, _ = vc.approve_session(ACCOUNT)
    assert outcome == final
    assert "run it again" in vc.explain_approval("expired")


def test_older_brain_is_unsupported(env):
    fake, opened, _ = env
    fake.approvals = False
    outcome, tok = vc.approve_session(ACCOUNT)
    assert outcome == "unsupported" and tok == "cached.jwt"
    assert opened == []
    assert not any(c[1] == "/api/v1/vault/session/request" for c in fake.calls)


def test_wait_times_out(env, monkeypatch):
    fake, _, _ = env
    fake.statuses = ["pending"] * 1000
    t = [0.0]

    def clock():
        t[0] += 50
        return t[0]

    assert vc.wait_for_approval("req-1", ACCOUNT, 300, clock=clock) == "timeout"


def test_wait_reports_sign_out_instead_of_polling_forever(env):
    _, _, tokens = env
    tokens["jwt"] = None
    assert vc.wait_for_approval("req-1", ACCOUNT, 300) == "signed_out"


def test_wait_treats_a_vanished_request_as_expired(env, monkeypatch):
    fake, _, _ = env

    def gone(method, path, **kw):
        raise vc.VaultCliError("HTTP 404", status=404, payload={"error": "not_found"})

    monkeypatch.setattr(vc, "brain_request", gone)
    assert vc.wait_for_approval("req-1", ACCOUNT, 300) == "expired"


def test_wait_keeps_polling_through_transient_errors(env, monkeypatch):
    fake, _, _ = env
    errors = [vc.VaultCliError("HTTP 502", status=502)]

    def flaky(method, path, **kw):
        if errors:
            raise errors.pop()
        return fake(method, path, **kw)

    monkeypatch.setattr(vc, "brain_request", flaky)
    fake.statuses = ["approved"]
    assert vc.wait_for_approval("req-1", ACCOUNT, 300) == "approved"


def test_no_cached_token_signs_in_then_redirects_same_tab(env, monkeypatch, capsys):
    """With nothing cached, the sign-in's callback creates the request,
    prints the code, and the browser goes straight to the approval page."""
    fake, opened, tokens = env
    tokens["jwt"] = None
    seen = {}

    def fake_sign_in(next_url_for, ttl=180):
        tokens["jwt"] = "fresh.jwt"
        seen["redirect"] = next_url_for("fresh.jwt")
        return "fresh.jwt"

    monkeypatch.setattr(vc, "sign_in_then", fake_sign_in)
    outcome, tok = vc.approve_session(ACCOUNT)
    assert outcome == "approved" and tok == "fresh.jwt"
    assert seen["redirect"] == "https://portal.example/vault/approve?request=req-1"
    assert opened == []  # no second tab
    assert "ABCD-EFGH" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The loopback redirect handler
# ---------------------------------------------------------------------------

def _serve(handler_cls):
    import socketserver
    srv = socketserver.TCPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _get(port, path):
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        r = opener.open(f"http://127.0.0.1:{port}{path}", timeout=5)
        return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def test_callback_exchanges_then_redirects():
    captured, done = vc._CodeCapture(), threading.Event()
    calls = []
    handler = vc._build_redirect_handler(
        captured, "st", done, lambda code: calls.append(code) or "https://portal.example/next")
    srv = _serve(handler)
    try:
        status, headers, _ = _get(srv.server_address[1], "/callback?state=st&code=abc")
    finally:
        srv.shutdown()
    assert status == 302 and headers["Location"] == "https://portal.example/next"
    assert calls == ["abc"] and done.is_set()


def test_callback_rejects_a_wrong_state_without_exchanging():
    captured, done = vc._CodeCapture(), threading.Event()
    calls = []
    srv = _serve(vc._build_redirect_handler(captured, "st", done, lambda c: calls.append(c)))
    try:
        status, _, body = _get(srv.server_address[1], "/callback?state=evil&code=abc")
    finally:
        srv.shutdown()
    assert status == 200 and "state mismatch" in body
    assert calls == [] and captured.error == "state_mismatch"


def test_callback_shows_a_failed_exchange():
    captured, done = vc._CodeCapture(), threading.Event()

    def boom(code):
        raise vc.VaultCliError("token exchange failed (400)")

    srv = _serve(vc._build_redirect_handler(captured, "st", done, boom))
    try:
        status, _, body = _get(srv.server_address[1], "/callback?state=st&code=abc")
    finally:
        srv.shutdown()
    assert status == 200 and "sign-in failed" in body
    assert captured.error == "exchange_failed"


# ---------------------------------------------------------------------------
# Policy lookups and the unlock wait
# ---------------------------------------------------------------------------

def test_a_failed_policy_lookup_is_not_cached(monkeypatch):
    vc._policy_cache.clear()
    monkeypatch.setattr(vc, "discover_brain_url", lambda: ACCOUNT)
    answers = [vc.VaultCliError("HTTP 502", status=502), {"session_grant": True}]

    def flaky(method, path, **kw):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(vc, "brain_request", flaky)
    assert vc.server_has_session_grants("t") is False
    assert vc.server_has_session_grants("t") is True
    vc._policy_cache.clear()


def test_unlock_wait_refreshes_an_expired_token(monkeypatch):
    monkeypatch.setattr(vc, "discover_brain_url", lambda: ACCOUNT)
    monkeypatch.setattr(vc, "open_in_browser", lambda *a: True)
    seen = []

    def status(ns, jwt):
        seen.append(jwt)
        if jwt == "old":
            raise vc.VaultCliError("HTTP 401", status=401)
        return {"locked": False}

    monkeypatch.setattr(vc, "fetch_unlock_status", status)
    monkeypatch.setattr(vc, "refresh_jwt", lambda account: "new")
    assert vc.wait_for_unlock("a:vault", "old", "https://u", sleep=lambda s: None) is True
    assert seen == ["old", "new"]


def test_unlock_wait_stops_when_signed_out(monkeypatch, capsys):
    monkeypatch.setattr(vc, "discover_brain_url", lambda: ACCOUNT)
    monkeypatch.setattr(vc, "open_in_browser", lambda *a: True)
    monkeypatch.setattr(vc, "fetch_unlock_status",
                        lambda ns, jwt: (_ for _ in ()).throw(vc.VaultCliError("401", status=401)))
    monkeypatch.setattr(vc, "refresh_jwt", lambda account: None)
    assert vc.wait_for_unlock("a:vault", "old", "https://u", sleep=lambda s: None) is False
    assert "signed out" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# vault-unlock and vault-run
# ---------------------------------------------------------------------------

def test_vault_unlock_reports_only_after_approval(env, capsys):
    vu = _load("vault-unlock")
    rc = vu.main([])
    out = capsys.readouterr()
    assert rc == 0
    assert "open for this Claude session" in out.out
    assert out.err.index("ABCD-EFGH") >= 0


def test_vault_unlock_says_so_when_denied(env, capsys):
    fake, _, _ = env
    fake.statuses = ["denied"]
    vu = _load("vault-unlock")
    assert vu.main([]) == 1
    out = capsys.readouterr()
    assert "open for" not in out.out and "denied" in out.err


def test_vault_unlock_skips_when_already_open(env, capsys):
    fake, opened, _ = env
    fake.grant_open, fake.grant_left = True, 600
    vu = _load("vault-unlock")
    assert vu.main([]) == 0
    assert "already open" in capsys.readouterr().out
    assert opened == []


def test_vault_unlock_status_asks_brain(env, capsys):
    fake, _, _ = env
    vu = _load("vault-unlock")
    assert vu.main(["--status"]) == 1
    fake.grant_open, fake.grant_left = True, 900
    assert vu.main(["--status"]) == 0
    assert "(15 more minutes)" in capsys.readouterr().out


def test_vault_run_step_up_runs_approval_and_retries(env, monkeypatch):
    fake, opened, _ = env
    vr = _load("vault-run")
    reads = []

    def brain(method, path, *, body=None, query=None, jwt):
        if path == "/api/v1/vault/value/get":
            reads.append(jwt)
            if len(reads) == 1:
                raise vc.VaultCliError("HTTP 403", status=403, payload={
                    "error": "step_up_required", "required_acr": "loa3", "reauth": "acr"})
            return {"env_var": "GH_TOKEN", "value": "ghp_x"}
        return fake(method, path, body=body, query=query, jwt=jwt)

    monkeypatch.setattr(vc, "brain_request", brain)
    monkeypatch.setattr(vr, "brain_request", brain)
    env_var, value, _ = vr._resolve_one("secret:gh:token", "alice:vault", "cached.jwt")
    assert (env_var, value) == ("GH_TOKEN", "ghp_x")
    assert len(reads) == 2 and opened  # approval link opened once
