#!/usr/bin/env bash
set -euo pipefail

# orca-proxy first-time install / upgrade (design ticket #12).
#
# Must run as root:
#   sudo bash deploy/install.sh                     (from an existing checkout)
#   curl -fsSL https://raw.githubusercontent.com/cau777/jetty-vm/main/orca-proxy/deploy/install.sh | sudo bash
#
# Why root now, when the old install.sh ran as the target user and only
# sudo'd two small steps: the firewall-sync helper is only safe if nothing
# in its own path is writable by the user invoking it. The old layout
# resolved through ~/.orca-proxy/current/venv/bin/... — fully writable by
# that same user, so overwriting it (then running it, formerly via `sudo`,
# now via its own `setcap`-granted capability) was a trivial escalation.
# The fix installs a standalone copy of the helper to a root-owned location
# outside the user's home directory entirely, which this script can only do
# as root. Upgrading is accordingly now a deliberate "run this again as
# root" action — see design.md's "Service installation and firewall-rule
# lifecycle" section.
#
# Everything *unprivileged* (the venv, the systemd --user unit, the data
# directory) is still built and owned by the target user, not root — this
# script only elevates for the two things that actually need it: compiling
# and installing the root-owned, capability-bearing firewall-sync binary,
# and applying `setcap` to it. There is no sudoers entry anywhere in this
# design — the helper's elevated access comes from a file capability on its
# own binary, not from a NOPASSWD grant.

ORCA_PROXY_GIT_URL="${ORCA_PROXY_GIT_URL:-https://github.com/cau777/jetty-vm.git}"

if [ "$(id -u)" -ne 0 ]; then
  cat >&2 <<'MSG'
!! orca-proxy's installer must run as root:
     sudo bash deploy/install.sh
   or, with no local checkout:
     curl -fsSL https://raw.githubusercontent.com/cau777/jetty-vm/main/orca-proxy/deploy/install.sh | sudo bash
MSG
  exit 1
fi

command -v setcap > /dev/null || {
  echo "!! setcap is required (Debian/Ubuntu: apt install libcap2-bin) to grant the firewall-sync binary CAP_NET_ADMIN/CAP_NET_RAW." >&2
  exit 1
}
command -v cc > /dev/null || command -v gcc > /dev/null || {
  echo "!! a C compiler (gcc or cc) is required to compile the firewall-sync helper." >&2
  exit 1
}

# --- who is this actually for? ---
# `sudo bash install.sh` sets SUDO_USER to the invoking (non-root) account;
# `curl ... | sudo bash` does too, since sudo itself sets it regardless of
# how bash's stdin is fed. ORCA_PROXY_USER overrides for anything that
# doesn't go through sudo (e.g. already running as root some other way).
TARGET_USER="${ORCA_PROXY_USER:-${SUDO_USER:-}}"
if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = "root" ]; then
  echo "!! could not determine a non-root target user -- run via 'sudo', or set ORCA_PROXY_USER=<name>" >&2
  exit 1
fi
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
if [ -z "$TARGET_HOME" ] || [ ! -d "$TARGET_HOME" ]; then
  echo "!! $TARGET_USER has no home directory" >&2
  exit 1
fi
TARGET_UID="$(id -u "$TARGET_USER")"

as_user() {
  # Login shell so PATH/profile-sourced tooling (uv, etc.) resolves the same
  # way it would if $TARGET_USER ran this themselves; XDG_RUNTIME_DIR is
  # forced so `systemctl --user` reaches the right session bus even when
  # invoked from a root shell rather than a real login of that user.
  sudo -u "$TARGET_USER" -H env XDG_RUNTIME_DIR="/run/user/$TARGET_UID" bash -lc "$1"
}

CLEANUP_DIR=""
cleanup() { [ -n "$CLEANUP_DIR" ] && rm -rf "$CLEANUP_DIR"; }
trap cleanup EXIT

# --- get the source ---
if [ -n "${ORCA_PROXY_REPO:-}" ]; then
  REPO_DIR="$ORCA_PROXY_REPO"
elif [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ] \
     && [ -f "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/pyproject.toml" ]; then
  # Invoked as a real file (`sudo bash deploy/install.sh`, not piped) from
  # inside an actual checkout.
  REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
