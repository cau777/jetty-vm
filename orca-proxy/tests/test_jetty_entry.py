import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("jetty_entry", Path(__file__).parents[1] / "src" / "jetty_entry.py")
jetty_entry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jetty_entry)


def test_drops_the_bundle_library_path_when_the_host_had_none():
    environ = {"LD_LIBRARY_PATH": "/tmp/.mount_jetty/usr/lib/jetty/_internal", "PATH": "/usr/bin"}
    jetty_entry.restore_host_library_path(environ)
    assert environ == {"PATH": "/usr/bin"}


def test_restores_the_host_library_path():
    environ = {"LD_LIBRARY_PATH": "/tmp/.mount_jetty/usr/lib/jetty/_internal", "LD_LIBRARY_PATH_ORIG": "/opt/lib"}
    jetty_entry.restore_host_library_path(environ)
    assert environ == {"LD_LIBRARY_PATH": "/opt/lib"}
