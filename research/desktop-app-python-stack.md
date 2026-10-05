# Jetty desktop app: an all-Python stack (PySide6 + QtWebEngine, embedded mitmproxy, AppImage)

**Date:** 2026-10-05
**Method:** Primary-source only.
- **mitmproxy**: version `10.4.2` is the version `orca-proxy/uv.lock` pins. Its installed source was read in
  `orca-proxy/.venv/lib/python3.13/site-packages/`. The upstream repo was cloned at tag `v10.4.2` (commit
  `537908f5c7b323235fd59708865a9403caf6e419`) for `release/`. Paths below are relative to those roots.
- **mitmproxy_rs**: `0.6.3`, from the same lockfile and venv (`mitmproxy_rs-0.6.3.dist-info`).
- **Qt**: `qtbase` source on the `6.8` branch (`src/gui/platform/unix/qgenericunixthemes.cpp`,
  `src/gui/platform/unix/dbustray/qdbustrayicon.cpp` and `src/widgets/util/qsystemtrayicon_qpa.cpp`), fetched raw
  from GitHub. Also the `doc.qt.io` QSystemTrayIcon page and the Qt WebEngine Platform Notes.
- **PySide6**: PyPI JSON API (`6.11.2`). The `PySide6-Addons` manylinux x86_64 wheel was downloaded and measured
  with `zipfile`.
- **AppImage**: `docs.appimage.org`. `AppImage/type2-runtime` was cloned at commit `8f39b89e` (2026-09-28) and
  `src/runtime/runtime.c` read directly.
- **Nuitka**: cloned at commit `e1963dd6` (2026-10-05). Read `nuitka/options/OptionParsing.py` and `Options.py`.
- **PyInstaller**: `pyinstaller.org/en/stable` (operating-mode, usage, runtime-information, license pages).
- **LXD**: `canonical.com/lxd/docs/latest` (installing, REST API) and `canonical/lxd` `main`
  (`client/connection.go`, `doc/rest-api.yaml`). The snap socket was also checked on this host (LXD snap
  `5.21.8`, Ubuntu 26.04.1).
- **Ubuntu**: 24.04 release notes, the Ubuntu 23.10 user-namespace blog post, and `packages.ubuntu.com`.
- **Tauri**: `v2.tauri.app` (prerequisites, sidecar). **pywebview**: `pywebview.flowrl.com`.
- **Local man pages**: `pkexec(1)`, `systemd.special(7)`, `systemd-xdg-autostart-generator(8)` and `loginctl(1)`.
- **Repo**: this repo at `0191709`.

Every claim below is followed by its source. Anything not confirmed from a primary source is marked
**unverified** or **inference**. §6 lists where the sources corrected the design conversation that led to this
document.

---

## 1. Motivation

Today Jetty installs with `curl … | bash` (`README.md`). That puts a uv-managed venv under
`~/.local/share/jetty/releases/<version>/`, starts `mitmdump` as a systemd user unit (`install.sh:175-190`), and
leaves LXD provisioning to a 258-line bash script (`orca-proxy/deploy/jetty-lxd`), which the agent drives through
the `orca-ssh-setup` skill. The flow we want instead:

1. The user downloads **one executable**.
2. It installs a **service that starts on login**.
3. The service is visible as a **system tray icon**.
4. Clicking the tray icon opens a **native GUI window**.
5. **The app, not the agent, handles the deterministic setup**: installing LXD, creating the Jetty network and
   gateway VM, creating project VMs, SSH keys and `~/.ssh/config`.
6. The **agent is invoked afterwards** to finish the parts that need judgement: reading the repo, confirming
   sizes and harnesses, and installing the toolchain.

A single executable is strongly preferred over a multi-step install.

## 2. Decision

**Stay all-Python.** One frozen executable contains the proxy, the daemon, the tray icon and the GUI. The GUI is
**PySide6 + QtWebEngine showing the existing `orca-proxy` web UI**.

### 2.1 Process model: one binary, several modes

| Mode | Started by | Responsibility |
|---|---|---|
| `jetty daemon` | systemd user unit (`~/.config/systemd/user/`) | mitmproxy in-process, the management API, LXD orchestration, SSH config generation. Never imports Qt. |
| `jetty tray` | XDG autostart entry (`~/.config/autostart/jetty.desktop`) | Tray icon, plus a QtWebEngine window pointed at the daemon's loopback API. |
| `jetty setup` | first launch | Copies itself to a stable path, writes the unit and autostart entry, and runs the privileged steps through `pkexec`. |
| `jetty vm …`, `jetty status`, `jetty tunnel gateway` | agent, user, gateway provisioning | CLI over the same API. Replaces direct `lxc` calls in the skill and `python -m orca_proxy.tunnel gateway` in `jetty-lxd`. |

