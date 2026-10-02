#!/usr/bin/env bash
# PROTOTYPE — throwaway. One command: build patched Lima, start the explicit
# proxy, boot a Lima VM whose only NIC is the patched user-mode network, then
# run the bypass checks. Needs no sudo and writes no host firewall rules.
#
#   ./run.sh           # set up + start everything + run checks
#   ./run.sh down      # stop VM and proxy
#
# Everything lands in $JETTY_PROTO_DIR (default ~/.cache/jetty-lima-proto).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
W=${JETTY_PROTO_DIR:-$HOME/.cache/jetty-lima-proto}
LIMA_VERSION=2.2.0
GVTV_VERSION=v0.8.9
GO_VERSION=go1.27.1
PROXY_ADDR=127.0.0.1:18080
VM=proto

export LIMA_HOME=$W/lima-home
export PATH=$W/lima/bin:$W/qbin:$W/go/bin:$PATH
mkdir -p "$W"

if [ "${1:-}" = down ]; then
  limactl stop -f "$VM" 2>/dev/null || true
  [ -f "$W/mitmdump.pid" ] && kill "$(cat "$W/mitmdump.pid")" 2>/dev/null || true
  exit 0
fi

# 1. QEMU without a system install: extract the distro .debs.
if ! command -v qemu-system-x86_64 >/dev/null; then
  mkdir -p "$W/debs" "$W/qroot" "$W/qbin"
  (cd "$W/debs" && apt-get download qemu-system-x86 qemu-system-common qemu-system-data qemu-utils \
    seabios ipxe-qemu libslirp0 libfdt1 libcacard0 libdaxctl1 libmpathcmd0 libmultipath0 \
    libmpathpersist0 libndctl6 libpmem1 librdmacm1t64 libusbredirparser1t64)
  for d in "$W"/debs/*.deb; do dpkg -x "$d" "$W/qroot"; done
  cat >"$W/qbin/qemu-system-x86_64" <<EOF
#!/bin/sh
export LD_LIBRARY_PATH=$W/qroot/usr/lib/x86_64-linux-gnu
exec $W/qroot/usr/bin/qemu-system-x86_64 -L $W/qroot/usr/share/qemu -L $W/qroot/usr/share/seabios "\$@"
EOF
  cat >"$W/qbin/qemu-img" <<EOF
#!/bin/sh
export LD_LIBRARY_PATH=$W/qroot/usr/lib/x86_64-linux-gnu
exec $W/qroot/usr/bin/qemu-img "\$@"
EOF
  chmod +x "$W"/qbin/*
fi

# 2. Lima release (guest agents, templates) + limactl rebuilt against patched gvisor-tap-vsock.
if [ ! -x "$W/lima/bin/limactl.orig" ]; then
  [ -x "$W/go/bin/go" ] || curl -fsSL "https://go.dev/dl/$GO_VERSION.linux-amd64.tar.gz" | tar -xz -C "$W"
  mkdir -p "$W/lima" "$W/src"
  curl -fsSL "https://github.com/lima-vm/lima/releases/download/v$LIMA_VERSION/lima-$LIMA_VERSION-Linux-x86_64.tar.gz" | tar -xz -C "$W/lima"
  [ -d "$W/src/lima" ] || git clone -q --depth 1 --branch "v$LIMA_VERSION" https://github.com/lima-vm/lima "$W/src/lima"
  [ -d "$W/src/gvisor-tap-vsock" ] || git clone -q --depth 1 --branch "$GVTV_VERSION" https://github.com/containers/gvisor-tap-vsock "$W/src/gvisor-tap-vsock"
  git -C "$W/src/gvisor-tap-vsock" apply "$HERE/gvisor-tap-vsock-$GVTV_VERSION.patch"
  (cd "$W/src/lima" && go mod edit -replace github.com/containers/gvisor-tap-vsock=../gvisor-tap-vsock \
    && CGO_ENABLED=0 go build -ldflags "-X github.com/lima-vm/lima/v2/pkg/version.Version=$LIMA_VERSION" \
      -o "$W/limactl-jetty" ./cmd/limactl)
  mv "$W/lima/bin/limactl" "$W/lima/bin/limactl.orig"
  cp "$W/limactl-jetty" "$W/lima/bin/limactl"
fi

# 3. A dedicated user-v2 network for this VM (one usernet process == one VM identity).
mkdir -p "$LIMA_HOME/_config"
cat >"$LIMA_HOME/_config/networks.yaml" <<'EOF'
paths:
  socketVMNet: ""
  varRun: /run/lima
  sudoers: /etc/sudoers.d/lima
group: everyone
networks:
  jetty-proto:
    mode: user-v2
    gateway: 192.168.110.1
    netmask: 255.255.255.0
EOF

# 4. Explicit proxy (stand-in for orca-proxy), unprivileged, loopback only.
if ! { [ -f "$W/mitmdump.pid" ] && kill -0 "$(cat "$W/mitmdump.pid")" 2>/dev/null; }; then
  MITMDUMP=${MITMDUMP:-$HERE/../.venv/bin/mitmdump}
  PYTHONUNBUFFERED=1 nohup "$MITMDUMP" --mode regular --listen-host 127.0.0.1 --listen-port 18080 \
    --set confdir="$W/mitm" -s "$HERE/jetty_addon.py" >"$W/mitmdump.log" 2>&1 &
  echo $! >"$W/mitmdump.pid"
  for _ in $(seq 50); do [ -f "$W/mitm/mitmproxy-ca-cert.pem" ] && break; sleep 0.2; done
fi

# 5. Boot. The usernet process inherits JETTY_* from this environment.
export JETTY_PROXY=$PROXY_ADDR JETTY_VM=$VM
if limactl list -q 2>/dev/null | grep -qx "$VM"; then
  limactl start "$VM"
else
  limactl start --tty=false --name "$VM" "$HERE/jetty-proto.yaml"
fi

# 6. Trust the interception CA in the guest (as orca-ssh-setup does today).
limactl shell "$VM" sudo tee /usr/local/share/ca-certificates/jetty-proto.crt <"$W/mitm/mitmproxy-ca-cert.pem" >/dev/null
limactl shell "$VM" sudo update-ca-certificates >/dev/null

"$HERE/checks.sh"
