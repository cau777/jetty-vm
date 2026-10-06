#!/usr/bin/env bash
# Install, upgrade or roll back one version-pinned Jetty release, as your own
# user (never root). Release automation stamps JETTY_RELEASE_VERSION into the
# downloadable jetty-install.sh asset; run from a checkout, it uses VERSION.
#
#   ~/.local/share/jetty/releases/<ver>/   the release (its orca-proxy/.venv too)
#   ~/.local/share/jetty/current           -> releases/<ver>; the only thing an
#                                             upgrade or rollback repoints
#   ~/.local/share/jetty/jetty.env         legacy helper settings
#   ~/.<instance>/                         proxy state (DB, CA, tunnel keys);
#                                             never touched by upgrades
#   ~/.config/systemd/user/<instance>.service
set -euo pipefail

JETTY_RELEASE_VERSION=""
JETTY_REPOSITORY="${JETTY_REPOSITORY:-cau777/jetty-vm}"
JETTY_HOME="${JETTY_HOME:-$HOME/.local/share/jetty}"
ENV_FILE="$JETTY_HOME/jetty.env"

usage() {
  cat <<'EOF'
Usage: install.sh [--instance NAME] [--port PORT]
       install.sh --rollback VERSION

Installs the Jetty skill and the host-side orca-proxy service for the version
carried by this installer, or upgrades an existing install to it.

  --instance NAME  service name and data dir (~/.NAME) for the proxy; default
                   orca-proxy. Use another name to run beside an older
                   (Multipass-era) orca-proxy. Remembered for upgrades.
  --port PORT      the proxy's loopback Management API port; default 8080.
                   Remembered for upgrades.
  --rollback VER   point Jetty back at an already-installed release and restart.
EOF
}

INSTANCE="" PORT="" ROLLBACK=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --instance) INSTANCE="${2:?--instance needs a name}"; shift ;;
    --port) PORT="${2:?--port needs a number}"; shift ;;
    --rollback) ROLLBACK="${2:?--rollback needs a version}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [ "$(id -u)" -eq 0 ]; then
  echo "Run the Jetty installer as the user who will own the VMs and proxy, not root." >&2
  exit 1
fi

# Upgrades and rollbacks keep the instance and port chosen at first install.
if [ -f "$ENV_FILE" ]; then
  PREV_INSTANCE="$(sed -n 's/^export ORCA_PROXY_SERVICE=\(.*\)\.service$/\1/p' "$ENV_FILE")"
  PREV_PORT="$(sed -n 's/^export ORCA_PROXY_MANAGEMENT_PORT=//p' "$ENV_FILE")"
  if [ -n "$INSTANCE" ] && [ "$INSTANCE" != "$PREV_INSTANCE" ]; then
    echo "Jetty is already installed as instance '$PREV_INSTANCE'; it cannot be renamed by an upgrade." >&2
    exit 1
  fi
  INSTANCE="$PREV_INSTANCE"
  PORT="${PORT:-$PREV_PORT}"
fi
INSTANCE="${INSTANCE:-orca-proxy}"
PORT="${PORT:-8080}"
[[ "$INSTANCE" =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "Invalid instance name: $INSTANCE" >&2; exit 1; }
[[ "$PORT" =~ ^[0-9]+$ ]] || { echo "Invalid port: $PORT" >&2; exit 1; }
SERVICE="$INSTANCE.service"
DATA_DIR="$HOME/.$INSTANCE"

restart_proxy() {
  # Lets the user service run without a login session; polkit allows this for
  # one's own account by default.
  loginctl enable-linger "$(id -un)" 2>/dev/null \
    || echo "!! could not enable linger; $SERVICE will only run while you are logged in" >&2
  systemctl --user daemon-reload
  systemctl --user enable --quiet "$SERVICE"
  # restart, not `enable --now`: on an upgrade the old process must not keep
  # serving the previous release.
  systemctl --user restart "$SERVICE"

  # The daemon serves its UI before LXD setup and starts the proxy listener
  # when the Jetty uplink address appears.
  if ! ip -4 -o addr show 2>/dev/null | grep -q ' 10\.201\.0\.1/'; then
    echo "$SERVICE is running; the proxy listener starts after Jetty's gateway network is created."
    return
  fi
  sleep 3
  systemctl --user is-active --quiet "$SERVICE" || {
    systemctl --user status "$SERVICE" --no-pager --lines=20 || true
    echo "$SERVICE failed to start -- resolve before continuing" >&2
    exit 1
  }
}

if [ -n "$ROLLBACK" ]; then
  [ -d "$JETTY_HOME/releases/$ROLLBACK/orca-proxy/.venv" ] || {
    echo "$ROLLBACK is not installed. Installed releases:" >&2
    ls "$JETTY_HOME/releases" >&2
    exit 1
  }
  ln -sfn "releases/$ROLLBACK" "$JETTY_HOME/current"
  restart_proxy
  echo "Rolled back: current -> $ROLLBACK"
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -z "$JETTY_RELEASE_VERSION" ] && [ -f "$SCRIPT_DIR/VERSION" ]; then
  JETTY_RELEASE_VERSION="$(tr -d '[:space:]' < "$SCRIPT_DIR/VERSION")"