**Why the daemon and the tray are separate processes:**
- The daemon must run without a desktop. User services sit under the per-user systemd manager.
  `loginctl enable-linger` even keeps that manager running "after logouts" (`loginctl(1)`).
- A tray icon only makes sense inside a graphical session. XDG autostart entries run "during startup of the
  user's desktop environment after the user has logged in" (freedesktop Autostart spec). On systemd-based
  desktops these entries become `.service` units through `systemd-xdg-autostart-generator(8)`, tied to
  `graphical-session.target`, which "is active whenever any graphical session is running"
  (`systemd.special(7)`).
- The tray exists to show whether the proxy is running, and a process cannot report its own death. If they
  shared a process, a proxy crash would remove the icon, which looks the same as "never started" or "the
  desktop's tray isn't showing it". As a separate process, the tray polls the daemon's loopback API and stays
  up to show the failure.
- The proxy is the agent VMs' only web egress. The tray process carries QtWebEngine (Chromium), the component
  most likely to crash or leak, so keeping it out of the daemon means a GUI fault never cuts the VMs off.
- systemd restarts the daemon (`Restart=`) and keeps its logs in the journal. The tray can be quit without
  stopping the proxy. No bridge between Qt's event loop and mitmproxy's asyncio loop (`qasync` or a proxy
  thread) is needed.

**Tray states** (decided; derived from the daemon's status endpoint and `/readyz`):

| Icon | Meaning | Menu actions |
|---|---|---|
| Green | Daemon reachable, proxy running, `/readyz` passes | Open Jetty, Quit tray |
| Grey | Daemon reachable but `not_configured` / `waiting_for_network` (§2.2) | Open setup |
| Red | Daemon unreachable, or `/readyz` fails | Restart proxy (`systemctl --user restart`), Show logs, Open Jetty |

A single process that runs both, as a user unit bound to `graphical-session.target`, was considered and
rejected. It would get systemd restarts back, but the icon could still not show that the proxy had crashed,
and showing that is the reason to have the tray.

**The UI needs no changes to work in the window.** The daemon already serves it: `create_app()` registers
`GET /` → `static/index.html` plus `add_static("/", static_dir)` (`orca-proxy/src/orca_proxy/app.py:75-86`) on
`127.0.0.1:$ORCA_PROXY_MANAGEMENT_PORT`, default 8080 (`config.py:26-27`). The tray window loads that URL, so the
browser UI and the app window are the same code. The setup wizard is built as more pages of the same UI.

### 2.2 The daemon needs a "not configured" state

The WireGuard listener binds `10.201.0.1:51820` (`install.sh:185`). That is the host's address on `jettyup0`,
the uplink bridge `jetty-lxd setup` creates (`deploy/jetty-lxd:31-33`). Today systemd covers the gap:
"until it exists the bind fails and Restart= retries" (`install.sh:183-184`, `Restart=always`,
`RestartSec=5`). The installer prints that the service "starts once 'jetty-lxd setup' creates the Jetty network"
(`install.sh:83-87`).

In the app, the GUI and the setup wizard must be reachable **before** that network exists. So the daemon starts
its API and UI unconditionally, reports `not_configured` or `waiting_for_network` on a status endpoint, and only
starts the mitmproxy WireGuard mode once the bridge address is present. Restart-looping the whole daemon would
take the GUI down with it. (Inference from the cited lines; no new source.)

### 2.3 Running mitmproxy inside the daemon (no `mitmdump -s`)

mitmproxy 10.4.2's `DumpMaster` is a small subclass of `Master` (`mitmproxy/tools/dump.py`):

```python
class DumpMaster(master.Master):
    def __init__(self, options, loop=None, with_termlog=True, with_dumper=True) -> None:
        super().__init__(options, event_loop=loop, with_termlog=with_termlog)
        self.addons.add(*addons.default_addons())
        if with_dumper:
            self.addons.add(dumper.Dumper())
        self.addons.add(keepserving.KeepServing(), readfile.ReadFileStdin(), errorcheck.ErrorCheck())
```

The parts that matter for embedding:
- **It must be constructed inside a running event loop.** `Master.__init__` does
  `self.event_loop = event_loop or asyncio.get_running_loop()`, with the comment "We expect an active event loop
  here already because some addons may want to spawn tasks during the initial configuration phase"
  (`mitmproxy/master.py:44-47`).
- **`AddonManager.add` runs the addon's `load` event immediately.** Its docstring: "Add addons to the end of the
  chain, and run their load event" (`mitmproxy/addonmanager.py:198-204`). So `OrcaProxyAddon.load()` (key file
  and SQLite setup) runs at `add()` time.
- **`master.run()` is a coroutine.** `async def run(self)` (`master.py:54`). `master.shutdown()` is
  "thread-safe" and sets `should_exit` (`master.py:92-97`). The `running` and `done` hooks fire through
  `Master.running()` and `Master.done()` (`master.py:99-106`), so the existing `running()`/`done()` handlers
  that start and stop the aiohttp API keep working unchanged.
