"""PyInstaller entry point that imports the CLI as a package module."""

from orca_proxy.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
