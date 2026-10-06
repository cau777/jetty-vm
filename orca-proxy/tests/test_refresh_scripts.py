"""Tests for the Claude/Codex OAuth-refresh Credential commands.

The catalog (`resources/quick-add-catalog.json`) is the source of truth for
these commands -- there is no separate Python implementation to test
instead. Each test loads the actual command string for a given key and runs
it through the real `CredentialCache` execution path (`bash -lc`, same as
production), against a stubbed `curl` on PATH so no real network call is
made. This exercises the exact bash that ships, not a hand-maintained copy
of it.
"""

import json
import os
import shlex
from pathlib import Path

import pytest

from orca_proxy.credential_exec import CredentialCache, CredentialExecutionError

CATALOG_PATH = Path(__file__).parent.parent / "src/orca_proxy/resources/quick-add-catalog.json"


def _catalog_command(key: str) -> str:
    catalog = json.loads(CATALOG_PATH.read_text())
    return next(entry["command"] for entry in catalog if entry["key"] == key)


CODEX_COMMAND = _catalog_command("codex-subscription")
CLAUDE_COMMAND = _catalog_command("claude-code-subscription")


def _install_mock_curl(bin_dir: Path, *, status: int, body: str) -> Path:
    """A stand-in `curl` that logs every arg it was called with (NUL-separated,
    so an arg containing a literal newline -- e.g. jq's pretty-printed JSON --
    doesn't get misread as multiple args) and mimics real `curl -f`'s
    behavior: print the body and exit 0 for status < 400, print nothing and
    exit nonzero otherwise.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    log_path = bin_dir / "curl.log"
    body_path = bin_dir / "curl_body.txt"
    body_path.write_text(body)
    curl_path = bin_dir / "curl"
    curl_path.write_text(
        f"""#!/usr/bin/env bash
printf '%s\\0' "$@" >> {shlex.quote(str(log_path))}
if [ {status} -ge 400 ]; then
  exit 22