- **The WireGuard mode is set through `mode`.** It is declared as `Sequence[str]` with default `["regular"]`.
  The docstring lists `"wireguard[:PATH]"` and the `@listen_host:listen_port` suffix (`mitmproxy/options.py:107-119`).
  `confdir` is a core option as well (`options.py:36`), so `Options(mode=[...], confdir=...)` is accepted at
  construction.

The resulting shape:

```python
async def run_proxy(keys_path, confdir):
    tunnel.install_log_filter()           # still required: mitmproxy logs the client private key
    tunnel.ensure_keys(keys_path)         # replaces the unit's ExecStartPre
    opts = Options(mode=[f"wireguard:{keys_path}@10.201.0.1:51820"], confdir=str(confdir))
    master = DumpMaster(opts, with_termlog=True, with_dumper=False)
    master.addons.add(OrcaProxyAddon())   # runs load() now
    await master.run()
```

This removes the "absolute imports, not package-relative" workaround that `proxy_addon.py` documents for
`mitmdump -s` script loading (`orca-proxy/src/orca_proxy/proxy_addon.py:32-37`).

`install_log_filter` still matters in-process. It filters the `mitmproxy.proxy.mode_servers` logger
(`orca_proxy/tunnel.py:57-58`), and embedding does not change what mitmproxy logs.

### 2.4 GUI: PySide6 + QtWebEngine, with QSystemTrayIcon

**How the tray icon works on Linux.** The QSystemTrayIcon docs list Linux support as "All Linux desktop
environments that implement the D-Bus StatusNotifierItem specification, including KDE, Gnome, Xfce, LXQt, and
DDE", plus "window managers … for X11 that implement the freedesktop.org XEmbed system tray specification". They
add that GNOME Shell 3.26+ may not support all activation reasons without shell extensions
(doc.qt.io/qt-6/qsystemtrayicon.html). From the qtbase 6.8 source:
- **The D-Bus tray is chosen by `shouldUseDBusTray()`.** On any non-`xcb` platform (Wayland) it always returns
  true, with the comment "There's no other tray implementation to fallback to on non-X11". On X11 it returns true
  only if `QDBusMenuConnection().isWatcherRegistered()` (`qgenericunixthemes.cpp:80-90`). The GNOME, KDE and
  generic themes all return `new QDBusTrayIcon()` when it is true (`:477-482`, `:1271-1276`, `:1468-1473`).
- **Availability means a watcher is registered.** `QDBusTrayIcon::isSystemTrayAvailable()` returns
  `conn->isWatcherRegistered()`, with the comment "If the KDE watcher service is registered, we must be on a
  desktop where a StatusNotifier-conforming system tray exists" (`qdbustrayicon.cpp:335-343`). The watcher
  service is `org.kde.StatusNotifierWatcher` (`:71`). `QSystemTrayIcon::isSystemTrayAvailable()` delegates to
  this (`qsystemtrayicon_qpa.cpp:79-86`).

So on a GNOME desktop with no SNI host (no AppIndicator extension), `isSystemTrayAvailable()` is false.
`jetty tray` should then open the window directly, and a regular `.desktop` launcher stays as the way back in.

**What QtWebEngine ships.** Measured from the `pyside6_addons-6.11.2-cp310-abi3-manylinux_2_34_x86_64.whl` wheel
(PyPI):
- The wheel is 175.1 MB and unpacks to 438 MB.
- WebEngine-related files unpack to **≈266 MB** (≈112 MB compressed). The largest are:
  - `libQt6WebEngineCore.so.6`: 203.8 MB
  - `qtwebengine_devtools_resources.pak`: 11.7 MB
  - `icudtl.dat`: 10.5 MB
  - per-locale `.pak` files: 1–1.6 MB each
- The helper `PySide6/Qt/libexec/QtWebEngineProcess` is in the same wheel.

`pyside6_essentials` (QtCore/Gui/Widgets and others) is a separate 80.1 MB wheel. **Inference:** devtools
resources and unused locales are candidates to exclude at freeze time.

**Two consequences of the PySide6 wheel tags:**
- The tag is `manylinux_2_34`, so the bundle needs **glibc ≥ 2.34** on the target. Ubuntu 22.04 ships
  `libc6 2.35` (packages.ubuntu.com/jammy/libc6), so 22.04 is the oldest Ubuntu LTS this stack can support. See
  §2.5 for building there.
