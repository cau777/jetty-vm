#!/usr/bin/env bash
set -euo pipefail

# orca-proxy first-time install / upgrade (design ticket #12).
#
# Runs as the user who will own the service, never as root:
#   bash deploy/install.sh                          (from an existing checkout)
#   curl -fsSL https://raw.githubusercontent.com/cau777/jetty-vm/main/orca-proxy/deploy/install.sh | bash
#
# ORCA_PROXY_INSTANCE (default orca-proxy) names the service, data dir
# (~/.<instance>) and install dir, so this can run beside an older install
# (e.g. the Multipass-era one). ORCA_PROXY_MANAGEMENT_PORT sets its API port.
# The install writes ~/.<instance>/jetty.env; source it before running
# deploy/jetty-lxd so that tool talks to this instance.
#
# Nothing here needs privileges. Agent VMs reach the proxy through the Jetty
# gateway VM's WireGuard tunnel (deploy/jetty-lxd), so there is no host
# firewall to manage and no capability-bearing helper to install.

ORCA_PROXY_GIT_URL="${ORCA_PROXY_GIT_URL:-https://github.com/cau777/jetty-vm.git}"

if [ "$(id -u)" -eq 0 ]; then
  echo "!! run orca-proxy's installer as the user who will own the service, not root" >&2
  exit 1
fi

as_user() { bash -lc "$1"; }
TARGET_USER="$(id -un)"
TARGET_HOME="$HOME"

CLEANUP_DIR=""
cleanup() { [ -n "$CLEANUP_DIR" ] && rm -rf "$CLEANUP_DIR"; }
trap cleanup EXIT

# --- get the source ---
if [ -n "${ORCA_PROXY_REPO:-}" ]; then
  REPO_DIR="$ORCA_PROXY_REPO"
elif [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ] \
     && [ -f "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/pyproject.toml" ]; then
  # Invoked as a real file (`bash deploy/install.sh`, not piped) from
  # inside an actual checkout.
  REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
else
  # Piped (`curl ... | bash`) -- BASH_SOURCE isn't a real file in that
  # mode, so there's no checkout to derive a path from. Fetch a fresh
  # sparse checkout of just orca-proxy/ instead.
  echo "No local checkout detected -- fetching orca-proxy from $ORCA_PROXY_GIT_URL"
  CLEANUP_DIR="$(mktemp -d)"
  git clone --quiet --depth 1 --filter=blob:none --sparse "$ORCA_PROXY_GIT_URL" "$CLEANUP_DIR/skills"
  git -C "$CLEANUP_DIR/skills" sparse-checkout set orca-proxy
  REPO_DIR="$CLEANUP_DIR/skills/orca-proxy"
fi

VERSION="${ORCA_PROXY_VERSION:-$(cd "$REPO_DIR" && git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"
INSTANCE="${ORCA_PROXY_INSTANCE:-orca-proxy}"
PORT="${ORCA_PROXY_MANAGEMENT_PORT:-8080}"
INSTALL_DIR="$TARGET_HOME/.local/share/$INSTANCE/$VERSION"
DATA_DIR="$TARGET_HOME/.$INSTANCE"
CURRENT_LINK="$DATA_DIR/current"
UNIT_DIR="$TARGET_HOME/.config/systemd/user"
UNIT_PATH="$UNIT_DIR/$INSTANCE.service"

if [ -e "$INSTALL_DIR" ]; then
  echo "!! $INSTALL_DIR already exists -- refusing to overwrite an existing versioned install" >&2
  exit 1
fi

echo "Installing orca-proxy $VERSION to $INSTALL_DIR (for $TARGET_USER)"
install -d "$INSTALL_DIR" "$DATA_DIR" "$UNIT_DIR"

# Copy the source tree rather than symlinking it — a versioned install must
# stay immutable even if the working checkout later moves to a new commit.
cp -r "$REPO_DIR"/. "$INSTALL_DIR/source"
as_user "cd $(printf '%q' "$INSTALL_DIR/source") && uv sync --no-dev"
as_user "ln -sfn $(printf '%q' "$INSTALL_DIR/source/.venv") $(printf '%q' "$INSTALL_DIR/venv")"

# Stable, version-independent path for the systemd unit's `-s` argument —
# proxy_addon.py's real location inside site-packages varies by Python
# version, so it's copied to one fixed spot per versioned install instead.
cp "$INSTALL_DIR/source/src/orca_proxy/proxy_addon.py" "$INSTALL_DIR/proxy_addon.py"

echo "Writing systemd user unit $INSTANCE.service"
sed -e "s#%h/\.orca-proxy/#%h/.$INSTANCE/#g" \
    -e "s#^\[Service\]\$#[Service]\nEnvironment=ORCA_PROXY_HOME=%h/.$INSTANCE\nEnvironment=ORCA_PROXY_MANAGEMENT_PORT=$PORT#" \
    "$REPO_DIR/deploy/orca-proxy.service" > "$UNIT_PATH"
chmod 0644 "$UNIT_PATH"

cat > "$DATA_DIR/jetty.env" <<EOF
# Source before running jetty-lxd so it uses this orca-proxy instance.
export ORCA_PROXY_HOME=$DATA_DIR
export ORCA_PROXY_MANAGEMENT_PORT=$PORT
export ORCA_PROXY_SERVICE=$INSTANCE.service
export ORCA_PROXY_PYTHON=$CURRENT_LINK/venv/bin/python
JL=$CURRENT_LINK/source/deploy/jetty-lxd
API=http://127.0.0.1:$PORT/api/v1
EOF

# Atomic repoint — the only step that changes what "current" (and therefore
# the systemd unit) actually points at.
as_user "ln -sfn $(printf '%q' "$INSTALL_DIR") $(printf '%q' "$CURRENT_LINK")"

echo "Starting $INSTANCE.service"
# Lets the user service run without a login session; polkit allows this
# for one's own account by default.
loginctl enable-linger "$TARGET_USER" || echo "!! could not enable linger; orca-proxy will only run while $TARGET_USER is logged in" >&2
# `enable --now` is a no-op on an already-running unit -- it does NOT
# restart it. On an upgrade that silently leaves the OLD process running
# (old code, still resolved against whatever "current" pointed at when it
# started) despite `current` having just been repointed above -- silently
# serving stale logic. `restart` unconditionally guarantees
# the new version is actually what's running.
as_user "systemctl --user daemon-reload && systemctl --user enable $INSTANCE.service && systemctl --user restart $INSTANCE.service"

# The tunnel listener binds the host's address on jetty-lxd's uplink bridge;
# before `jetty-lxd setup` has created it the unit just keeps retrying.
if ! ip -4 -o addr show 2>/dev/null | grep -q ' 10\.201\.0\.1/'; then
  echo "orca-proxy $VERSION installed (current -> $INSTALL_DIR); it starts once 'jetty-lxd setup' creates the Jetty network"
  exit 0
fi

sleep 2
as_user "systemctl --user is-active --quiet $INSTANCE.service" || {
  as_user "systemctl --user status $INSTANCE.service --no-pager --lines=20" || true
  echo "orca-proxy failed to start -- resolve before continuing" >&2
  exit 1
}

echo "orca-proxy $VERSION installed and running (current -> $INSTALL_DIR)"
