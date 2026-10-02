#!/usr/bin/env bash
# End-to-end checks for the LXD gateway mode, against a running Jetty setup:
#
#   deploy/jetty-lxd setup
#   deploy/jetty-lxd launch test-a --cpus 2 --memory 2GiB --disk 10GiB
#   deploy/jetty-lxd launch test-b --cpus 2 --memory 2GiB --disk 10GiB
#   tests/e2e/lxd-gateway-checks.sh
#
# Point ORCA_PROXY_HOME / ORCA_PROXY_MANAGEMENT_PORT at a test proxy, not the
# real one: this adds a test Credential and Rule, and stops the proxy near the
# end (the proxy pid is read from $ORCA_PROXY_PIDFILE). Guest commands run as
# root: the threat is an agent with sudo.
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
JL=$HERE/../../deploy/jetty-lxd
LXC="lxc --project ${JETTY_LXD_PROJECT:-jetty}"
API=http://127.0.0.1:${ORCA_PROXY_MANAGEMENT_PORT:-8080}/api/v1
PY=${ORCA_PROXY_PYTHON:-$HERE/../../.venv/bin/python}
RDB=${ORCA_PROXY_HOME:-$HOME/.orca-proxy}/requests.sqlite
A=test-a B=test-b
B_IP=$("$JL" ip $B)
fails=0

ag() { $LXC exec "$1" -- bash -c "$2" 2>&1; }
ok() { echo "PASS  $1"; }
ko() { echo "FAIL  $1"; [ -n "${2:-}" ] && echo "      $(tr '\n' ' ' <<<"$2" | cut -c1-300)"; fails=$((fails+1)); }
expect() { if grep -Eq -- "$3" <<<"$2"; then ok "$1"; else ko "$1" "$2"; fi; }
probe() { ag "$1" "timeout 5 bash -c '</dev/tcp/$2/$3' && echo OPEN || echo CLOSED"; }
since() { "$PY" - "$RDB" "$1" <<'EOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
for r in c.execute("select vm_name, destination_ip, destination_port, destination_hostname, outcome, intercepted from connections where id > ? order by id", (int(sys.argv[2]),)):
    print(*r)
EOF
}
mark() { "$PY" -c 'import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("select coalesce(max(id),0) from connections").fetchone()[0])' "$RDB"; }

curl -fsS -X PUT "$API/credentials/e2e-cred" -H 'Content-Type: application/json' \
  -d '{"command": "printf e2e-secret-only-for-a", "ttl_seconds": 0}' >/dev/null
curl -fsS -X PUT "$API/rules/e2e-a-echo" -H 'Content-Type: application/json' -d "{
  \"priority\": 9001, \"vm_selector\": {\"type\": \"only\", \"vms\": [\"$A\"]}, \"hostname\": \"postman-echo.com\",
  \"action\": {\"type\": \"allow_with_credential\", \"credential\": \"e2e-cred\", \"path_prefix\": \"/headers\", \"injection\": {\"type\": \"bearer\"}}}" >/dev/null

echo "--- identity and policy"
m=$(mark)
expect "a: credential injected" "$(ag $A 'curl -sS -m 20 https://postman-echo.com/headers')" 'Bearer e2e-secret-only-for-a'
out=$(ag $B 'curl -sS -m 20 https://postman-echo.com/headers')
if grep -q '"host"' <<<"$out" && ! grep -q e2e-secret <<<"$out"; then ok "b: same host, passthrough, no credential"; else ko "b: passthrough" "$out"; fi
log=$(since "$m")
expect "proxy attributed a (intercepted)" "$log" "^$A [0-9.]+ 443 postman-echo.com .* 1\$"
expect "proxy attributed b (passthrough)" "$log" "^$B [0-9.]+ 443 postman-echo.com allow_default 0\$"
expect "plain HTTP to a public host works" "$(ag $A 'curl -sS -o /dev/null -w "%{http_code}" -m 20 http://example.com/')" '^200$'
expect "plain HTTP never carries a credential" \
  "$(ag $A 'curl -sS -m 20 http://postman-echo.com/headers')" 'credential rules only apply over TLS'

echo "--- guest root tries to escape or impersonate"
expect "a spoofs b's IP -> no traffic" \
  "$(ag $A "ip addr add $B_IP/32 dev enp5s0; curl -sS -m 8 --interface $B_IP https://example.com; echo rc=\$?; ip addr del $B_IP/32 dev enp5s0")" 'rc=(7|28)'
expect "a changes its MAC -> no traffic" \
  "$(ag $A 'mac=$(cat /sys/class/net/enp5s0/address); ip link set enp5s0 address 00:16:3e:de:ad:01; curl -sS -m 8 https://example.com; echo rc=$?; ip link set enp5s0 address $mac')" 'rc=(7|28)'
