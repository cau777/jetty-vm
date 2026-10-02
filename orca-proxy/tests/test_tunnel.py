import json
import logging

import mitmproxy_rs

from orca_proxy import tunnel


def test_ensure_keys_creates_owner_only_valid_keys(tmp_path):
    path = tmp_path / "wireguard.json"
    keys = tunnel.ensure_keys(path)
    assert path.stat().st_mode & 0o777 == 0o600
    mitmproxy_rs.pubkey(keys["server_key"])
    mitmproxy_rs.pubkey(keys["client_key"])
    assert tunnel.keys_ok(path)


def test_ensure_keys_keeps_existing_keys_and_tightens_mode(tmp_path):
    path = tmp_path / "wireguard.json"
    first = tunnel.ensure_keys(path)
    path.chmod(0o664)
    assert not tunnel.keys_ok(path)
    assert tunnel.ensure_keys(path) == first
    assert tunnel.keys_ok(path)


def test_keys_ok_false_when_missing_or_malformed(tmp_path):
    path = tmp_path / "wireguard.json"
    assert not tunnel.keys_ok(path)
    path.write_text(json.dumps({"server_key": "nope", "client_key": "nope"}))
    path.chmod(0o600)
    assert not tunnel.keys_ok(path)


def test_gateway_material_has_client_private_and_server_public_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ORCA_PROXY_HOME", str(tmp_path))
    assert tunnel.main(["gateway"]) == 0
    out = json.loads(capsys.readouterr().out)
    keys = json.loads((tmp_path / "wireguard.json").read_text())
    assert out == {"client_key": keys["client_key"], "server_public_key": mitmproxy_rs.pubkey(keys["server_key"])}


def test_log_filter_drops_mitmproxys_client_config_line(caplog):
    tunnel.install_log_filter()
    logger = logging.getLogger("mitmproxy.proxy.mode_servers")
    with caplog.at_level(logging.INFO, logger="mitmproxy.proxy.mode_servers"):
        logger.info("[Interface]\nPrivateKey = abc\n")
        logger.info("WireGuard server listening")
    assert [r.getMessage() for r in caplog.records] == ["WireGuard server listening"]
