"""PROTOTYPE — size-measurement entry: the whole daemon/CLI plus the QML UI."""

import sys

import orca_proxy.cli  # noqa: F401  (pulls in the daemon, mitmproxy and every proxy dependency)
from orca_proxy.tray_qml_prototype.__main__ import main

if __name__ == "__main__":
    if sys.argv[1:2] == ["ui"]:
        sys.argv.pop(1)
        raise SystemExit(main())
    raise SystemExit(orca_proxy.cli.main())
