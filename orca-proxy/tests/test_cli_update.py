from orca_proxy import cli


def _process(proc, pid, *argv):
    entry = proc / str(pid)
    entry.mkdir()
    (entry / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")


def test_tray_pids_match_appimage_runtime_and_frozen_tray(tmp_path):
    _process(tmp_path, 101, "/home/user/.local/bin/jetty", "tray")
    _process(tmp_path, 102, "/tmp/.mount_jettyAbc/usr/lib/jetty/jetty", "tray")
    _process(tmp_path, 103, "/home/user/.local/bin/jetty", "daemon")
    _process(tmp_path, 104, "/usr/bin/python3", "tray")
    (tmp_path / "self").mkdir()

    assert sorted(cli._tray_pids(tmp_path)) == [101, 102]


def test_restart_tray_does_nothing_without_running_tray(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_tray_pids", lambda: [])
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError))

    assert cli._restart_tray(tmp_path / "jetty") is False