else
  # Piped (`curl ... | sudo bash`) -- BASH_SOURCE isn't a real file in that
  # mode, so there's no checkout to derive a path from. Fetch a fresh
  # sparse checkout of just orca-proxy/ instead.
  echo "No local checkout detected -- fetching orca-proxy from $ORCA_PROXY_GIT_URL"
  CLEANUP_DIR="$(mktemp -d)"
  git clone --quiet --depth 1 --filter=blob:none --sparse "$ORCA_PROXY_GIT_URL" "$CLEANUP_DIR/skills"
  git -C "$CLEANUP_DIR/skills" sparse-checkout set orca-proxy
  REPO_DIR="$CLEANUP_DIR/skills/orca-proxy"
fi

VERSION="${ORCA_PROXY_VERSION:-$(cd "$REPO_DIR" && git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"
INSTALL_DIR="$TARGET_HOME/.local/share/orca-proxy/$VERSION"
DATA_DIR="$TARGET_HOME/.orca-proxy"
CURRENT_LINK="$DATA_DIR/current"
UNIT_DIR="$TARGET_HOME/.config/systemd/user"
UNIT_PATH="$UNIT_DIR/orca-proxy.service"

if [ -e "$INSTALL_DIR" ]; then
  echo "!! $INSTALL_DIR already exists -- refusing to overwrite an existing versioned install" >&2
  exit 1
fi

echo "Installing orca-proxy $VERSION to $INSTALL_DIR (for $TARGET_USER)"
install -d -o "$TARGET_USER" -g "$TARGET_USER" "$INSTALL_DIR" "$DATA_DIR" "$UNIT_DIR"

# Copy the source tree rather than symlinking it — a versioned install must
# stay immutable even if the working checkout later moves to a new commit.
cp -r "$REPO_DIR"/. "$INSTALL_DIR/source"
chown -R "$TARGET_USER:$TARGET_USER" "$INSTALL_DIR"
as_user "cd $(printf '%q' "$INSTALL_DIR/source") && uv sync --no-dev"
# Created by the target user directly (they already own $INSTALL_DIR, as of
# the chown -R above) rather than root creating it then handing ownership
# over via a separate chown -h -- simpler, and sidesteps root creating a
# symlink cau777 doesn't end up owning.
as_user "ln -sfn $(printf '%q' "$INSTALL_DIR/source/.venv") $(printf '%q' "$INSTALL_DIR/venv")"

# Stable, version-independent path for the systemd unit's `-s` argument —
# proxy_addon.py's real location inside site-packages varies by Python
# version, so it's copied to one fixed spot per versioned install instead.
cp "$INSTALL_DIR/source/src/orca_proxy/proxy_addon.py" "$INSTALL_DIR/proxy_addon.py"
chown "$TARGET_USER:$TARGET_USER" "$INSTALL_DIR/proxy_addon.py"

echo "Writing systemd user unit"
install -o "$TARGET_USER" -g "$TARGET_USER" -m 0644 "$REPO_DIR/deploy/orca-proxy.service" "$UNIT_PATH"

echo "Compiling the firewall-sync helper (as $TARGET_USER, so the build never runs as root)"
# Nuitka needs a real .py suffix to recognize the entry file; the source is
# deliberately extensionless in the repo (installed verbatim in the old
# design, see the file's own docstring) so it's copied to a throwaway name
# first rather than renamed in place. Built inside $INSTALL_DIR/source
# (already owned by $TARGET_USER as of the chown -R above) so `as_user`
# needs no extra directory setup, and removed once the compiled output is
# copied out below.
#
# --standalone, not --onefile: onefile's bootstrap self-extracts to a fresh
# /tmp directory -- owned by whoever *invokes* the binary, i.e. the target
# user, every single run -- and fork+execs the real program from there. That
# extracted copy is a different file with no file capabilities of its own;
# setcap on the outer onefile binary never reaches it (capabilities only
# survive that hop via the ambient set, and nothing in Nuitka's generic
# bootstrap raises it before the fork). Verified empirically during
# development: /proc/self/exe inside a running onefile build resolves to
# the extracted temp copy, not the setcap'd file, which means
# CAP_NET_ADMIN/CAP_NET_RAW would silently never reach the code that calls
# iptables -- this script's own _raise_ambient_capabilities() would run too
# late, in a process whose permitted set is already empty, and fail closed
# exactly the way it does under pytest, except for real. --standalone's
# output binary is what actually gets exec'd, confirmed the same way
# (/proc/self/exe matches the installed path directly, no extra hop).
# `--with patchelf` supplies Nuitka's standalone-mode dependency from a pure
# wheel, so this needs no system `patchelf` package.
BUILD_DIR="$INSTALL_DIR/source/.firewall-sync-build"
as_user "mkdir -p $(printf '%q' "$BUILD_DIR") \
  && cp $(printf '%q' "$INSTALL_DIR/source/deploy/orca-proxy-firewall-sync") $(printf '%q' "$BUILD_DIR/orca-proxy-firewall-sync.py") \
  && cd $(printf '%q' "$INSTALL_DIR/source") \
  && uv run --with nuitka --with patchelf python -m nuitka --standalone --quiet \
       --output-dir=$(printf '%q' "$BUILD_DIR") \
       --output-filename=orca-proxy-firewall-sync.bin \
       $(printf '%q' "$BUILD_DIR/orca-proxy-firewall-sync.py")"

