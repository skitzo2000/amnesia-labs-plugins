"""vault-put (brain #242): store file and binary secrets without a paste form.

Run: python -m pytest tests/  (needs only pytest; the server is mocked)
"""

from __future__ import annotations

import base64
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "brain" / "bin"
sys.path.insert(0, str(BIN))

import _vault_common as vc  # noqa: E402


def _load_vault_put():
    loader = importlib.machinery.SourceFileLoader("vault_put", str(BIN / "vault-put"))
    spec = importlib.util.spec_from_loader("vault_put", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


vp = _load_vault_put()

# 32 random-looking bytes including NUL, newline and non-UTF-8 bytes.
OTA_KEY = bytes(range(0, 256, 8)) + b"\n\x00\xff"
SECRET = "ghp_SUPERSECRETVALUE123"


class FakeBrain:
    """Stands in for brain_request; keeps what put stored, like the server."""

    def __init__(self):
        self.calls = []
        self.store = {}
        self.fail = None

    def __call__(self, method, path, *, body=None, query=None, jwt):
        self.calls.append((method, path, dict(body or {}), jwt))
        if self.fail:
            raise self.fail
        key = body["episode_id"]
        if path.endswith("/put"):
            if key in self.store:
                raise vc.VaultCliError("HTTP 409", status=409, payload={
                    "error": f"vault entry already exists at {body['namespace']}/{key}; "
                             "use rotate"})
        elif key not in self.store:
            raise vc.VaultCliError("HTTP 404", status=404, payload={"error": "not_found"})
        self.store[key] = body["value"]
        return {"status": "created", "metadata": {
            "namespace": body["namespace"], "episode_id": key,
            "env_var": body["env_var"], "value_type": body.get("value_type"),
            "username": body.get("username"), "sealed": True}}


@pytest.fixture
def brain(monkeypatch):
    fake = FakeBrain()
    monkeypatch.setattr(vp, "brain_request", fake)
    monkeypatch.setattr(vp, "write_jwt", lambda: "fresh.loa3.jwt")
    monkeypatch.setattr(vp, "vault_namespace", lambda: "alice:vault")
    return fake


def _run(argv, capsys, stdin: bytes | None = None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin)))
    try:
        code = vp.main(argv)
    except SystemExit as e:
        code = e.code
    out, err = capsys.readouterr()
    return code, out, err


# ---------------------------------------------------------------------------
# Acceptance: binary file round-trips byte-identically
# ---------------------------------------------------------------------------

def test_binary_file_round_trip_is_byte_identical(brain, tmp_path, capsys):
    key = tmp_path / "ota.key"
    key.write_bytes(OTA_KEY)
    code, out, err = _run(["secret:dns2:ota-key", "--env-var", "DNS2_OTA_KEY",
                           "--type", "private_key", "--file", str(key), "--base64"], capsys)
    assert code == 0, err

    stored = brain.store["secret:dns2:ota-key"]
    restored = tmp_path / "restored.key"
    # The restore the docstring documents, run for real through sh + base64.
    subprocess.run(["sh", "-c", 'printf %s "$DNS2_OTA_KEY" | base64 -d > "$OUT"'],
                   env={**os.environ, "DNS2_OTA_KEY": stored, "OUT": str(restored)},
                   check=True)
    assert restored.read_bytes() == OTA_KEY


def test_binary_without_base64_is_refused(brain, tmp_path, capsys):
    key = tmp_path / "ota.key"
    key.write_bytes(OTA_KEY)
    code, _, err = _run(["secret:x", "--env-var", "X", "--file", str(key)], capsys)
    assert code == 1
    assert "--base64" in err
    assert brain.calls == []


# ---------------------------------------------------------------------------
# Acceptance: the value never appears in output, argv or the request path
# ---------------------------------------------------------------------------

def test_value_never_printed(brain, tmp_path, capsys):
    f = tmp_path / "tok"
    f.write_text(SECRET + "\n")
    code, out, err = _run(["secret:gh:token", "--env-var", "GH_TOKEN", "--file", str(f)], capsys)
    assert code == 0
    assert SECRET not in out and SECRET not in err
    assert json.loads(out)["env_var"] == "GH_TOKEN"


def test_base64_form_never_printed(brain, tmp_path, capsys):
    key = tmp_path / "ota.key"
    key.write_bytes(OTA_KEY)
    _, out, err = _run(["secret:k", "--env-var", "K", "--file", str(key), "--base64"], capsys)
    b64 = base64.b64encode(OTA_KEY).decode()
    assert b64 not in out and b64 not in err


def test_value_never_printed_on_server_error(brain, tmp_path, capsys):
    brain.fail = vc.VaultCliError("HTTP 500 from POST /api/v1/vault/value/put: boom",
                                  status=500, payload={"error": "boom"})
    f = tmp_path / "tok"
    f.write_text(SECRET)
    code, out, err = _run(["secret:gh:token", "--env-var", "GH_TOKEN", "--file", str(f)], capsys)
    assert code == 1
    assert SECRET not in out and SECRET not in err


def test_no_value_option_on_the_command_line():
    """There is no way to pass the value itself as an argument."""
    with pytest.raises(SystemExit):
        vp.main(["secret:x", "--env-var", "X", SECRET])
    with pytest.raises(SystemExit):  # a source is mandatory
        vp.main(["secret:x", "--env-var", "X"])


# ---------------------------------------------------------------------------
# Sources and request shape
# ---------------------------------------------------------------------------

def test_stdin_text_strips_trailing_newline(brain, capsys, monkeypatch):
    code, _, err = _run(["secret:gh:token", "--env-var", "GH_TOKEN", "--stdin"],
                        capsys, stdin=(SECRET + "\n").encode(), monkeypatch=monkeypatch)
    assert code == 0, err
    assert brain.store["secret:gh:token"] == SECRET


