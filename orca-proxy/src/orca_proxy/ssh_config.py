"""Manage Jetty's generated OpenSSH config fragment without replacing user config."""

from __future__ import annotations

import os
import re
import shlex
import tempfile
from pathlib import Path

_HOST_OR_MATCH = re.compile(r"^\s*(?:Host|Match)\s+", re.IGNORECASE)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_jetty_config(fragment: str, ssh_dir: Path | None = None) -> Path:
    """Write the generated fragment and include it before user host blocks."""
    directory = ssh_dir or (Path.home() / ".ssh")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    fragment_path = directory / "jetty_config"
    config_path = directory / "config"
    _atomic_write(fragment_path, fragment.rstrip() + "\n")

    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    has_global_include = False
    for line in existing.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _HOST_OR_MATCH.match(line):
            break
        try:
            directive = shlex.split(line)
        except ValueError:
            continue
        if directive and directive[0].lower() == "include":
            if any(Path(pattern).name == "jetty_config" for pattern in directive[1:]):
                has_global_include = True
                break
    if not has_global_include:
        prefix = "Include jetty_config\n"
        if existing:
            prefix += "\n" if not existing.startswith("\n") else ""
        existing = prefix + existing
        _atomic_write(config_path, existing)
    elif config_path.exists():
        os.chmod(config_path, 0o600)
    return fragment_path