fi
if [[ ! "$JETTY_RELEASE_VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.-]+)?$ ]]; then
  echo "Jetty installer has no valid release version." >&2
  exit 1
fi

for tool in uv npx systemctl; do
  command -v "$tool" > /dev/null || { echo "$tool is required." >&2; exit 1; }
done

RELEASE_DIR="$JETTY_HOME/releases/$JETTY_RELEASE_VERSION"
TEMP_DIR=""
cleanup() { [ -z "$TEMP_DIR" ] || rm -rf "$TEMP_DIR"; }
trap cleanup EXIT

install_source() {
  local source_dir="$1"
  if [ -d "$RELEASE_DIR" ]; then
    local installed_version
    installed_version="$(tr -d '[:space:]' < "$RELEASE_DIR/VERSION" 2>/dev/null || true)"
    if [ "$installed_version" != "$JETTY_RELEASE_VERSION" ]; then
      echo "Refusing to replace $RELEASE_DIR: it is not $JETTY_RELEASE_VERSION." >&2
      exit 1
    fi
    return
  fi
  mkdir -p "$JETTY_HOME/releases"
  cp -a "$source_dir" "$RELEASE_DIR"
  rm -rf "$RELEASE_DIR/.git" "$RELEASE_DIR/orca-proxy/.venv"
}

if [ -f "$SCRIPT_DIR/VERSION" ] && [ -d "$SCRIPT_DIR/orca-ssh-setup" ]; then
  install_source "$SCRIPT_DIR"
else
  for tool in curl sha256sum tar; do
    command -v "$tool" > /dev/null || { echo "$tool is required." >&2; exit 1; }
  done
  TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/jetty-install.XXXXXX")"
  BUNDLE="jetty-${JETTY_RELEASE_VERSION}.tar.gz"
  RELEASE_URL="https://github.com/$JETTY_REPOSITORY/releases/download/$JETTY_RELEASE_VERSION"
  curl --fail --location --silent --show-error "$RELEASE_URL/$BUNDLE" -o "$TEMP_DIR/$BUNDLE"
  curl --fail --location --silent --show-error "$RELEASE_URL/$BUNDLE.sha256" -o "$TEMP_DIR/$BUNDLE.sha256"
  (cd "$TEMP_DIR" && sha256sum --check --quiet "$BUNDLE.sha256")
  tar -xzf "$TEMP_DIR/$BUNDLE" -C "$TEMP_DIR"
  SOURCE_DIR="$TEMP_DIR/jetty-${JETTY_RELEASE_VERSION}"
  [ -f "$SOURCE_DIR/VERSION" ] || { echo "Release bundle is malformed." >&2; exit 1; }
  install_source "$SOURCE_DIR"
fi
chmod +x "$RELEASE_DIR/gh-rest/gh-rest.py" "$RELEASE_DIR/orca-proxy/deploy/jetty-lxd"
install -D -m 0755 "$RELEASE_DIR/gh-rest/gh-rest.py" "$JETTY_HOME/gh-rest.py"
install -D -m 0644 \
  "$RELEASE_DIR/orca-proxy/src/orca_proxy/resources/quick-add-catalog.json" \
  "$JETTY_HOME/quick-add-catalog.json"

echo "Preparing orca-proxy $JETTY_RELEASE_VERSION"
(cd "$RELEASE_DIR/orca-proxy" && uv sync --no-dev --quiet)

# Everything below resolves through `current`, so neither file changes across
# upgrades and the repoint below is the single switch-over.
CURRENT="$JETTY_HOME/current"
PROXY="$CURRENT/orca-proxy"
mkdir -p "$DATA_DIR" "$HOME/.config/systemd/user"

cat > "$HOME/.config/systemd/user/$SERVICE" <<EOF
# Written by Jetty's install.sh; rerun it rather than editing this file.
[Unit]
Description=orca-proxy for Jetty VMs ($INSTANCE)
After=network-online.target

[Service]
Environment=ORCA_PROXY_HOME=$DATA_DIR
Environment=ORCA_PROXY_MANAGEMENT_PORT=$PORT
ExecStart=$PROXY/.venv/bin/jetty daemon
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

cat > "$ENV_FILE" <<EOF
# Written by Jetty's install.sh. jetty-lxd reads it; source it for \$JL and \$API.
export ORCA_PROXY_HOME=$DATA_DIR
export ORCA_PROXY_MANAGEMENT_PORT=$PORT
export ORCA_PROXY_SERVICE=$SERVICE
export ORCA_PROXY_PYTHON=$PROXY/.venv/bin/python
export JETTY_CLI=$PROXY/.venv/bin/jetty
JETTY_RELEASE_DIR=$CURRENT
JL=$PROXY/deploy/jetty-lxd
API=http://127.0.0.1:$PORT/api/v1
EOF

ln -sfn "releases/$JETTY_RELEASE_VERSION" "$CURRENT"
restart_proxy

npx --yes skills add "$RELEASE_DIR" --skill orca-ssh-setup --global --copy --yes

echo "Installed Jetty $JETTY_RELEASE_VERSION (current -> $RELEASE_DIR)"
echo "Proxy: $SERVICE, Management API http://127.0.0.1:$PORT, data in $DATA_DIR"
echo "The orca-ssh-setup skill is ready in your detected agent(s)."
