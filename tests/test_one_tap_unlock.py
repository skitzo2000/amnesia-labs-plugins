"""One tap opens the vault, by default (v0.11.1).

When a read needs it, vault-run signs in itself: one Keycloak tab at the
policy's level, the security key tap (which, with the brain-vault login
theme, also unlocks the vault key), the token back on 127.0.0.1, then
this Claude session's grant, then the same read again. No portal page and
no code. The portal approval only runs with BRAIN_VAULT_APPROVE=1.

Run: python -m pytest tests/  (needs only pytest; the server is mocked)
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "brain" / "bin"
sys.path.insert(0, str(BIN))

import _vault_common as vc  # noqa: E402

SESSION = "22222222-2222-4222-8222-222222222222"
ACCOUNT = "https://brain.example"


def _load(name: str):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(BIN / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def env(monkeypatch):
    calls: list[tuple] = []
    state = {"grant": False, "reads": 0, "fail_reads": 1}
    monkeypatch.setenv(vc.SESSION_ENV, SESSION)
    monkeypatch.delenv(vc.APPROVE_ENV, raising=False)
    vc._policy_cache.clear()
    monkeypatch.setattr(vc, "discover_brain_url", lambda: ACCOUNT)
    monkeypatch.setattr(vc, "vault_namespace", lambda: "alice:vault")
    minted: list[bool] = []

    def mint(*, prompt=True, ttl=180, force_full_login=False):
        minted.append(force_full_login)
        return "fresh.jwt", 0

    monkeypatch.setattr(vc, "mint_loa3_jwt", mint)

    def no_approval(*a, **k):
        raise AssertionError("the portal approval must not run by default")

    monkeypatch.setattr(vc, "approve_session", no_approval)

    def brain(method, path, *, body=None, query=None, jwt):
        calls.append((method, path, jwt))
        if path == "/api/v1/vault/policy":
            return {"session_grant": True, "grant_ttl_seconds": 1800,
                    "required_acr": "loa2", "reauth": "acr", "max_age_seconds": 1800}
        if (method, path) == ("POST", "/api/v1/vault/session"):
            assert jwt == "fresh.jwt"
            state["grant"] = True
            return {"status": "open", "expires_in": 1800}
        if (method, path) == ("GET", "/api/v1/vault/session"):
            return {"open": state["grant"], "expires_in": 1800 if state["grant"] else 0}
        if path == "/api/v1/vault/value/get":
            state["reads"] += 1
            if state["reads"] <= state["fail_reads"]:
                raise vc.VaultCliError("HTTP 403", status=403, payload={
                    "error": "step_up_required", "reason": "stale_auth_time",
                    "required_acr": "loa2", "reauth": "acr"})
            return {"env_var": "GH_TOKEN", "value": "ghp_x"}
        if path == "/api/v1/vault/unlock/status":
            return {"enrolled": True, "locked": False, "expires_in": 300, "session_hold": True}
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(vc, "brain_request", brain)
    return calls, state, minted


def test_vault_run_taps_opens_the_grant_and_gets_the_value(env, monkeypatch):
    calls, state, minted = env
    vr = _load("vault-run")
    monkeypatch.setattr(vr, "brain_request", vc.brain_request)
    env_var, value, jwt = vr._resolve_one("secret:gh:token", "alice:vault", "stale.jwt")
    assert (env_var, value, jwt) == ("GH_TOKEN", "ghp_x", "fresh.jwt")
    assert minted == [False]  # one tap, no full login
    assert ("POST", "/api/v1/vault/session", "fresh.jwt") in calls
    reads = [c for c in calls if c[1] == "/api/v1/vault/value/get"]
    assert [r[2] for r in reads] == ["stale.jwt", "fresh.jwt"]


def test_nothing_cached_taps_instead_of_approval(env, monkeypatch):
    calls, state, minted = env
    monkeypatch.setattr(vc, "_read_with_expiry", lambda **k: None)
    monkeypatch.setattr(vc, "refresh_jwt", lambda account: None)
    assert vc.get_jwt_or_die(prompt_password=False, approve=True) == "fresh.jwt"
    assert minted == [False] and state["grant"]


def test_outside_a_session_the_tap_opens_no_grant(env, monkeypatch):
    calls, state, minted = env
    monkeypatch.delenv(vc.SESSION_ENV)
    assert vc.tap_open_grant("alice:vault") == "fresh.jwt"
    assert not state["grant"]
    assert not any(c[1] == "/api/v1/vault/session" for c in calls)


def test_brain_refusing_the_grant_is_reported(env, monkeypatch):
    def brain(method, path, *, body=None, query=None, jwt):
        raise vc.VaultCliError("HTTP 403", status=403, payload={"error": "step_up_required"})

    monkeypatch.setattr(vc, "brain_request", brain)
    with pytest.raises(SystemExit) as e:
        vc.tap_open_grant("alice:vault")
    assert e.value.code == 2


def test_approve_env_brings_back_the_portal_approval(env, monkeypatch):
    monkeypatch.setenv(vc.APPROVE_ENV, "1")
    ran = []
    monkeypatch.setattr(vc, "approve_session", lambda account: ran.append(account) or ("approved", "ok.jwt"))
    err = vc.VaultCliError("HTTP 403", status=403, payload={"error": "step_up_required"})
    assert vc.handle_step_up(err, ACCOUNT) == "ok.jwt"
    assert ran == [ACCOUNT]


def test_vault_unlock_in_a_session_is_one_tap(env, monkeypatch, capsys):
    calls, state, minted = env
    vu = _load("vault-unlock")
    monkeypatch.setattr(vu, "session_jwt", lambda account: "cached.jwt")
    monkeypatch.setattr(vu, "get_cached_jwt", lambda account, max_auth_age=None: None)
    monkeypatch.setattr(vu, "_read_with_expiry", lambda **k: "cached.jwt")
    assert vu.main([]) == 0
    assert minted == [False] and state["grant"]
    assert "until this Claude session ends" in capsys.readouterr().out
    assert not any("/vault/session/request" in c[1] for c in calls)