- PySide6, PySide6-Essentials and PySide6-Addons are all licensed "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only"
  (PyPI metadata). Under LGPLv3, Qt's own obligations page says:
  - "The user is allowed to change and re-link the library used in the application".
  - The complete corresponding library source (or a written offer) must be provided.
  - Users must be notified with "a copy of the LGPL license text" and "a prominent notice".
  - Static linking risks the application no longer being "work that uses the library"
    (qt.io/licensing/open-source-lgpl-obligations).

  PyInstaller in onedir mode keeps Qt as separate shared libraries inside the bundle, which preserves
  relinkability (**inference**). PyInstaller itself imposes nothing: bundles "can be shipped with whatever
  license you want, as long as it complies with the licenses of your dependencies" (pyinstaller.org license
  page).

**Risk: QtWebEngine's sandbox vs Ubuntu's user-namespace restriction.**
- Qt WebEngine's Linux sandbox needs "the anonymous namespaces feature". On Ubuntu with AppArmor, administrators
  "may need to adjust `/proc/sys/kernel/apparmor_restrict_unprivileged_userns` to 0 or implement custom AppArmor
  profiles". The sandbox can be disabled with `QTWEBENGINE_DISABLE_SANDBOX=1`, `--no-sandbox`, or
  `QTWEBENGINE_CHROMIUM_FLAGS=--no-sandbox` (Qt WebEngine Platform Notes).
- Ubuntu 24.04 turned that restriction on by default: "the Ubuntu kernel now restricts the use of unprivileged
  user namespaces" (Ubuntu 24.04 release notes). The opt-out is an AppArmor profile containing a `userns,` rule
  (ubuntu.com blog, "Restricted unprivileged user namespaces", 23.10).
- An AppImage mounts at a fresh `/tmp/.mount_*` path on each launch (see `$APPDIR` in §2.5), which makes a
  path-based AppArmor profile awkward.
- **Unverified:** whether QtWebEngine 6.11 actually fails to start, or degrades, under the 24.04 default. This
  needs an empty-VM test.
- **Options:**
  - (a) Set `QTWEBENGINE_DISABLE_SANDBOX=1`. The window only ever loads Jetty's own loopback UI, never arbitrary
    web content, so this is a defensible but conscious trade-off.
  - (b) Install an AppArmor profile during the `pkexec` setup step.

### 2.5 Packaging: PyInstaller onedir inside an AppImage

**Why PyInstaller.**
- **mitmproxy builds its own Linux standalone binaries with it.** `release/build.py`'s `standalone_binaries`
  command is documented as "Windows and Linux: Build the standalone binaries generated with PyInstaller" and runs
  `_pyinstaller("standalone.spec")` (`release/build.py:136-150`). `release/specs/standalone.spec` builds
  `mitmproxy`, `mitmdump` and `mitmweb` as single `EXE`s with `a.binaries`/`a.datas` embedded, which is onefile
  mode. For `mitmdump` it passes the `unbuffered` option, referencing issue #6757.
- **mitmproxy and mitmproxy_rs ship their own PyInstaller hooks**, registered through `[pyinstaller40] hook-dirs`
  entry points (`mitmproxy-10.4.2.dist-info/entry_points.txt`, `mitmproxy_rs-0.6.3.dist-info/entry_points.txt`):
  - `mitmproxy/utils/pyinstaller/hook-mitmproxy.py` adds `hiddenimports = ["mitmproxy.script"]`.
  - `mitmproxy_rs/_pyinstaller/hook-mitmproxy_rs.py` collects data files and adds `mitmproxy_macos` or
    `mitmproxy_windows` as hidden imports **only** when `sys.platform` is `darwin` or `win32`.

**Why onedir, wrapped in an AppImage, instead of onefile.**
- PyInstaller onefile "creates a temporary folder … named `_MEIxxxxxx`" on every start, decompresses into it
  ("a one-file app is a little slower to start"), and deletes it afterwards. Also, "It is *much* easier to
  diagnose problems in one-folder mode" (pyinstaller.org operating-mode page).
- With two processes started at login (daemon and tray), each would unpack about 0.5 GB of Qt every time.
- An AppImage mounts its SquashFS instead. `$APPDIR` is the "Path of mountpoint of the SquashFS image", and
  `$APPIMAGE` is the "(Absolute) path to AppImage file (with symlinks resolved)" (docs.appimage.org,
  environment variables). The type-2 runtime sets `APPIMAGE` itself (`type2-runtime/src/runtime/runtime.c:1642`,
  `:1833`).

**FUSE.**
- The classic runtime needs `libfuse2`. On Ubuntu 22.04+ users must `apt install libfuse2`, renamed `libfuse2t64`
  in 24.04 (docs.appimage.org troubleshooting/fuse).
- The current `AppImage/type2-runtime` is statically linked: "libfuse2 is no longer required on the target
  system" (its README). It still locates a host `fusermount*` binary on `$PATH` (`find_fusermount()`,
  `runtime.c:417-455`, which matches any entry starting with `fusermount`, so `fusermount3` works).