expect "a works again after restoring its identity" \
  "$(ag $A 'sleep 2; curl -sS -o /dev/null -w "%{http_code}" -m 20 https://example.com')" '^200$'
expect "a cannot reach b (port isolation)" "$(probe $A "$B_IP" 22)" CLOSED
expect "a cannot reach the gateway's services" "$(probe $A 10.202.0.1 22)" CLOSED
expect "a cannot reach the host on the uplink (:22)" "$(probe $A 10.201.0.1 22)" CLOSED
expect "a cannot reach the proxy's Management API port" "$(probe $A 10.201.0.1 "${ORCA_PROXY_MANAGEMENT_PORT:-8080}")" CLOSED
expect "a cannot reach a private address on :443 via the proxy" "$(probe $A 10.201.0.1 443)" CLOSED
expect "non-web egress still works (github.com:22)" "$(probe $A github.com 22)" OPEN
expect "no IPv6 egress" "$(ag $A 'curl -6 -sS -m 5 https://example.com; echo rc=$?')" 'rc=[1-9]'
expect "host has no IPv4 on the agent bridge" "$(ip -4 -o addr show jettypriv0 | wc -l)" '^0$'

echo "--- unregistered source"
curl -fsS -X DELETE "$API/vms/$B" -o /dev/null
m=$(mark)
expect "unregistered b: HTTPS refused" "$(ag $B 'curl -sS -m 10 -o /dev/null https://example.com; echo rc=$?')" 'rc=(35|52|56)'
expect "unregistered b: logged as block_unknown_vm" "$(since "$m")" 'block_unknown_vm'
curl -fsS -X PUT "$API/vms/$B" -H 'Content-Type: application/json' -d "{\"ip_address\": \"$B_IP\"}" -o /dev/null

echo "--- operations"
SSHCFG=$(mktemp); trap 'rm -f "$SSHCFG"' EXIT
"$JL" ssh-config $A >"$SSHCFG"
printf 'Host *\n  StrictHostKeyChecking accept-new\n  UserKnownHostsFile /dev/null\n  LogLevel ERROR\n' >>"$SSHCFG"
expect "host SSH to agent via ProxyJump through the gateway" \
  "$(ssh -F "$SSHCFG" $A 'echo ssh-ok' 2>&1)" 'ssh-ok'
expect "100 MB passthrough download (MTU/MSS)" \
  "$(ag $A 'curl -sS -o /dev/null -m 180 -w "%{http_code} %{size_download}" https://ash-speed.hetzner.com/100MB.bin')" '^200 104857600$'

echo "--- gateway reboot keeps enforcement"
$LXC restart jetty-gw
for _ in $(seq 60); do $LXC exec jetty-gw -- systemctl is-active --quiet jetty-gateway 2>/dev/null && break; sleep 2; done
expect "after gateway reboot: HTTPS works through the proxy again" \
  "$(ag $A 'sleep 3; curl -sS -o /dev/null -w "%{http_code}" -m 20 https://example.com')" '^200$'
expect "after gateway reboot: credential still injected" \
  "$(ag $A 'curl -sS -m 20 https://postman-echo.com/headers')" 'Bearer e2e-secret-only-for-a'

curl -fsS -X DELETE "$API/rules/e2e-a-echo" -o /dev/null
curl -fsS -X DELETE "$API/credentials/e2e-cred" -o /dev/null

echo "--- fail closed"
if [ -n "${ORCA_PROXY_PIDFILE:-}" ]; then
  kill "$(cat "$ORCA_PROXY_PIDFILE")"; sleep 1
  expect "proxy down -> HTTPS fails" "$(ag $A 'curl -sS -m 10 -o /dev/null https://example.com; echo rc=$?')" 'rc=(7|28|35|56)'
  expect "proxy down -> nothing forwarded direct" \
    "$($LXC exec jetty-gw -- iptables -L FORWARD -v -n -x | grep 'multiport dports 80,443' | grep DROP)" '^ +0 +0 '
else
  echo "SKIP  proxy-down checks (set ORCA_PROXY_PIDFILE)"
fi
$LXC exec jetty-gw -- ip link del wg0
expect "tunnel deleted -> no direct path" "$(ag $A 'curl -sS -m 10 -o /dev/null https://example.com; echo rc=$?')" 'rc=(7|28)'
$LXC exec jetty-gw -- systemctl restart jetty-gateway

echo
[ $fails = 0 ] && echo "ALL CHECKS PASSED" || echo "$fails CHECK(S) FAILED"
[ $fails = 0 ]
