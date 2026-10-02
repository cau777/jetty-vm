#!/usr/bin/env bash
# PROTOTYPE — throwaway. Gateway-VM topology from research/incus-migration-plan.md,
# run on the host's existing LXD (same model as Incus) in an isolated project.
#
#   agent VMs a,b (one NIC on jpriv: no host IP, MAC/IP filtered)
#        -> gateway VM (jpriv + jpup)
#             tcp/80,443 -> WireGuard -> host mitmproxy wireguard mode + REAL orca-proxy addon
#             other      -> NAT out the uplink
#
# Host side: no sudo, no host firewall writes by us (LXD's daemon manages its bridges).
#   ./up.sh   then   ./checks.sh   then   ./down.sh
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
W=${JETTY_PROTO_DIR:-$HOME/.cache/jetty-incus-proto}
P=jetty-proto
LXC="lxc --project $P"
IMAGE=ubuntu:24.04
SIZE=(-c limits.cpu=2 -c limits.memory=2GiB)
mkdir -p "$W"

# --- throwaway SSH key for host -> gateway -> agent (ProxyJump)
[ -f "$W/id" ] || ssh-keygen -q -t ed25519 -N '' -C jetty-proto -f "$W/id"
PUB=$(cat "$W/id.pub")

# --- project + networks
lxc project show "$P" >/dev/null 2>&1 || lxc project create "$P" -c features.images=false -c features.profiles=true
$LXC profile device show default | grep -q '^root:' || $LXC profile device add default root disk pool=default path=/
lxc network show jpup >/dev/null 2>&1 || lxc network create jpup ipv4.address=10.201.0.1/24 ipv4.nat=true ipv6.address=none
# The agent network: host has no L3 presence on it at all.
lxc network show jpriv >/dev/null 2>&1 || lxc network create jpriv ipv4.address=none ipv6.address=none

# --- host proxy: the real orca-proxy addon, isolated state, stub firewall helper
export ORCA_PROXY_HOME=$W/orca ORCA_PROXY_MANAGEMENT_PORT=18081 ORCA_PROXY_FIREWALL_SCRIPT=$HERE/firewall-stub.py
if ! { [ -f "$W/mitmdump.pid" ] && kill -0 "$(cat "$W/mitmdump.pid")" 2>/dev/null; }; then
  PYTHONUNBUFFERED=1 nohup "$HERE/../.venv/bin/mitmdump" --mode wireguard@10.201.0.1:51820 \
    --set confdir="$ORCA_PROXY_HOME/mitm-confdir" -s "$HERE/../src/orca_proxy/proxy_addon.py" \
    >"$W/mitmdump.log" 2>&1 &
  echo $! >"$W/mitmdump.pid"
  for _ in $(seq 100); do curl -fsS -o /dev/null http://127.0.0.1:18081/readyz 2>/dev/null && break; sleep 0.2; done
fi
WGCONF=$ORCA_PROXY_HOME/mitm-confdir/wireguard.conf
for _ in $(seq 50); do [ -s "$WGCONF" ] && break; sleep 0.2; done
SERVER_PUB=$("$HERE/../.venv/bin/python" - "$WGCONF" <<'EOF'
import base64, json, sys
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
k = json.load(open(sys.argv[1]))
print(base64.b64encode(X25519PrivateKey.from_private_bytes(base64.b64decode(k["server_key"])).public_key().public_bytes_raw()).decode())
EOF
)

userdata() { printf '#cloud-config\nssh_authorized_keys:\n  - %s\n%s\n' "$PUB" "${1:-}"; }
wait_agent() { for _ in $(seq 120); do $LXC exec "$1" -- true 2>/dev/null && return; sleep 2; done; echo "lxd-agent never came up in $1" >&2; exit 1; }

# --- gateway VM
if ! $LXC info gw >/dev/null 2>&1; then
  $LXC init "$IMAGE" gw --vm "${SIZE[@]}" -n jpup
  $LXC config device add gw eth1 nic network=jpriv
  $LXC config set gw cloud-init.user-data "$(userdata 'packages: [wireguard-tools, iptables]')"
  $LXC start gw
fi
wait_agent gw
$LXC exec gw -- cloud-init status --wait >/dev/null || true
UP_MAC=$($LXC config get gw volatile.eth0.hwaddr) PRIV_MAC=$($LXC config get gw volatile.eth1.hwaddr)
CLIENT_KEY=$("$HERE/../.venv/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["client_key"])' "$WGCONF")
# Key is the first stdin line, the script follows; key reaches the setup script on fd 3.
{ echo "$CLIENT_KEY"; cat "$HERE/gateway-setup.sh"; } |
  $LXC exec gw --env UP_MAC="$UP_MAC" --env PRIV_MAC="$PRIV_MAC" --env SERVER_PUB="$SERVER_PUB" -- \
    bash -c 'read -r K; exec 3<<<"$K"; unset K; bash -s'

# --- agent VMs: single filtered NIC on jpriv, static IP via cloud-init
declare -A IP=([a]=10.202.0.11 [b]=10.202.0.12)
for vm in a b; do
  if ! $LXC info "$vm" >/dev/null 2>&1; then
    $LXC init "$IMAGE" "$vm" --vm "${SIZE[@]}"
    $LXC config device add "$vm" eth0 nic network=jpriv ipv4.address="${IP[$vm]}" \
      security.ipv4_filtering=true security.ipv6_filtering=true security.mac_filtering=true
    mac=$($LXC config get "$vm" volatile.eth0.hwaddr)
    $LXC config set "$vm" cloud-init.network-config "$(cat <<EOF
version: 2
ethernets:
  lan:
    match: {macaddress: "$mac"}
    addresses: [${IP[$vm]}/24]
    routes: [{to: default, via: 10.202.0.1}]
    nameservers: {addresses: [1.1.1.1]}
EOF
)"
    $LXC config set "$vm" cloud-init.user-data "$(userdata)"
    $LXC start "$vm"
  fi
  wait_agent "$vm"
  $LXC exec "$vm" -- cloud-init status --wait >/dev/null || true
done

# --- register VMs with the test orca-proxy and trust its CA in the agents
API=http://127.0.0.1:18081/api/v1
for vm in a b; do
  curl -fsS -X PUT "$API/vms/proto-$vm" -H 'Content-Type: application/json' -d "{\"ip_address\": \"${IP[$vm]}\"}" >/dev/null
  curl -fsS "$API/ca" | "$HERE/../.venv/bin/python" -c 'import json,sys; print(json.load(sys.stdin)["certificate_pem"])' |
    $LXC exec "$vm" -- bash -c 'cat >/usr/local/share/ca-certificates/jetty-proto.crt && update-ca-certificates >/dev/null'
done
echo "UP. gateway uplink IP: $($LXC list gw -f csv -c 4 | cut -d' ' -f1)"
