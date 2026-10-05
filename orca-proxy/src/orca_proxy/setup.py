"""Install the downloaded AppImage for the current desktop user."""

from __future__ import annotations

import os
import getpass
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

APP_NAME = "jetty"
DAEMON_UNIT = "jetty-daemon.service"


def _appimage_source() -> Path:
    raw = os.environ.get("APPIMAGE")
    if raw:
        return Path(raw).expanduser().resolve()
    executable = Path(sys.argv[0]).expanduser().resolve()
    if executable.is_file() and executable.suffix.lower() == ".appimage":
        return executable
    raise RuntimeError("Run setup from the downloaded Jetty AppImage.")


def _quote_path(value: str) -> str:
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _copy_asset_if_present(source: Path, target: Path, *, executable: bool = False) -> None:
    if not source.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    if executable:
        target.chmod(0o755)


def _write_user_files(executable: Path) -> None:
    unit_dir = Path.home() / ".config/systemd/user"
    autostart_dir = Path.home() / ".config/autostart"
    applications_dir = Path.home() / ".local/share/applications"
    icon_dir = Path.home() / ".local/share/icons/hicolor/scalable/apps"
    for directory in (unit_dir, autostart_dir, applications_dir, icon_dir):
        directory.mkdir(parents=True, exist_ok=True)

    quoted_executable = _quote_path(str(executable))
    unit = (
        "# Written by Jetty; rerun `jetty setup` to update it.\n"
        "[Unit]\nDescription=Jetty management service\nAfter=default.target\n\n"
        "[Service]\nType=simple\n"
        f"ExecStart={quoted_executable} daemon\n"
        "Restart=on-failure\nRestartSec=5\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    (unit_dir / DAEMON_UNIT).write_text(unit, encoding="utf-8")

    desktop_exec = f"{_quote_path(str(executable))} tray"
    desktop = (
        "[Desktop Entry]\nType=Application\nName=Jetty\nComment=Manage Jetty virtual machines\n"
        f"Exec={desktop_exec}\nIcon=jetty\nTerminal=false\nCategories=Development;Utility;\n"
    )
    (applications_dir / "jetty.desktop").write_text(desktop, encoding="utf-8")
    (autostart_dir / "jetty.desktop").write_text(desktop + "X-GNOME-Autostart-enabled=true\n", encoding="utf-8")
    icon = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">'
        '<rect x="8" y="8" width="112" height="112" rx="28" fill="#12243a"/>'
        '<path d="M37 90V38h12v20l22-20h16L61 62l27 28H71L49 67v23z" fill="#67e8a5"/>'
        "</svg>\n"
    )
    (icon_dir / "jetty.svg").write_text(icon, encoding="utf-8")


def _run_user_service() -> None:
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", DAEMON_UNIT], check=True)


def _enable_linger() -> None:
    user = getpass.getuser()
    result = subprocess.run(
        ["loginctl", "enable-linger", user], capture_output=True, text=True, check=False
    )
    if result.returncode == 0:
        return
    try:
        _pkexec("/usr/bin/loginctl", "enable-linger", user)
    except (OSError, subprocess.CalledProcessError, RuntimeError):
        print("Jetty could not enable user-service lingering; the daemon may stop after logout.")


def _pkexec(program: str, *args: str) -> None:
    pkexec = shutil.which("pkexec")
    if not pkexec:
        raise RuntimeError("pkexec is required to install and initialize LXD.")
    command = shutil.which(program)
    if not command:
        command = program if Path(program).is_absolute() else None
    if not command:
        raise RuntimeError(f"Required program was not found: {program}")
    print(f"Requesting administrator authorization for: {Path(command).name} {' '.join(args)}")
    subprocess.run([pkexec, command, *args], check=True)


