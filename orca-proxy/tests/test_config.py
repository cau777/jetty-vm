from pathlib import Path

from orca_proxy import config


def test_firewall_sync_script_path_defaults_to_a_root_owned_fixed_location(tmp_path, monkeypatch):
    # Not anywhere under the data dir / a user's home -- that's the whole
    # point of the fix (see config.py's docstring and design.md's "Service
    # installation and firewall-rule lifecycle" section): this binary
    # carries CAP_NET_ADMIN/CAP_NET_RAW via setcap, tied to this exact
    # file's inode, so it must not resolve through anything the same
    # unprivileged user could overwrite.
    monkeypatch.delenv("ORCA_PROXY_FIREWALL_SCRIPT", raising=False)
    monkeypatch.setenv("ORCA_PROXY_HOME", str(tmp_path))
    path = config.firewall_sync_script_path()
    assert path == Path("/usr/local/sbin/orca-proxy-firewall-sync")
    assert str(tmp_path) not in str(path)
    assert not str(path).startswith(str(Path.home()))


def test_firewall_sync_script_path_respects_override(monkeypatch):
    monkeypatch.setenv("ORCA_PROXY_FIREWALL_SCRIPT", "/opt/custom/firewall-sync")
    assert config.firewall_sync_script_path() == Path("/opt/custom/firewall-sync")