- On Ubuntu this binary is a given for Jetty users. `snapd` declares `Depends: fuse3`
  (`apt-cache depends snapd`; packages.ubuntu.com lists `fuse3` for both jammy and noble), and `fuse3` ships
  `/bin/fusermount3` (packages.ubuntu.com noble fuse3 filelist). LXD is installed as a snap, so snapd is always
  present.
- Fallback without FUSE: `--appimage-extract-and-run` or `APPIMAGE_EXTRACT_AND_RUN=1` (docs.appimage.org;
  `runtime.c:1580`).

**A stable install path.** `jetty setup` copies `$APPIMAGE` to `~/.local/bin/jetty`. The systemd unit and the
autostart entry point at that copy, never at the download location or `$APPDIR`.

**glibc.** "always build your application on the oldest GNU/Linux version you intend to support". PyInstaller
does not bundle libc, and libc "is forward compatible to newer releases, but it is not backward compatible"
(pyinstaller.org usage page, "Making GNU/Linux Apps Forward-Compatible"). Combined with PySide6's
`manylinux_2_34` floor (§2.4), the CI build runs in an **Ubuntu 22.04** container.

**Package data that must be collected.** Several files are located relative to `__file__`:
- `static/` (`app.py:80`)
- `migrations/` and `requests_migrations/` (`db.py:5-6`)

PyInstaller sets a bundled module's `__file__` "to the correct path relative to the bundle folder"
(pyinstaller.org runtime-information), so the code keeps working, provided these directories (and
`quick-add-catalog.json`) are declared as `datas` in the spec or an `orca_proxy` hook.

**Rough size (inference, not measured on a real build):**
- the Python venv: 109 MB unpacked today (`du -sh orca-proxy/.venv/lib/python3.13/site-packages`)
- the QtWebEngine files (≈266 MB) plus the used part of Essentials

That puts a compressed AppImage at about 200 MB. The first CI build will give the real number.

### 2.6 Privileged setup with `pkexec`

The root-only steps are `snap install lxd`, `lxd init --preseed` and `usermod -aG lxd $USER`. Relevant
`pkexec(1)` behaviour:
- It "will use the authentication agent registered for the calling process or session", which gives the desktop
  password dialog. It falls back to a text agent when none exists.
- Exit codes: 127 when not authorized or on error, 126 when "the user dismissed the authentication dialog".
- It runs the program in "a minimal known and safe environment".
- "the authentication dialog presented to the user will display the full path to the program to be executed".

Two design points follow:
- **Inference:** a wrapper like `pkexec sh -c '…'` makes that dialog show `/usr/bin/sh`, which tells the user
  nothing. Prefer one `pkexec` call per fixed command (`pkexec /usr/bin/snap install lxd`, …), or ship a polkit
  action whose `org.freedesktop.policykit.exec.path` annotation names a fixed helper (`pkexec(1)`, "ACTION AND
  AUTHORIZATIONS").
- **Never run the AppImage itself as root.** `pkexec` "does no validation of the ARGUMENTS passed to PROGRAM"
  (`pkexec(1)`, "SECURITY NOTES"), so the privileged surface should stay a fixed list of commands.

### 2.7 LXD over its REST API (aiohttp + Unix socket)

- **Socket path.** The LXD Go client's own default resolution is `$LXD_SOCKET`, then `$LXD_DIR/unix.socket`, then
  `/var/snap/lxd/common/lxd/unix.socket` "if the file exists and is writable", else `/var/lib/lxd/unix.socket`
  (`canonical/lxd` `client/connection.go:169-174`). Use the same order. On this host the socket is
  `srw-rw---- root lxd /var/snap/lxd/common/lxd/unix.socket` (LXD snap 5.21.8).
- **Local access is root-equivalent.** "The root user and all members of the `lxd` group can interact with the
  local daemon", and "Local access to LXD through the Unix socket always grants full access to LXD … you should
  only give such access to users who you'd trust with root access to your system" (LXD docs, installing). The
  GUI should say this during setup, not hide it.
- **Long operations are asynchronous.** "Any operation which may take more than a second to be done must be done
  in the background, returning a background operation ID". The server answers HTTP 202 with the operation URL
  in `Location`, which clients poll or long-poll (LXD docs, REST API). aiohttp, already a dependency
  (`orca-proxy/pyproject.toml`), handles this with `UnixConnector` and no new library.
- **`exec` is the exception.** `POST /1.0/instances/{name}/exec` returns "either 2 or 4 websockets" plus a
  control socket (`doc/rest-api.yaml:13806-13818`). A non-interactive exec can instead use
  `record-output` ("Whether to capture the output for later download (requires non-interactive)", `:2689-2691`)
  and fetch it from `/1.0/instances/{name}/logs/exec-output` (`:14105-14152`). `jetty-lxd` uses `lxc exec` for
  `wait_agent`, `cmd_exec` and the gateway setup. Porting those needs aiohttp websockets or `record-output`.
  **Inference:** `record-output` covers the provisioning cases, and only an interactive `jetty vm exec` would
  need websockets.

### 2.8 Joining the `lxd` group needs a new login session

The LXD docs: after `sudo usermod -aG lxd "$USER"`, either run `newgrp lxd`, which "only applies to the current
shell", or log out and back in (LXD docs, installing). The daemon (systemd user manager) and the tray (desktop
session) were both started before the group change, so neither can open the socket until the user logs in again.