def test_stdin_base64_keeps_every_byte(brain, capsys, monkeypatch):
    code, _, _ = _run(["secret:k", "--env-var", "K", "--stdin", "--base64"],
                      capsys, stdin=OTA_KEY, monkeypatch=monkeypatch)
    assert code == 0
    assert base64.b64decode(brain.store["secret:k"]) == OTA_KEY


def test_empty_value_refused(brain, tmp_path, capsys):
    f = tmp_path / "empty"
    f.write_text("\n")
    code, _, err = _run(["secret:x", "--env-var", "X", "--file", str(f)], capsys)
    assert code == 1 and "empty" in err
    assert brain.calls == []


def test_missing_file_reported(brain, tmp_path, capsys):
    code, _, err = _run(["secret:x", "--env-var", "X", "--file", str(tmp_path / "nope")], capsys)
    assert code == 1 and "cannot read" in err


def test_put_request_carries_catalog_fields(brain, tmp_path, capsys):
    f = tmp_path / "tok"
    f.write_text(SECRET)
    _run(["secret:gh:token", "--env-var", "GH_TOKEN", "--type", "token",
          "--username", "alice@example.com", "--purpose", "CI pushes", "--file", str(f)], capsys)
    method, path, body, jwt = brain.calls[0]
    assert (method, path, jwt) == ("POST", "/api/v1/vault/value/put", "fresh.loa3.jwt")
    assert body == {"namespace": "alice:vault", "episode_id": "secret:gh:token",
                    "value": SECRET, "env_var": "GH_TOKEN", "value_type": "token",
                    "username": "alice@example.com", "purpose": "CI pushes"}


def test_namespace_override(brain, tmp_path, capsys):
    f = tmp_path / "tok"
    f.write_text(SECRET)
    _run(["secret:x", "--env-var", "X", "--file", str(f), "--namespace", "bob:vault"], capsys)
    assert brain.calls[0][2]["namespace"] == "bob:vault"


# ---------------------------------------------------------------------------
# put vs rotate
# ---------------------------------------------------------------------------

def test_put_on_existing_entry_points_at_rotate(brain, tmp_path, capsys):
    brain.store["secret:gh:token"] = "old"
    f = tmp_path / "tok"
    f.write_text(SECRET)
    code, _, err = _run(["secret:gh:token", "--env-var", "GH_TOKEN", "--file", str(f)], capsys)
    assert code == 1 and "--rotate" in err
    assert brain.store["secret:gh:token"] == "old"


def test_rotate_replaces_value(brain, tmp_path, capsys):
    brain.store["secret:gh:token"] = "old"
    f = tmp_path / "tok"
    f.write_text(SECRET)
    code, _, _ = _run(["secret:gh:token", "--env-var", "GH_TOKEN", "--file", str(f),
                       "--rotate"], capsys)
    assert code == 0
    assert brain.calls[0][1] == "/api/v1/vault/value/rotate"
    assert brain.store["secret:gh:token"] == SECRET


def test_rotate_missing_entry_says_so(brain, tmp_path, capsys):
    f = tmp_path / "tok"
    f.write_text(SECRET)
    code, _, err = _run(["secret:new", "--env-var", "X", "--file", str(f), "--rotate"], capsys)
    assert code == 1 and "does not exist" in err


# ---------------------------------------------------------------------------
# MFA freshness: writes need a fresh step-up, a session grant is not enough
# ---------------------------------------------------------------------------

def test_stale_mfa_exits_2_before_any_request(monkeypatch, tmp_path, capsys):
    fake = FakeBrain()
    monkeypatch.setattr(vp, "brain_request", fake)
    monkeypatch.setattr(vp, "discover_brain_url", lambda: "https://brain.example")
    monkeypatch.setattr(vp, "_read_with_expiry", lambda **_: "stale.jwt")
    monkeypatch.setattr(vp, "fetch_loa3_window", lambda _jwt: 300)
    monkeypatch.setattr(vp, "get_cached_jwt", lambda *_a, **_k: None)
    monkeypatch.setattr(vp, "jwt_auth_age", lambda _jwt: 900)
    monkeypatch.setattr(vp, "vault_namespace", lambda: "alice:vault")
    f = tmp_path / "tok"
    f.write_text(SECRET)
    code, _, err = _run(["secret:x", "--env-var", "X", "--file", str(f)], capsys)
    assert code == 2
    assert "vault-unlock --force" in err and "900s" in err
    assert fake.calls == []


def test_fresh_mfa_token_is_used(monkeypatch):
    monkeypatch.setattr(vp, "discover_brain_url", lambda: "https://brain.example")
    monkeypatch.setattr(vp, "_read_with_expiry", lambda **_: "tok")
    monkeypatch.setattr(vp, "fetch_loa3_window", lambda _jwt: 300)
    seen = {}

    def cached(account, *, max_auth_age):
        seen["max_auth_age"] = max_auth_age
        return "fresh.jwt"

    monkeypatch.setattr(vp, "get_cached_jwt", cached)
    assert vp.write_jwt() == "fresh.jwt"
    assert seen["max_auth_age"] == 300


def test_server_stale_auth_time_says_force(brain, tmp_path, capsys):
    brain.fail = vc.VaultCliError("HTTP 403", status=403, payload={
        "error": "step_up_required", "reason": "stale_auth_time",
        "auth_age_seconds": 400, "max_age_seconds": 300})
    f = tmp_path / "tok"
    f.write_text(SECRET)
    code, _, err = _run(["secret:x", "--env-var", "X", "--file", str(f)], capsys)
    assert code == 2 and "vault-unlock --force" in err
