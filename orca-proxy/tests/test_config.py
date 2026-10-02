from orca_proxy import config


def test_tunnel_keys_live_in_the_data_dir_not_mitmproxys_confdir(tmp_path, monkeypatch):
    # mitmproxy writes its own wireguard.conf into confdir world-readable;
    # ours must be a separate file it never creates.
    monkeypatch.setenv("ORCA_PROXY_HOME", str(tmp_path))
    assert config.tunnel_keys_path() == tmp_path / "wireguard.json"
    assert config.mitm_confdir() not in config.tunnel_keys_path().parents