def _bootstrap_lxd() -> bool:
    """Install/init snap LXD and add the user to its root-equivalent group.

    Returns true when a new login session is needed for the group change.
    """
    user = getpass.getuser()
    snap = shutil.which("snap") or "/usr/bin/snap"
    installed = subprocess.run(
        [snap, "list", "lxd"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode == 0
    if not installed:
        _pkexec(snap, "install", "lxd")
    lxd = "/snap/bin/lxd"
    if not Path(lxd).exists():
        lxd = shutil.which("lxd") or lxd
    # LXD creates its socket before it has a default storage pool. Running the
    # idempotent auto-init also handles a preinstalled but uninitialized snap.
    _pkexec(lxd, "init", "--auto")
    groups = set(os.getgroups()) | {os.getgid()}
    try:
        import grp

        lxd_gid = grp.getgrnam("lxd").gr_gid
    except KeyError:
        _pkexec("/usr/sbin/groupadd", "--system", "lxd")
        import grp

        lxd_gid = grp.getgrnam("lxd").gr_gid
    if lxd_gid in groups:
        return False
    _pkexec("/usr/sbin/usermod", "-aG", "lxd", user)
    return True


def _install_skill() -> None:
    npx = shutil.which("npx")
    if not npx:
        print("The agent skill was not installed because npx is unavailable.")
        return
    if getattr(sys, "frozen", False):
        skill_root = Path(getattr(sys, "_MEIPASS")) / "orca_ssh_setup"
    else:
        skill_root = Path(__file__).resolve().parents[3] / "orca-ssh-setup"
    if not (skill_root / "SKILL.md").is_file():
        print("The bundled orca-ssh-setup skill was not found; install it from the Jetty repository.")
        return
    result = subprocess.run(
        [npx, "--yes", "skills", "add", str(skill_root), "--global", "--copy", "--yes"],
        check=False,
    )
    if result.returncode:
        print("The agent skill could not be installed automatically; install orca-ssh-setup from the Jetty repository.")


def _install_gh_helper() -> None:
    if getattr(sys, "frozen", False):
        source = Path(getattr(sys, "_MEIPASS")) / "orca_proxy" / "gh-rest.py"
    else:
        source = Path(__file__).resolve().parents[3] / "gh-rest" / "gh-rest.py"
    target = Path.home() / ".local/share/jetty/gh-rest.py"
    _copy_asset_if_present(source, target, executable=True)


def _install_catalog() -> None:
    if getattr(sys, "frozen", False):
        source = Path(getattr(sys, "_MEIPASS")) / "orca_proxy" / "static" / "quick-add-catalog.json"
    else:
        source = Path(__file__).resolve().parent / "static" / "quick-add-catalog.json"
    target = Path.home() / ".local/share/jetty/quick-add-catalog.json"
    _copy_asset_if_present(source, target)


def main(argv: list[str] | None = None) -> int:
    del argv
    if os.geteuid() == 0:
        print("Run Jetty setup as your desktop user, not as root.", file=sys.stderr)
        return 1
    try:
        source = _appimage_source()
        target = Path.home() / ".local/bin/jetty"
        target.parent.mkdir(parents=True, exist_ok=True)
        if source != target.resolve():
            fd, temp_name = tempfile.mkstemp(prefix=".jetty-install-", dir=target.parent)
            os.close(fd)
            temp = Path(temp_name)
            try:
                shutil.copyfile(source, temp)
                temp.chmod(0o755)
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
        else:
            target.chmod(0o755)
        _write_user_files(target)
        _enable_linger()
        _run_user_service()
        relogin_required = _bootstrap_lxd()
        _install_skill()
        _install_gh_helper()
        _install_catalog()
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"Jetty setup failed: {exc}", file=sys.stderr)
        return 1

    print(f"Jetty is installed at {target}.")
    if relogin_required:
        print(
            "Sign out and back in so Jetty can access the lxd group. If LXD is still unavailable afterward, "
            "reboot so the lingering user manager receives the new group membership."
        )
    else:
        print("Open Jetty from the tray to create the gateway and manage virtual machines.")
        try:
            subprocess.Popen([str(target), "tray"], close_fds=True, start_new_session=True)
        except OSError as exc:
            print(f"Jetty is installed; start the tray with `jetty tray`: {exc}")
    return 0