**Inference:** the user manager may outlive a desktop logout (lingering, or other sessions still open), in which
case a full reboot is the reliable reset. The wizard should:
1. do the privileged steps,
2. tell the user plainly that a re-login is needed (offer to log out), and
3. resume from the daemon's `not_configured` state on the next start, detecting socket access by attempting a
   `GET /1.0`.

### 2.9 Splitting the work between the app and the agent

- **App:** installing LXD, the `jettyup0`/`jettypriv0` networks and the `jetty-gw` gateway, VM
  launch/start/stop/delete, IP and SSH port allocation, `~/.ssh/config` entries (everything `jetty-lxd` does
  today, `deploy/jetty-lxd:14-22`), and credentials and rules in the GUI.
- **Agent (`orca-ssh-setup`):** steps 1–2 of the skill (inspect the repo, confirm name, sizing, image and
  harnesses with the user), installing the toolchain inside the VM, and calling `jetty vm create …` and
  `jetty status` instead of `lxc` or `jetty-lxd`.

---

## 3. Alternatives considered and rejected

### 3.1 Rewrite the proxy in Go (then Go + Wails for the whole app)

Rejected because the port is not small and its risk sits on the security boundary. From the repo:
- **The easy part.** The policy logic is plain Python over SQLite: `rule_engine.py` (163 lines), `validation.py`
  (207), `credential_exec.py` (237), `request_log.py` (243), and the repos and handlers. It would port easily.
- **The hard part.** `proxy_addon.py` (361 lines) "never talks TCP/TLS itself". mitmproxy's WireGuard mode
  "hands each stream to its transparent layer with the VM's own source address as the peer and the original
  destination as the server address" (`proxy_addon.py:7-12`). In Go, Jetty would own:
  - a user-space WireGuard endpoint that accepts connections to any destination (`wireguard-go` plus gVisor
    netstack in forwarder mode),
  - ClientHello peeking that can still replay the bytes for pass-through (`tls_clienthello` with
    `data.ignore_connection = True`, `proxy_addon.py:120-187`),
  - ALPN matching and per-SNI certificate minting.

  The earlier passthrough research already maps those Go building blocks
  (`research/transparent-mitm-passthrough.md` §2.1, §5).
- **The tests don't carry over.**
  - `tests/test_proxy_addon.py` (448 lines) is written against mitmproxy's test helpers:
    `from mitmproxy.test import tflow`, `from mitmproxy.proxy.context import Context`, `tls.ClientHelloData`
    (`tests/test_proxy_addon.py:1-9`).
  - The API tests drive the aiohttp app in-process through `aiohttp_client(create_app())`
    (`tests/conftest.py`), not over a socket against a running server.
  - Only `tests/e2e/lxd-gateway-checks.sh` is black-box.

  A port would therefore start with almost no safety net at the boundary that matters most.

### 3.2 Tauri v2 shell with a PyInstaller-frozen Python sidecar

