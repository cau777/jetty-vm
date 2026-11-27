#!/usr/bin/env bash
# End-to-end checks for Jetty's centralized architecture against a running Jetty setup:
#
#   jetty gateway setup
#   jetty vm create test-a --cpus 2 --memory 2GiB --disk 10GiB
#   jetty vm create test-b --cpus 2 --memory 2GiB --disk 10GiB
#   tests/e2e/lxd-gateway-checks.sh
#
# Point ORCA_PROXY_HOME / JETTY_LISTEN_PORT / JETTY_ADDR at a test daemon, not
# the real one: this adds a test Credential and Rule, and stops the daemon near
# the end (the daemon pid is read from $JETTY_DAEMON_PIDFILE).
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
JETTY=${JETTY_CLI:-jetty}
LXC="lxc --project ${JETTY_LXD_PROJECT:-jetty}"
PORT=${JETTY_LISTEN_PORT:-7700}
SDB=${ORCA_PROXY_HOME:-$HOME/.orca-proxy}/state.sqlite
RDB=${ORCA_PROXY_HOME:-$HOME/.orca-proxy}/requests.sqlite
A=test-a B=test-b
B_IP=$("$JETTY" vm ip "$B")
fails=0

ag() { $LXC exec "$1" -- bash -c "$2" 2>&1; }
ok() { echo "PASS  $1"; }
ko() { echo "FAIL  $1"; [ -n "${2:-}" ] && echo "      $(tr '\n' ' ' <<<"$2" | cut -c1-300)"; fails=$((fails+1)); }
expect() { if grep -Eq -- "$3" <<<"$2"; then ok "$1"; else ko "$1" "$2"; fi; }
probe() { ag "$1" "timeout 5 bash -c '</dev/tcp/$2/$3' && echo OPEN || echo CLOSED"; }
since() { python3 - "$RDB" "$1" <<'EOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
for r in c.execute("select vm_name, destination_ip, destination_port, destination_hostname, outcome, intercepted from connections where id > ? order by id", (int(sys.argv[2]),)):
    print(*r)
EOF
}
mark() { python3 -c 'import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("select coalesce(max(id),0) from connections").fetchone()[0])' "$RDB"; }

"$JETTY" credential put e2e-cred --command "printf e2e-secret-only-for-a" --ttl 0 >/dev/null
"$JETTY" rule put e2e-a-echo --vm "$A" --priority 9001 --hostname postman-echo.com \
  --action allow_with_credential --credential e2e-cred --path-prefix /headers --injection bearer >/dev/null

echo "--- identity and policy"
m=$(mark)
expect "a: credential injected" "$(ag "$A" 'curl -sS -m 20 https://postman-echo.com/headers')" 'Bearer e2e-secret-only-for-a'
out=$(ag "$B" 'curl -sS -m 20 https://postman-echo.com/headers')
if grep -q '"host"' <<<"$out" && ! grep -q e2e-secret <<<"$out"; then ok "b: same host, passthrough, no credential"; else ko "b: passthrough" "$out"; fi
log=$(since "$m")
expect "proxy attributed a (intercepted)" "$log" "^$A [0-9.]+ 443 postman-echo.com .* 1\$"
expect "proxy attributed b (passthrough)" "$log" "^$B [0-9.]+ 443 postman-echo.com allow_default 0\$"
expect "plain HTTP to a public host works" "$(ag "$A" 'curl -sS -o /dev/null -w "%{http_code}" -m 20 http://example.com/')" '^200$'
expect "plain HTTP never carries a credential" \
  "$(ag "$A" 'curl -sS -m 20 http://postman-echo.com/headers')" 'credential rules only apply over TLS'

echo "--- guest root tries to escape or impersonate"
expect "a spoofs b's IP -> no traffic" \
  "$(ag "$A" "ip addr add $B_IP/32 dev enp5s0; curl -sS -m 8 --interface $B_IP https://example.com; echo rc=\$?; ip addr del $B_IP/32 dev enp5s0")" 'rc=(7|28)'
expect "a changes its MAC -> no traffic" \
  "$(ag "$A" 'mac=$(cat /sys/class/net/enp5s0/address); ip link set enp5s0 address 00:16:3e:de:ad:01; curl -sS -m 8 https://example.com; echo rc=$?; ip link set enp5s0 address $mac')" 'rc=(7|28)'
expect "a works again after restoring its identity" \
  "$(ag "$A" 'sleep 2; curl -sS -o /dev/null -w "%{http_code}" -m 20 https://example.com')" '^200$'
expect "a cannot reach b (port isolation)" "$(probe "$A" "$B_IP" 22)" CLOSED
expect "a cannot reach the gateway's SSH service" "$(probe "$A" 10.202.0.1 22)" CLOSED
expect "a cannot open the gateway's mitmproxy listener directly" "$(probe "$A" 10.202.0.1 8080)" CLOSED
expect "a cannot reach the host on the uplink (:22)" "$(probe "$A" 10.201.0.1 22)" CLOSED
expect "a cannot reach jettyd on the uplink" "$(probe "$A" 10.201.0.1 "$PORT")" CLOSED
expect "a cannot reach a private address on :443 via the proxy" "$(probe "$A" 10.201.0.1 443)" CLOSED
expect "non-web egress still works (github.com:22)" "$(probe "$A" github.com 22)" OPEN
expect "no IPv6 egress" "$(ag "$A" 'curl -6 -sS -m 5 https://example.com; echo rc=$?')" 'rc=[1-9]'
expect "host has no IPv4 on the agent bridge" "$(ip -4 -o addr show jettypriv0 | wc -l)" '^0$'

