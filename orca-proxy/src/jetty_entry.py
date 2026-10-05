"""PyInstaller entry point that imports the CLI as a package module."""

import os
import sys


def restore_host_library_path(environ=os.environ) -> None:
    """Give child processes the host's library path back.

    PyInstaller points LD_LIBRARY_PATH at the bundled libraries for this
    process. The dynamic linker has already read it by now, so Jetty keeps its
    own libraries, but children inherit it: host programs such as curl in a
    Credential command then load the bundle's older libssl and fail to start.
    """
    original = environ.pop("LD_LIBRARY_PATH_ORIG", None)
    if original is None:
        environ.pop("LD_LIBRARY_PATH", None)
    else:
        environ["LD_LIBRARY_PATH"] = original


if getattr(sys, "frozen", False):
    restore_host_library_path()

from orca_proxy.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
