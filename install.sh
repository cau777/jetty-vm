#!/usr/bin/env bash
# Install one version-pinned Jetty release. Release automation stamps
# JETTY_RELEASE_VERSION into the downloadable jetty-install.sh asset.
set -euo pipefail

JETTY_RELEASE_VERSION=""
JETTY_REPOSITORY="${JETTY_REPOSITORY:-cau777/jetty-vm}"
INSTALL_PROXY=false

usage() {
  cat <<'EOF'
Usage: install.sh [--with-proxy]

Installs the Jetty skill for the version carried by this installer. With
--with-proxy, it also asks sudo to install the matching host-side proxy.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --with-proxy) INSTALL_PROXY=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -z "$JETTY_RELEASE_VERSION" ] && [ -f "$SCRIPT_DIR/VERSION" ]; then
  JETTY_RELEASE_VERSION="$(tr -d '[:space:]' < "$SCRIPT_DIR/VERSION")"
fi
if [[ ! "$JETTY_RELEASE_VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.-]+)?$ ]]; then
  echo "Jetty installer has no valid release version." >&2
  exit 1
fi

JETTY_HOME="${JETTY_HOME:-$HOME/.local/share/jetty}"
INSTALL_DIR="$JETTY_HOME/releases/$JETTY_RELEASE_VERSION"
TEMP_DIR=""

cleanup() {
  [ -z "$TEMP_DIR" ] || rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

install_source() {
  local source_dir="$1"
  if [ -d "$INSTALL_DIR" ]; then
    local installed_version
    installed_version="$(tr -d '[:space:]' < "$INSTALL_DIR/VERSION" 2>/dev/null || true)"
    if [ "$installed_version" != "$JETTY_RELEASE_VERSION" ]; then
      echo "Refusing to replace $INSTALL_DIR: it is not $JETTY_RELEASE_VERSION." >&2
      exit 1
    fi
    return
  fi

  mkdir -p "$(dirname "$INSTALL_DIR")"
  cp -a "$source_dir" "$INSTALL_DIR"
}

if [ -f "$SCRIPT_DIR/VERSION" ] && [ -d "$SCRIPT_DIR/orca-ssh-setup" ]; then
  install_source "$SCRIPT_DIR"
else
  command -v curl > /dev/null || { echo "curl is required." >&2; exit 1; }
  command -v sha256sum > /dev/null || { echo "sha256sum is required." >&2; exit 1; }
  command -v tar > /dev/null || { echo "tar is required." >&2; exit 1; }

  TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/jetty-install.XXXXXX")"
  BUNDLE="jetty-${JETTY_RELEASE_VERSION}.tar.gz"
  RELEASE_URL="https://github.com/$JETTY_REPOSITORY/releases/download/$JETTY_RELEASE_VERSION"
  curl --fail --location --silent --show-error "$RELEASE_URL/$BUNDLE" -o "$TEMP_DIR/$BUNDLE"
  curl --fail --location --silent --show-error "$RELEASE_URL/$BUNDLE.sha256" -o "$TEMP_DIR/$BUNDLE.sha256"
  (
    cd "$TEMP_DIR"
    sha256sum --check "$BUNDLE.sha256"
  )
  tar -xzf "$TEMP_DIR/$BUNDLE" -C "$TEMP_DIR"
  SOURCE_DIR="$TEMP_DIR/jetty-${JETTY_RELEASE_VERSION}"
  [ -f "$SOURCE_DIR/VERSION" ] || { echo "Release bundle is malformed." >&2; exit 1; }
  install_source "$SOURCE_DIR"
fi

GH_REST_SCRIPT="$INSTALL_DIR/gh-rest/gh-rest.py"
[ -f "$GH_REST_SCRIPT" ] || { echo "Jetty release is missing gh-rest/gh-rest.py." >&2; exit 1; }
chmod +x "$GH_REST_SCRIPT"

command -v npx > /dev/null || {
  echo "npx is required to install Jetty's agent skill. Install Node.js first." >&2
  exit 1
}

npx --yes skills add "$INSTALL_DIR" \
  --skill orca-ssh-setup \
  --global \
  --copy \
  --yes

echo "Installed Jetty $JETTY_RELEASE_VERSION to $INSTALL_DIR"
echo "The orca-ssh-setup skill is ready in your detected agent(s)."

if [ "$INSTALL_PROXY" = true ]; then
  echo "Installing matching orca-proxy $JETTY_RELEASE_VERSION (sudo confirmation required)..."
  sudo env ORCA_PROXY_VERSION="$JETTY_RELEASE_VERSION" \
    bash "$INSTALL_DIR/orca-proxy/deploy/install.sh"
fi