echo "--- gateway listener trust checks (10.201.0.1)"
expect "host without 10.201.0.2 source is refused on 10.201.0.1:$PORT" \
  "$(curl -sS -m 3 "http://10.201.0.1:$PORT/jetty.v1.Jetty/GetStatus" 2>&1 || echo REFUSED)" '(REFUSED|Empty reply|Connection reset|Failed)'
expect "gateway call without bearer token is rejected" \
  "$($LXC exec jetty-gw -- curl -sS -o /dev/null -w "%{http_code}" -m 5 -X POST -H 'Content-Type: application/json' -d '{}' "http://10.201.0.1:$PORT/jetty.v1.Jetty/GetStatus" 2>&1 || true)" '^401$'

echo "--- unregistered source"
python3 -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("delete from vms where name=?", (sys.argv[2],)); c.commit()' "$SDB" "$B"
m=$(mark)
expect "unregistered b: HTTPS refused" "$(ag "$B" 'curl -sS -m 10 -o /dev/null https://example.com; echo rc=$?')" 'rc=(35|52|56)'
expect "unregistered b: logged as block_unknown_vm" "$(since "$m")" 'block_unknown_vm'
"$JETTY" vm put "$B" "$B_IP" >/dev/null

echo "--- operations and concurrent throughput"
SSHCFG=$(mktemp); trap 'rm -f "$SSHCFG"' EXIT
"$JETTY" ssh-config "$A" >"$SSHCFG"
printf 'Host *\n  StrictHostKeyChecking accept-new\n  UserKnownHostsFile /dev/null\n  LogLevel ERROR\n' >>"$SSHCFG"
expect "host SSH to agent in one hop (gateway port forward)" \
  "$(ssh -F "$SSHCFG" "$A" 'echo ssh-ok' 2>&1)" 'ssh-ok'
A_PORT=$(awk '/^ *Port /{print $2}' "$SSHCFG")
expect "b cannot use a's forwarded SSH port on the gateway" "$(probe "$B" 10.201.0.2 "$A_PORT")" CLOSED
expect "100 MB passthrough download" \
  "$(ag "$A" 'curl -sS -o /dev/null -m 180 -w "%{http_code} %{size_download}" https://ash-speed.hetzner.com/100MB.bin')" '^200 104857600$'

# Concurrent multi-agent download check
ag "$A" 'curl -sS -o /dev/null -m 120 -w "a:%{http_code}:%{size_download}\n" https://ash-speed.hetzner.com/100MB.bin' >/tmp/dl-a &
pid_a=$!
ag "$B" 'curl -sS -o /dev/null -m 120 -w "b:%{http_code}:%{size_download}\n" https://ash-speed.hetzner.com/100MB.bin' >/tmp/dl-b &
pid_b=$!
wait "$pid_a" "$pid_b"
expect "concurrent download a" "$(cat /tmp/dl-a)" '^a:200:104857600$'
expect "concurrent download b" "$(cat /tmp/dl-b)" '^b:200:104857600$'
rm -f /tmp/dl-a /tmp/dl-b

echo "--- gateway reboot keeps enforcement"
$LXC restart jetty-gw
for _ in $(seq 60); do $LXC exec jetty-gw -- systemctl is-active --quiet jetty-gw-agent 2>/dev/null && break; sleep 2; done
expect "after gateway reboot: HTTPS works through the proxy again" \
  "$(ag "$A" 'sleep 3; curl -sS -o /dev/null -w "%{http_code}" -m 20 https://example.com')" '^200$'
expect "after gateway reboot: credential still injected" \
  "$(ag "$A" 'curl -sS -m 20 https://postman-echo.com/headers')" 'Bearer e2e-secret-only-for-a'

"$JETTY" rule delete e2e-a-echo --vm "$A" >/dev/null
"$JETTY" credential delete e2e-cred >/dev/null

echo "--- fail closed"
if [ -n "${JETTY_DAEMON_PIDFILE:-}" ]; then
  kill "$(cat "$JETTY_DAEMON_PIDFILE")"; sleep 1
  expect "jettyd down -> HTTPS fails" "$(ag "$A" 'curl -sS -m 10 -o /dev/null https://example.com; echo rc=$?')" 'rc=(7|28|35|52|56)'
  expect "jettyd down -> nothing forwarded direct" \
    "$($LXC exec jetty-gw -- iptables -L FORWARD -v -n -x | grep 'multiport dports 80,443' | grep DROP)" '^ +0 +0 '
else
  echo "SKIP  jettyd-down checks (set JETTY_DAEMON_PIDFILE)"
fi
$LXC exec jetty-gw -- systemctl stop jetty-gw-agent
expect "gateway agent stopped -> no direct web path" "$(ag "$A" 'curl -sS -m 10 -o /dev/null https://example.com; echo rc=$?')" 'rc=(7|28)'
$LXC exec jetty-gw -- systemctl start jetty-gw-agent

echo
[ $fails = 0 ] && echo "ALL CHECKS PASSED" || echo "$fails CHECK(S) FAILED"
[ $fails = 0 ]
