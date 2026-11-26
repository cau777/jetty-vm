"""Exercise the shipped keyring refresh command via CredentialCache, without login/network."""

import json
from pathlib import Path

import pytest

from orca_proxy.credential_exec import CredentialCache, CredentialExecutionError

CATALOG = Path(__file__).parent.parent / "src/orca_proxy/resources/quick-add-catalog.json"
COMMAND = next(x["command"] for x in json.loads(CATALOG.read_text()) if x["key"] == "antigravity-subscription")


@pytest.fixture
def host(tmp_path, monkeypatch):
    # /usr/bin/python3 loads these stand-ins in the real credential subprocess.
    (tmp_path / "dbus.py").write_text('''
import json, os
from pathlib import Path
root = Path(os.environ["ANTIGRAVITY_TEST_ROOT"])
class String(str):
    def __new__(cls, value, **kwargs): return super().__new__(cls, value)
ByteArray = bytes
class SessionBus:
    def get_object(self, service, path):
        assert service == "org.freedesktop.secrets"
        return self
    def OpenSession(self, algorithm, value):
        assert algorithm == "plain"
        return "", "/session"
    def SearchItems(self, attrs):
        assert attrs == {"service": "gemini", "username": "antigravity"}
        return (["/item"], []) if (root / "stored.json").exists() else ([], [])
    def GetSecret(self, session):
        return session, b"", (root / "stored.json").read_bytes(), "text/plain"
    def SetSecret(self, value):
        (root / "stored.json").write_bytes(value[2])
def Interface(obj, interface): return obj
''')
    (tmp_path / "sitecustomize.py").write_text('''
import io, json, os, urllib.request, urllib.error
from pathlib import Path
root = Path(os.environ["ANTIGRAVITY_TEST_ROOT"])
def urlopen(request, timeout):
    assert request.full_url == "https://oauth2.googleapis.com/token"
    assert timeout == 15
    (root / "wire.txt").write_bytes(request.data)
    status = int((root / "status").read_text())
    if status != 200:
        raise urllib.error.HTTPError(request.full_url, status, "failure", {}, None)
    return io.BytesIO((root / "response.json").read_bytes())
urllib.request.urlopen = urlopen
''')
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("ANTIGRAVITY_TEST_ROOT", str(tmp_path))
    monkeypatch.setenv("ORCA_PROXY_HOME", str(tmp_path))
    (tmp_path / "antigravity-oauth-client.json").write_text(json.dumps({
        "client_id": "test-client-id", "client_secret": "test-client-secret",
    }))
    (tmp_path / "stored.json").write_text(json.dumps({
        "token": {"refresh_token": "old-refresh", "access_token": "old-access"},
        "auth_method": "consumer", "id_token_email": "keep@example.invalid",
    }))
    (tmp_path / "response.json").write_text(json.dumps({"access_token": "new-access", "expires_in": 3600}))
    (tmp_path / "status").write_text("200")
    return tmp_path


@pytest.mark.parametrize("rotate", [False, True])
async def test_refresh_preserves_metadata_and_handles_optional_rotation(host, rotate):
    from urllib.parse import parse_qs

    if rotate:
        (host / "response.json").write_text(json.dumps({
            "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600,
        }))
    value = await CredentialCache().get_value("antigravity", COMMAND, ttl_seconds=0)
    assert value == "new-access"
    wire = parse_qs((host / "wire.txt").read_text())
    assert wire["grant_type"] == ["refresh_token"]
    assert wire["refresh_token"] == ["old-refresh"]
    assert wire["client_id"] == ["test-client-id"]
    assert wire["client_secret"] == ["test-client-secret"]
    stored = json.loads((host / "stored.json").read_text())
    assert stored["id_token_email"] == "keep@example.invalid"
    assert stored["auth_method"] == "consumer"
    assert stored["token"]["access_token"] == value
    assert stored["token"]["refresh_token"] == ("new-refresh" if rotate else "old-refresh")
    from datetime import datetime, timezone
    expiry = datetime.fromisoformat(stored["token"]["expiry"])
    assert 3500 < (expiry - datetime.now(timezone.utc)).total_seconds() <= 3600


@pytest.mark.parametrize("failure", ["no-login", "locked", "no-refresh", "wrong-method", "no-client-config", "http", "no-access", "short-life", "malformed"])
async def test_failure_does_not_replace_keyring_or_print_secrets(host, failure):
    stored_path = host / "stored.json"
    if failure == "no-login":
        stored_path.unlink()
    elif failure == "locked":
        p = host / "dbus.py"
        p.write_text(p.read_text().replace('return (["/item"], []) if', 'return ([], ["/item"]) if'))
    elif failure == "no-refresh":
        stored_path.write_text('{"token":{},"auth_method":"consumer"}')
    elif failure == "wrong-method":
        stored_path.write_text('{"token":{"refresh_token":"secret"},"auth_method":"enterprise"}')
    elif failure == "no-client-config":
        (host / "antigravity-oauth-client.json").unlink()
    elif failure == "http":
        (host / "status").write_text("401")
    elif failure == "no-access":
        (host / "response.json").write_text('{"expires_in":3600}')
    elif failure == "short-life":
        (host / "response.json").write_text('{"access_token":"bad","expires_in":30}')
    elif failure == "malformed":
        (host / "response.json").write_text('not JSON')
    before = stored_path.read_bytes() if stored_path.exists() else None
    with pytest.raises(CredentialExecutionError) as error:
        await CredentialCache().get_value("antigravity", COMMAND, ttl_seconds=0)
    assert error.value.category == "exit"
    assert (stored_path.read_bytes() if stored_path.exists() else None) == before
    if failure in {"no-login", "locked", "no-refresh", "wrong-method", "no-client-config"}:
        assert not (host / "wire.txt").exists()
    assert "old-refresh" not in error.value.detail
    assert "old-access" not in error.value.detail