echo "Installing the privileged firewall-sync binary (root-owned, outside $TARGET_USER's home)"
# --standalone produces a directory (the compiled binary plus the shared
# libraries it links against, e.g. libpython*.so) rather than a single
# file. The binary's RPATH is $ORIGIN, so its libraries must land in the
# same directory it does -- both go into /usr/local/sbin, root-owned and
# non-writable by $TARGET_USER by the same standard-FHS assumption the rest
# of this design already relies on for the binary itself (see design.md's
# "Service installation and firewall-rule lifecycle" section). Stale
# libraries from a prior install (a version bump can change the linked
# libpython filename) are cleared first so they don't just accumulate.
DIST_DIR="$BUILD_DIR/orca-proxy-firewall-sync.dist"
FIREWALL_BIN="/usr/local/sbin/orca-proxy-firewall-sync"
rm -f /usr/local/sbin/libpython*.so*
install -o root -g root -m 0755 "$DIST_DIR/orca-proxy-firewall-sync.bin" "$FIREWALL_BIN"
for lib in "$DIST_DIR"/*.so*; do
  install -o root -g root -m 0644 "$lib" "/usr/local/sbin/$(basename "$lib")"
done
rm -rf "$BUILD_DIR"

echo "Granting CAP_NET_ADMIN/CAP_NET_RAW to $FIREWALL_BIN"
# This is the entire privilege grant in this design -- no sudoers entry,
# no NOPASSWD rule. The capability is tied to this exact file's inode, so
# $FIREWALL_BIN being root-owned and outside $TARGET_USER's home (above) is
# what keeps it out of that account's reach; setcap itself grants nothing
# beyond what those permissions already protect.
setcap cap_net_admin,cap_net_raw+eip "$FIREWALL_BIN"

# Atomic repoint — the only step that changes what "current" (and therefore
# the systemd unit) actually points at. Irrelevant to the firewall-sync
# binary either way: it lives outside this symlink chain entirely, at a
# fixed path that doesn't change across upgrades. Created by the target
# user directly (they already own $DATA_DIR) for the same reason as the
# venv symlink above.
as_user "ln -sfn $(printf '%q' "$INSTALL_DIR") $(printf '%q' "$CURRENT_LINK")"

echo "Starting orca-proxy.service"
loginctl enable-linger "$TARGET_USER"
systemctl start "user@$TARGET_UID.service" 2>/dev/null || true
# `enable --now` is a no-op on an already-running unit -- it does NOT
# restart it. On an upgrade that silently leaves the OLD process running
# (old code, still resolved against whatever "current" pointed at when it
# started) despite `current` having just been repointed above -- invisible
# until something finally triggers a reconcile call against a process
# that's still running stale logic. `restart` unconditionally guarantees
# the new version is actually what's running.
as_user "systemctl --user daemon-reload && systemctl --user enable orca-proxy.service && systemctl --user restart orca-proxy.service"

sleep 2
as_user "systemctl --user is-active --quiet orca-proxy.service" || {
  as_user "systemctl --user status orca-proxy.service --no-pager --lines=20" || true
  echo "orca-proxy failed to start -- resolve before continuing" >&2
  exit 1
}

echo "orca-proxy $VERSION installed and running (current -> $INSTALL_DIR)"
echo "Privileged firewall-sync helper: $FIREWALL_BIN (root-owned, CAP_NET_ADMIN/CAP_NET_RAW via setcap, no sudoers entry)"
