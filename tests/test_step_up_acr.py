"""The step-up asks Keycloak for the level the user's vault policy names.

Until v0.11.1 the plugin hard-coded ``acr_values=loa3``. brain#267 removed
loa3, and Keycloak ignores an acr it doesn't know, so every step-up came
back as a loa1 token the vault refuses.

Run: python -m pytest tests/  (needs only pytest; the server is mocked)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "brain" / "bin"
sys.path.insert(0, str(BIN))

import _vault_common as vc  # noqa: E402


@pytest.fixture
def policy(monkeypatch):
    """Serve *policy* from /vault/policy; reset the per-process cache."""
    answer: dict = {}
    monkeypatch.setattr(vc, "_policy_cache", {})
    monkeypatch.setattr(vc, "discover_brain_url", lambda: "https://brain.example")

    def fake(method, path, *, body=None, query=None, jwt):
        assert (method, path) == ("GET", "/api/v1/vault/policy")
        return dict(answer)

    monkeypatch.setattr(vc, "brain_request", fake)
    return answer


def _params(acr, force=False):
    return vc.build_authorize_params(redirect_uri="http://127.0.0.1:1/callback",
                                     challenge="c", state="s", nonce="n",
                                     acr=acr, force_full_login=force)


@pytest.mark.parametrize("acr", ["loa1", "loa2"])
def test_authorize_asks_for_the_given_level(acr):
    assert _params(acr)["acr_values"] == acr


def test_routine_step_up_never_forces_a_full_login():
    """prompt/max_age re-run the whole flow, password included."""
    p = _params("loa2")
    assert not set(vc.FULL_REAUTH_PARAMS) & set(p)


def test_build_authorize_params_needs_an_acr():
    with pytest.raises(TypeError):
        vc.build_authorize_params(redirect_uri="x", challenge="c", state="s", nonce="n")


def test_passkey_policy_steps_up_to_loa2(policy):
    policy.update({"required_acr": "loa2", "reauth": "acr"})
    assert vc.step_up_target("tok") == ("loa2", False)


def test_password_policy_is_loa1_with_a_full_login(policy):
    policy.update({"required_acr": "loa1", "reauth": "login"})
    assert vc.step_up_target("tok") == ("loa1", True)


def test_brain_before_per_user_policies_uses_required_acr_for_value(policy):
    policy.update({"required_acr_for_value": "loa2"})
    assert vc.step_up_target("tok") == ("loa2", False)


@pytest.mark.parametrize("jwt", [None, "tok"])
def test_no_policy_answer_falls_back_to_loa2(policy, jwt):
    assert vc.step_up_target(jwt) == ("loa2", False)


def test_window_is_the_policy_window(policy):
    policy.update({"max_age_seconds": 1800, "loa3_max_age_seconds": 999})
    assert vc.fetch_loa3_window("tok") == 1800


def test_window_reads_the_old_name_from_older_brains(policy):
    policy.update({"loa3_max_age_seconds": 120})
    assert vc.fetch_loa3_window("tok") == 120


def test_denial_without_required_acr_names_loa2():
    err = vc.VaultCliError("x", status=403, payload={
        "error": "step_up_required", "current_acr": "loa1"})
    assert vc.explain_denial(err) == (
        "token is loa1, vault values require loa2. Run `vault-unlock` to complete MFA.")


@pytest.mark.parametrize("answer,acr,login", [
    ({"required_acr": "loa2", "reauth": "acr"}, "loa2", False),
    ({"required_acr": "loa1", "reauth": "login"}, "loa1", True),
])
def test_mint_sends_the_policy_level_to_keycloak(policy, monkeypatch, answer, acr, login):
    import urllib.parse
    policy.update(answer)
    opened = []
    monkeypatch.setattr(vc, "_read_with_expiry", lambda **kw: "cached.tok")
    monkeypatch.setattr(vc, "discover_oauth_metadata", lambda url: {
        "authorization_endpoint": "https://kc.example/auth",
        "token_endpoint": "https://kc.example/token"})
    monkeypatch.setattr(vc, "open_in_browser", lambda url, label: opened.append(url) or True)
    with pytest.raises(SystemExit):  # nothing comes back: times out
        vc.mint_loa3_jwt(prompt=True, ttl=0.05)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(opened[0]).query)
    assert q["acr_values"] == [acr]
    assert ("prompt" in q) is login