fi
cat {shlex.quote(str(body_path))}
"""
    )
    curl_path.chmod(0o755)
    return log_path


def _read_argv(log_path: Path) -> list[str]:
    raw = log_path.read_bytes()
    return [part.decode() for part in raw.split(b"\0") if part]


@pytest.fixture
def bin_dir(tmp_path):
    return tmp_path / "bin"


def _use_mock_curl(monkeypatch, bin_dir, home, *, status, body):
    log_path = _install_mock_curl(bin_dir, status=status, body=body)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log_path


# --- codex-subscription ---


async def test_codex_command_sends_confirmed_wire_shape_and_rewrites_auth_file(tmp_path, monkeypatch, bin_dir):
    home = tmp_path / "home"
    auth_file = home / ".codex" / "auth.json"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text(json.dumps({"tokens": {"refresh_token": "old-refresh", "account_id": "keep-me"}}))
    auth_file.chmod(0o600)

    body = json.dumps({"access_token": "new-access", "refresh_token": "new-refresh"})
    log_path = _use_mock_curl(monkeypatch, bin_dir, home, status=200, body=body)

    value = await CredentialCache().get_value("codex-subscription", CODEX_COMMAND, ttl_seconds=0)

    assert value == "new-access"
    argv = _read_argv(log_path)
    assert "https://auth.openai.com/oauth/token" in argv
    assert "client_id=app_EMoamEEZ73f0CkXaXp7hrann" in argv
    assert "grant_type=refresh_token" in argv
    assert "refresh_token=old-refresh" in argv
    assert "scope=openid profile email" in argv
    assert "Content-Type: application/x-www-form-urlencoded" in argv
    assert "Accept: application/json" in argv

    rewritten = json.loads(auth_file.read_text())
    assert rewritten["tokens"]["refresh_token"] == "new-refresh"
    assert rewritten["tokens"]["access_token"] == "new-access"
    assert rewritten["tokens"]["account_id"] == "keep-me"  # untouched fields preserved
    assert oct(auth_file.stat().st_mode)[-3:] == "600"  # not silently widened by the rewrite


async def test_codex_command_missing_refresh_token_fails_without_network_call(tmp_path, monkeypatch, bin_dir):
    home = tmp_path / "home"
    auth_file = home / ".codex" / "auth.json"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text(json.dumps({"tokens": {}}))
    log_path = _use_mock_curl(monkeypatch, bin_dir, home, status=200, body="{}")

    with pytest.raises(CredentialExecutionError) as exc_info:
        await CredentialCache().get_value("codex-subscription", CODEX_COMMAND, ttl_seconds=0)

    assert exc_info.value.category == "exit"
    assert not log_path.exists()  # curl never invoked


async def test_codex_command_http_error_fails_without_stdout(tmp_path, monkeypatch, bin_dir):
    home = tmp_path / "home"
    auth_file = home / ".codex" / "auth.json"
    auth_file.parent.mkdir(parents=True)
    original = json.dumps({"tokens": {"refresh_token": "old-refresh"}})
    auth_file.write_text(original)
    _use_mock_curl(monkeypatch, bin_dir, home, status=400, body='{"error":"refresh_token_reused"}')

    with pytest.raises(CredentialExecutionError) as exc_info:
        await CredentialCache().get_value("codex-subscription", CODEX_COMMAND, ttl_seconds=0)

    assert exc_info.value.category == "exit"
    assert auth_file.read_text() == original  # never rewritten on failure


async def test_codex_command_response_missing_fields_fails(tmp_path, monkeypatch, bin_dir):
    home = tmp_path / "home"
    auth_file = home / ".codex" / "auth.json"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text(json.dumps({"tokens": {"refresh_token": "old-refresh"}}))
    _use_mock_curl(monkeypatch, bin_dir, home, status=200, body=json.dumps({"access_token": "x"}))

    with pytest.raises(CredentialExecutionError) as exc_info:
        await CredentialCache().get_value("codex-subscription", CODEX_COMMAND, ttl_seconds=0)

    assert exc_info.value.category == "exit"


# --- claude-code-subscription ---


async def test_claude_command_sends_confirmed_wire_shape_and_rewrites_credentials_file(tmp_path, monkeypatch, bin_dir):
    home = tmp_path / "home"
    creds_file = home / ".claude" / ".credentials.json"
    creds_file.parent.mkdir(parents=True)
    creds_file.write_text(json.dumps({"claudeAiOauth": {"refreshToken": "old-refresh", "subscriptionType": "keep-me"}}))
    creds_file.chmod(0o600)

    body = json.dumps({"access_token": "new-access", "refresh_token": "new-refresh"})
    log_path = _use_mock_curl(monkeypatch, bin_dir, home, status=200, body=body)

    value = await CredentialCache().get_value("claude-code-subscription", CLAUDE_COMMAND, ttl_seconds=0)

    assert value == "new-access"
    argv = _read_argv(log_path)
    assert "https://platform.claude.com/v1/oauth/token" in argv
    assert "Content-Type: application/json" in argv
    assert "User-Agent: axios/1.15.2" in argv
    body_arg = next(a for a in argv if a.startswith("{"))
    assert json.loads(body_arg) == {
        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        "grant_type": "refresh_token",
        "refresh_token": "old-refresh",
        "scope": "user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload",
    }

    rewritten = json.loads(creds_file.read_text())
    assert rewritten["claudeAiOauth"]["refreshToken"] == "new-refresh"
    assert rewritten["claudeAiOauth"]["accessToken"] == "new-access"
    assert rewritten["claudeAiOauth"]["subscriptionType"] == "keep-me"
    assert oct(creds_file.stat().st_mode)[-3:] == "600"


async def test_claude_command_preserves_refresh_token_when_response_omits_it(tmp_path, monkeypatch, bin_dir):
    # Confirmed by the research: rotation is optional, not guaranteed.
    home = tmp_path / "home"
    creds_file = home / ".claude" / ".credentials.json"
    creds_file.parent.mkdir(parents=True)
    creds_file.write_text(json.dumps({"claudeAiOauth": {"refreshToken": "old-refresh"}}))
    _use_mock_curl(monkeypatch, bin_dir, home, status=200, body=json.dumps({"access_token": "new-access"}))

    value = await CredentialCache().get_value("claude-code-subscription", CLAUDE_COMMAND, ttl_seconds=0)

    assert value == "new-access"
    rewritten = json.loads(creds_file.read_text())
    assert rewritten["claudeAiOauth"]["refreshToken"] == "old-refresh"


async def test_claude_command_missing_refresh_token_fails_without_network_call(tmp_path, monkeypatch, bin_dir):
    home = tmp_path / "home"
    creds_file = home / ".claude" / ".credentials.json"
    creds_file.parent.mkdir(parents=True)
    creds_file.write_text(json.dumps({"claudeAiOauth": {}}))
    log_path = _use_mock_curl(monkeypatch, bin_dir, home, status=200, body="{}")

    with pytest.raises(CredentialExecutionError) as exc_info:
        await CredentialCache().get_value("claude-code-subscription", CLAUDE_COMMAND, ttl_seconds=0)

    assert exc_info.value.category == "exit"
    assert not log_path.exists()


async def test_claude_command_response_missing_access_token_fails(tmp_path, monkeypatch, bin_dir):
    home = tmp_path / "home"
    creds_file = home / ".claude" / ".credentials.json"
    creds_file.parent.mkdir(parents=True)
    creds_file.write_text(json.dumps({"claudeAiOauth": {"refreshToken": "old-refresh"}}))
    _use_mock_curl(monkeypatch, bin_dir, home, status=200, body=json.dumps({"refresh_token": "new-refresh"}))

    with pytest.raises(CredentialExecutionError) as exc_info:
        await CredentialCache().get_value("claude-code-subscription", CLAUDE_COMMAND, ttl_seconds=0)

    assert exc_info.value.category == "exit"


async def test_claude_command_http_error_fails_without_rewrite(tmp_path, monkeypatch, bin_dir):
    home = tmp_path / "home"
    creds_file = home / ".claude" / ".credentials.json"
    creds_file.parent.mkdir(parents=True)
    original = json.dumps({"claudeAiOauth": {"refreshToken": "old-refresh"}})
    creds_file.write_text(original)
    _use_mock_curl(monkeypatch, bin_dir, home, status=401, body='{"error":"invalid_grant"}')

    with pytest.raises(CredentialExecutionError) as exc_info:
        await CredentialCache().get_value("claude-code-subscription", CLAUDE_COMMAND, ttl_seconds=0)

    assert exc_info.value.category == "exit"
    assert creds_file.read_text() == original


async def test_claude_command_handles_undecodable_response_without_crashing(tmp_path, monkeypatch, bin_dir):
    # Simulates Cloudflare honoring a compressed-encoding request anyway --
    # raw (here: garbage, standing in for gzip) bytes instead of JSON. jq
    # fails to parse it, and `set -euo pipefail` turns that into a clean
    # nonzero exit rather than a crash or a bogus printed token.
    home = tmp_path / "home"
    creds_file = home / ".claude" / ".credentials.json"
    creds_file.parent.mkdir(parents=True)
    creds_file.write_text(json.dumps({"claudeAiOauth": {"refreshToken": "old-refresh"}}))
    _use_mock_curl(monkeypatch, bin_dir, home, status=200, body="\x1f\x8b\x08not-real-gzip-but-not-json-either")

    with pytest.raises(CredentialExecutionError) as exc_info:
        await CredentialCache().get_value("claude-code-subscription", CLAUDE_COMMAND, ttl_seconds=0)

    assert exc_info.value.category == "exit"


def test_claude_command_does_not_advertise_compressed_encodings():
    # urllib/jq perform no Content-Encoding decompression -- advertising
    # gzip/br and having Cloudflare honor it breaks `jq` parsing on every
    # refresh (see test_claude_command_handles_undecodable_response_above).
    assert "--compressed" not in CLAUDE_COMMAND
    assert "Accept-Encoding" not in CLAUDE_COMMAND