Tauri supports this (`bundle.externalBin`, with each binary named with a `-$TARGET_TRIPLE` suffix). Its docs name
"Python CLI applications or API servers bundled using `pyinstaller`" as a use case (v2.tauri.app/develop/sidecar).
Rejected because:
- **It still freezes the full Python runtime** (the proxy can't be dropped), and adds a Rust toolchain and a second
  runtime on top.
- **It adds Linux system dependencies.** Tauri needs `libwebkit2gtk-4.1` for the webview and
  `libayatana-appindicator3` "for system tray functionality" (v2.tauri.app/start/prerequisites). Those become
  host package requirements, or get bundled into the AppImage.
- **Its main advantage doesn't hold.** Reusing the web UI is also achieved by QtWebEngine (§2.4).

### 3.3 PySide6 Widgets only (no QtWebEngine)

This would save the ≈266 MB of WebEngine files (§2.4). Rejected because the rules, credentials and request-log
screens already exist as a dependency-free web UI (`app.py:75-86`). Widgets would mean rewriting them in Qt and
then maintaining two UIs, since the web UI stays useful from a browser.

### 3.4 pywebview (system WebKitGTK) plus a separate tray library

This is the smallest bundle and also reuses the web UI. Rejected because its Linux GTK backend depends on system
PyGObject and typelibs (`python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1`, WebKit2 ≥ 2.22) or on a
Qt backend anyway (pywebview.flowrl.com installation guide). **Inference:** freezing PyGObject against whatever
GTK/WebKit the host has is the least predictable option across distros. Its Qt backend would bring QtWebEngine
back anyway.

### 3.5 Nuitka onefile (packaging alternative to AppImage)

This is the viable fallback if AppImage gets in the way. Nuitka's `--onefile-tempdir-spec` "Defaults to
'{TEMP}/onefile_{PID}_{TIME_US}_{RANDOM}' … being non-static it's removed". A spec such as
`'{CACHE_DIR}/{COMPANY}/{PRODUCT}/{VERSION}'` "is a good static cache path, this will then not be removed". With
`--onefile-cache-mode` (`auto`/`cached`/`temporary`), "cached will not remove it and see to reuse its contents
during next execution for faster startup times" (`nuitka/options/OptionParsing.py:500-528`).

Not chosen because the AppImage route (§2.5) avoids extraction entirely, and because mitmproxy's own release
tooling and both packages' shipped hooks target PyInstaller (§2.5), so that path is better trodden for this
dependency set. **Unverified:** whether Nuitka compiles mitmproxy 10.4.2 and PySide6 6.11 cleanly.

---

## 4. Verified facts (quick reference)

| Claim | Status | Source |
|---|---|---|
| `DumpMaster(options, loop=None, with_termlog=True, with_dumper=True)` adds `default_addons()` plus KeepServing, ReadFileStdin, ErrorCheck | Verified | `mitmproxy/tools/dump.py` (10.4.2) |
| `Master` must be constructed inside a running asyncio loop | Verified | `mitmproxy/master.py:44-47` |
| `addons.add()` runs the addon's `load` event | Verified | `mitmproxy/addonmanager.py:198-204` |
| `Options.mode` is `Sequence[str]` and accepts `wireguard[:PATH]` with `@host:port` | Verified | `mitmproxy/options.py:107-119` |
| mitmproxy's Linux release binaries are PyInstaller onefile builds | Verified | `release/build.py:136-150`, `release/specs/standalone.spec` (v10.4.2) |
| mitmproxy and mitmproxy_rs ship PyInstaller hooks | Verified | `[pyinstaller40]` entry points; `mitmproxy/utils/pyinstaller/`, `mitmproxy_rs/_pyinstaller/` |
| mitmproxy_rs's macOS/Windows packages are conditional dependencies, never pulled in on Linux | Verified | `mitmproxy_rs-0.6.3` `Requires-Dist` markers; `hook-mitmproxy_rs.py` |
| QSystemTrayIcon uses StatusNotifierItem over D-Bus on Wayland always, on X11 when a watcher exists (else XEmbed) | Verified | qtbase 6.8 `qgenericunixthemes.cpp:80-90`; Qt docs |
| `isSystemTrayAvailable()` means an `org.kde.StatusNotifierWatcher` is registered | Verified | `qdbustrayicon.cpp:71,335-343`; `qsystemtrayicon_qpa.cpp:79-86` |
| QtWebEngine ≈266 MB unpacked (core lib 203.8 MB); Addons wheel 175 MB, Essentials 80 MB | Measured | PyPI wheels, PySide6 6.11.2 |
| PySide6 wheels need glibc ≥ 2.34 (`manylinux_2_34`); Ubuntu 22.04 has 2.35 | Verified | PyPI filenames; packages.ubuntu.com/jammy/libc6 |
| PySide6 is LGPL-3.0-only OR GPL-2.0/3.0; LGPL requires user relinkability, source, notice | Verified | PyPI metadata; qt.io LGPL obligations page |
| QtWebEngine sandbox needs unprivileged user namespaces; Ubuntu 24.04 restricts them by default | Verified | Qt WebEngine Platform Notes; Ubuntu 24.04 release notes |
| Whether QtWebEngine 6.11 actually fails under that default | **Unverified** | needs an empty-VM test |
| Classic AppImages need libfuse2 (`libfuse2t64` on 24.04); type2-runtime is static and needs no libfuse2 | Verified | docs.appimage.org; type2-runtime README |
| type2-runtime still needs a host `fusermount*`; snapd `Depends: fuse3`, which ships `fusermount3` | Verified | `runtime.c:417-455`; apt/packages.ubuntu.com |
| `$APPIMAGE` = resolved path of the AppImage; `$APPDIR` = mountpoint | Verified | docs.appimage.org env vars; `runtime.c:1642,1833` |
| Nuitka `--onefile-tempdir-spec` `{CACHE_DIR}/{COMPANY}/{PRODUCT}/{VERSION}` and `--onefile-cache-mode cached` | Verified | `nuitka/options/OptionParsing.py:500-528` |
| PyInstaller: build on the oldest glibc; onefile unpacks to `_MEI*` every run | Verified | pyinstaller.org usage and operating-mode pages |
| Snap LXD socket: `/var/snap/lxd/common/lxd/unix.socket` (after `$LXD_SOCKET` and `$LXD_DIR`) | Verified | `canonical/lxd` `client/connection.go:169-174`; local host |
| `lxd` group = root-equivalent; membership needs `newgrp` or re-login | Verified | LXD docs, installing |
| LXD `exec` over REST uses websockets, or `record-output` for non-interactive | Verified | `canonical/lxd` `doc/rest-api.yaml` |
| `pkexec` shows the program's full path, sanitizes env, exit 126 on dismiss | Verified | `pkexec(1)` |

## 5. Open questions

1. **QtWebEngine sandbox on Ubuntu 24.04+.** Does it fail, or quietly run unsandboxed? Should `jetty tray` set
   `QTWEBENGINE_DISABLE_SANDBOX=1` (trusted loopback content only), or should setup install an AppArmor
   `userns,` profile that covers the AppImage mount path? Test on clean 22.04, 24.04 and 26.04 VMs.
2. **Re-login reset.** Is logging out of the desktop enough to restart the systemd user manager with the new
   `lxd` group, or does lingering or another session force a reboot? Test empirically.
3. **Distro scope.** Ubuntu-only (snap LXD, snapd guarantees `fusermount3`), or other distros too? Elsewhere
   neither snapd nor `fuse3` can be assumed, and the LXD install step differs.
4. **`exec` port.** Use `record-output` for all provisioning, or add an aiohttp websocket client for an
   interactive `jetty vm exec`?
5. **Real bundle size** after excluding WebEngine devtools resources and unused locales (first CI build).
6. **Self-update.** Reuse `install.sh`'s release verification to replace `~/.local/bin/jetty` and restart the
   unit, or rely on AppImage's zsync update mechanism. Not researched here.

## 6. Corrections to claims made in the design discussion

- **"Exclude the macOS and Windows platform packages."** Nothing needs excluding. `mitmproxy_rs` declares
  `mitmproxy-macos` and `mitmproxy-windows` only behind `sys_platform == 'darwin'` and `os_name == 'nt'` markers,
  and its PyInstaller hook adds them as hidden imports only on those platforms (`mitmproxy_rs-0.6.3` METADATA,
  `hook-mitmproxy_rs.py`).
- **"Running mitmproxy in-process puts the daemon and the proxy on one asyncio loop, rather than through the
  `running()` hook."** They already share one loop today: the API starts inside mitmdump's loop through the
  `running()` hook (`proxy_addon.py:14-22, 83-91`), and that hook keeps working when embedded. What *is* new is a
  constraint: `DumpMaster` must be constructed **inside** a running loop (`master.py:44-47`). The earlier snippet
  showed module-level construction, which would raise.
- **"QtWebEngine is about 200 MB unpacked."** Measured at ≈266 MB for WebEngine-related files in PySide6 6.11.2,
  of which `libQt6WebEngineCore.so.6` alone is 203.8 MB.
- **"Recent Ubuntu releases don't ship `libfuse2`; use the static type-2 runtime."** Correct as far as it went,
  but the static runtime still needs a host `fusermount*` binary. On Ubuntu that is guaranteed through
  `snapd → fuse3`, not by the runtime itself.
- **"Build in CI on the oldest distro you support (e.g. Ubuntu 22.04)."** Confirmed, and 22.04 is also a hard
  floor: PySide6 6.11 wheels are `manylinux_2_34`, so older glibc (for example Debian 11's) can't run the bundle.
- **New risk not raised before:** Ubuntu 24.04's default restriction on unprivileged user namespaces versus the
  QtWebEngine sandbox (§2.4).
- **New risk not raised before:** LXD `exec` over the REST API is websocket-based (§2.7). This affects porting
  `jetty-lxd`'s `lxc exec` calls.

## 7. Next steps (in order)

1. **Embed mitmproxy and add the subcommands.** Run `DumpMaster` in-process (§2.3) behind `jetty daemon`, and add
   `jetty setup` and `jetty status`, still installed from source the way it is now. The current pytest suite and
   `tests/e2e/lxd-gateway-checks.sh` validate this without any GUI.
2. **Add the "not configured" state (§2.2)** and move `deploy/jetty-lxd`'s steps into daemon code over the LXD
   REST socket (§2.7). Shrink `orca-ssh-setup` to the agent-only steps (§2.9).
3. **Add `jetty tray`** with PySide6: QSystemTrayIcon with a fallback window, and a QtWebEngine window on the
   loopback UI. Add the setup-wizard pages to the web UI, including `pkexec` (§2.6) and the re-login step (§2.8).
4. **Build the PyInstaller onedir and AppImage** (type-2 static runtime) in `release.yml` on an Ubuntu 22.04
   container (§2.5). Then add self-install to `~/.local/bin/jetty` and self-update.
