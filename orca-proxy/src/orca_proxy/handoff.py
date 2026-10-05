"""Hand a freshly created VM to a coding agent in a host terminal.

The daemon runs under the systemd user manager, whose PATH usually lacks the
user's node/nvm setup, so the skill install and the agent launch both run in
the user's interactive login shell inside the new terminal window.
"""

from __future__ import annotations

import hashlib
import os
import pwd
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .errors import ServiceUnavailable, ValidationFailed

SKILL_NAME = "orca-ssh-setup"


@dataclass(frozen=True)
class Agent:
    label: str
    skills_id: str  # the `skills` CLI's --agent value
    command: tuple[str, ...]  # the prompt is appended as the last argument
    skill_dirs: tuple[str, ...]  # global skill dirs this agent reads, relative to $HOME


AGENTS = {
    "claude": Agent("Claude Code", "claude-code", ("claude",), (".claude/skills",)),
    "codex": Agent("Codex", "codex", ("codex",), (".codex/skills", ".agents/skills")),
    "pi": Agent("Pi", "pi", ("pi",), (".pi/agent/skills",)),
    "opencode": Agent(
        "OpenCode", "opencode", ("opencode", "--prompt"), (".config/opencode/skills", ".agents/skills")
    ),
}

# (executable, arguments placed before the command to run)
_TERMINALS = (
    ("ptyxis", ("--",)),
    ("gnome-terminal", ("--",)),
    ("kgx", ("--",)),
    ("konsole", ("-e",)),
    ("xfce4-terminal", ("-x",)),
    ("kitty", ()),
    ("alacritty", ("-e",)),
    ("wezterm", ("start", "--")),
    ("x-terminal-emulator", ("-e",)),
)

# $1 project dir, $2 install flag, $3 skill source, $4 skills agent id, $5... agent command
_SCRIPT = r"""
cd "$1" || { echo "Cannot open $1"; read -r _; exit 1; }
if [ "$2" = 1 ]; then
  echo "Installing the orca-ssh-setup skill for this agent..."
  if ! npx --yes skills add "$3" --global --agent "$4" --copy --yes; then
    echo "The skill could not be installed. Install orca-ssh-setup from the Jetty repository."
    printf "Press Enter to start the agent anyway..."; read -r _
  fi
fi
shift 4
if ! command -v "$1" >/dev/null 2>&1; then
  echo "$1 is not installed or not on PATH."
  printf "Press Enter to close..."; read -r _
  exit 1
fi
exec "$@"
"""


def _bundled_skill() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "orca_ssh_setup"
    return Path(__file__).resolve().parents[3] / SKILL_NAME


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _stable_skill_copy() -> Path:
    """Copy the bundled skill out of the AppImage mount, which changes per run."""
    source = _bundled_skill()
    if not (source / "SKILL.md").is_file():
        raise ServiceUnavailable("The bundled orca-ssh-setup skill was not found")
    target = Path.home() / ".local/share/jetty" / SKILL_NAME
    if target.is_dir() and _tree_digest(target) == _tree_digest(source):
        return target
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target)
    return target


def skill_current(agent: Agent, skill: Path, home: Path | None = None) -> bool:
    home = home or Path.home()
    expected = _tree_digest(skill)
    for directory in agent.skill_dirs:
        installed = home / directory / SKILL_NAME
        if (installed / "SKILL.md").is_file() and _tree_digest(installed) == expected:
            return True
    return False


def prompt(vm_name: str, ip_address: str) -> str:
    return (
        f"Use the orca-ssh-setup skill to prepare the Jetty VM '{vm_name}' for the project in this "
        f"directory. The Jetty app already created and started it (address {ip_address}, SSH host "
        f"alias {vm_name}), so do not create a new VM: confirm it with `jetty vm list` and continue "
        "from there. Start with step 1."
    )


def _find_terminal() -> tuple[str, tuple[str, ...]]:
    for name, prefix in _TERMINALS:
        path = shutil.which(name)
        if path:
            return path, prefix
    raise ServiceUnavailable("No supported terminal emulator was found")


def _login_shell() -> str:
    shell = os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell
    # Only shells whose `-c script name args...` sets $1.. the POSIX way.
    if shell and Path(shell).name in {"bash", "zsh"} and Path(shell).is_file():
        return shell
    return shutil.which("bash") or "/bin/bash"


def project_directory(raw: object) -> Path:
    if raw in (None, ""):
        return Path.home()
    if not isinstance(raw, str):
        raise ValidationFailed("project_dir must be a string", fields={"project_dir": "invalid value"})
    path = Path(raw).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise ValidationFailed(
            "project_dir must be an existing absolute folder", fields={"project_dir": "folder not found"}
        )
    return path.resolve()


def launch(agent_key: str, vm_name: str, ip_address: str, project_dir: Path) -> dict:
    agent = AGENTS.get(agent_key)
    if agent is None:
        raise ValidationFailed(
            f"agent must be one of: {', '.join(AGENTS)}", fields={"agent": "unsupported agent"}
        )
    skill = _stable_skill_copy()
    install = not skill_current(agent, skill)
    terminal, prefix = _find_terminal()
    shell_command = [
        _login_shell(), "-lic", _SCRIPT, "jetty-handoff",
        str(project_dir), "1" if install else "0", str(skill), agent.skills_id,
        *agent.command, prompt(vm_name, ip_address),
    ]
    command = [terminal, *prefix, *shell_command]
    systemd_run = shutil.which("systemd-run")
    if systemd_run:
        # A transient unit keeps the terminal alive across daemon restarts.
        command = [
            systemd_run, "--user", "--collect", "--quiet",
            f"--unit=jetty-handoff-{secrets.token_hex(4)}", f"--working-directory={project_dir}",
            *command,
        ]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=15, cwd=project_dir)
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", b"") or b""
        message = detail.decode(errors="replace").strip() or str(exc)
        raise ServiceUnavailable(f"Could not open a terminal: {message}") from exc
    return {
        "agent": agent_key,
        "label": agent.label,
        "skill_install": install,
        "terminal": Path(terminal).name,
        "project_dir": str(project_dir),
    }
