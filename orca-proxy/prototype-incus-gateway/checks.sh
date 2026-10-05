#!/usr/bin/env bash
# PROTOTYPE — throwaway. Gate checks for the gateway topology (run after ./up.sh).
# Guest commands run as root: the threat is an agent with sudo.
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
W=${JETTY_PROTO_DIR:-$HOME/.cache/jetty-incus-proto}
LXC="lxc --project jetty-proto"
API=http://127.0.0.1:18081/api/v1
PY=$HERE/../.venv/bin/python
RDB=$W/orca/requests.sqlite
GW_UP=$($LXC list gw -f csv -c 4 | tr ' ",' '\n\n\n' | grep '^10\.201\.')
fails=0

ag() { $LXC exec "$1" -- bash -c "$2" 2>&1; }
ok() { echo "PASS  $1"; }
ko() { echo "FAIL  $1"; [ -n "${2:-}" ] && echo "      $(tr '\n' ' ' <<<"$2" | cut -c1-300)"; fails=$((fails+1)); }
expect() { if grep -Eq -- "$3" <<<"$2"; then ok "$1"; else ko "$1" "$2"; fi; }
# connections logged since a marker id: "vm dst_ip:port host outcome intercepted"
since() { "$PY" - "$RDB" "$1" <<'EOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
for r in c.execute("select vm_name, destination_ip, destination_port, destination_hostname, outcome, intercepted from connections where id > ? order by id", (int(sys.argv[2]),)):
    print(*r)
EOF
}
mark() { "$PY" -c 'import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("select coalesce(max(id),0) from connections").fetchone()[0])' "$RDB"; }

# --- policy: credential only for proto-a on postman-echo.com/headers
curl -fsS -X PUT "$API/credentials/proto-cred" -H 'Content-Type: application/json' \
  -d '{"command": "printf proto-secret-only-for-a", "ttl_seconds": 0}' >/dev/null
curl -fsS -X PUT "$API/rules/proto-a-echo" -H 'Content-Type: application/json' -d '{
  "priority": 10, "vm_selector": {"type": "only", "vms": ["proto-a"]}, "hostname": "postman-echo.com",
  "action": {"type": "allow_with_credential", "credential": "proto-cred", "path_prefix": "/headers", "injection": {"type": "bearer"}}}' >/dev/null

echo "--- identity & policy through one WireGuard peer"
m=$(mark)
expect "a: credential injected (rule targets proto-a)" \
  "$(ag a 'curl -sS -m 20 https://postman-echo.com/headers')" 'Bearer proto-secret-only-for-a'
out=$(ag b 'curl -sS -m 20 https://postman-echo.com/headers')
if grep -q '"host"' <<<"$out" && ! grep -q proto-secret <<<"$out"; then ok "b: same host, no injection, TLS passthrough"; else ko "b: same host, no injection" "$out"; fi
log=$(since "$m")
expect "proxy saw a's real source IP (proto-a, intercepted)" "$log" '^proto-a [0-9.]+ 443 postman-echo.com .* 1$'
expect "proxy saw b's real source IP (proto-b, passthrough)" "$log" '^proto-b [0-9.]+ 443 postman-echo.com allow_default 0$'
expect "original destination IP preserved (not the gateway/tunnel IP)" "$log" '^proto-a (104|162|172|18|3|5|34|35|44|52|54)\.'

m=$(mark)
for i in 1 2 3 4 5; do
  ag a 'curl -sS -o /dev/null -m 20 https://example.com' >/dev/null &
  ag b 'curl -sS -o /dev/null -m 20 https://www.wikipedia.org' >/dev/null &
done; wait
log=$(since "$m")
na=$(grep -c '^proto-a .* example.com' <<<"$log"); nb=$(grep -c '^proto-b .* www.wikipedia.org' <<<"$log")
bad=$(grep -Ec '^proto-a .* www.wikipedia.org|^proto-b .* example.com|^unknown|^10\.' <<<"$log")
[ "$na" = 5 ] && [ "$nb" = 5 ] && [ "$bad" = 0 ] && ok "concurrent a/b: 10/10 attributed correctly" || ko "concurrent attribution a=$na b=$nb bad=$bad" "$log"

m=$(mark)
expect "plain HTTP :80 works" "$(ag a 'curl -sS -o /dev/null -w "%{http_code}" -m 20 http://example.com/')" '^200$'
echo "      :80 connection rows logged: $(since "$m" | grep -c ' 80 ' || true); http_requests rows total: $("$PY" -c 'import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("select count(*) from http_requests").fetchone()[0])' "$RDB")  (characterization)"

echo "--- guest root tries to escape or impersonate"
expect "a spoofs b's IP -> no traffic" \
  "$(ag a 'ip addr add 10.202.0.12/32 dev enp5s0; curl -sS -m 8 --interface 10.202.0.12 https://postman-echo.com/headers; echo rc=$?; ip addr del 10.202.0.12/32 dev enp5s0')" 'rc=(7|28)'
expect "a uses an unassigned IP -> no traffic" \
  "$(ag a 'ip addr add 10.202.0.99/24 dev enp5s0; curl -sS -m 8 --interface 10.202.0.99 https://example.com; echo rc=$?; ip addr del 10.202.0.99/24 dev enp5s0')" 'rc=(7|28)'
expect "a changes its MAC -> no traffic" \
  "$(ag a 'mac=$(cat /sys/class/net/enp5s0/address); ip link set enp5s0 address 00:16:3e:de:ad:01; curl -sS -m 8 https://example.com; echo rc=$?; ip link set enp5s0 address $mac')" 'rc=(7|28)'
expect "a still works after restoring its identity" \
  "$(ag a 'sleep 2; curl -sS -o /dev/null -w "%{http_code}" -m 20 https://example.com')" '^200$'
expect "a claims the gateway IP 10.202.0.1 -> no hijack (filtered)" \
  "$(ag b 'curl -sS -o /dev/null -w "%{http_code}" -m 20 https://example.com' & ag a 'ip addr add 10.202.0.1/32 dev enp5s0; arping -c2 -U -I enp5s0 10.202.0.1 >/dev/null 2>&1; sleep 1; ip addr del 10.202.0.1/32 dev enp5s0'; wait)" '200'
expect "a cannot reach the gateway's own services (ssh)" \
  "$(ag a 'timeout 5 bash -c "</dev/tcp/10.202.0.1/22" && echo OPEN || echo CLOSED')" 'CLOSED'
expect "a cannot reach host via uplink IP (10.201.0.1:22)" \
  "$(ag a 'timeout 5 bash -c "</dev/tcp/10.201.0.1/22" && echo OPEN || echo CLOSED')" 'CLOSED'
expect "a cannot reach host LAN IP (10.0.0.178:22)" \
  "$(ag a 'timeout 5 bash -c "</dev/tcp/10.0.0.178/22" && echo OPEN || echo CLOSED')" 'CLOSED'
expect "a cannot reach host's Multipass bridge (10.14.105.1:22)" \
  "$(ag a 'timeout 5 bash -c "</dev/tcp/10.14.105.1/22" && echo OPEN || echo CLOSED')" 'CLOSED'
m=$(mark)
ag a 'curl -sS -m 8 -o /dev/null https://10.201.0.1/ ; true' >/dev/null
echo "      a -> 10.201.0.1:443 went to proxy as: $(since "$m" | head -1)  (host :80/:443 reachable via proxy — characterization)"
echo "      a -> b:22 (same L2, no port isolation): $(ag a 'timeout 5 bash -c "</dev/tcp/10.202.0.12/22" && echo OPEN || echo CLOSED')  (characterization)"
expect "non-web egress still works (github.com:22, today's semantics)" \
  "$(ag a 'timeout 8 bash -c "</dev/tcp/github.com/22" && echo OPEN || echo CLOSED')" 'OPEN'
expect "IPv6 egress has no path" "$(ag a 'curl -6 -sS -m 5 https://example.com; echo rc=$?')" 'rc=[1-9]'
expect "host has no IPv4 on the agent bridge" "$(ip -4 -o addr show jpriv | wc -l)" '^0$'

echo "--- operations"
expect "host SSH to agent via ProxyJump through gateway" \
  "$(ssh -i "$W/id" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$W/known_hosts" \
      -o ProxyCommand="ssh -i $W/id -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$W/known_hosts -W %h:%p ubuntu@$GW_UP" \
      ubuntu@10.202.0.11 'echo ssh-ok' 2>&1)" 'ssh-ok'
expect "large passthrough download (100 MB) through tunnel (MTU/MSS)" \
  "$(ag a 'curl -sS -o /dev/null -m 180 -w "%{http_code} %{size_download} %{speed_download}" https://ash-speed.hetzner.com/100MB.bin' | tee "$W/speed.txt")" '^200 104857600 '
echo "      throughput: $(awk '{printf "%.1f MB/s", $3/1e6}' "$W/speed.txt")"
expect "large intercepted (MITM) response through tunnel" \
  "$(ag a 'curl -sS -o /dev/null -m 60 -w "%{http_code} %{size_download}" "https://postman-echo.com/headers?pad=$(head -c 6000 /dev/zero | tr "\0" a)"')" '^200 '

echo "--- fail closed"
kill "$(cat "$W/mitmdump.pid")"; sleep 1
expect "proxy down -> HTTPS fails, nothing goes direct" "$(ag a 'curl -sS -m 10 -o /dev/null https://example.com; echo rc=$?')" 'rc=(7|28|35|56)'
expect "proxy down -> gateway direct-443 DROP counter is 0" \
  "$(ag gw 'iptables -L FORWARD -v -n -x | grep "multiport dports 80,443" | grep DROP')" '^ +0 +0 '
ag gw 'ip link del wg0'
expect "tunnel deleted -> unreachable route, still no direct path" "$(ag a 'curl -sS -m 10 -o /dev/null https://example.com; echo rc=$?')" 'rc=(7|28)'
$LXC stop gw
for _ in $(seq 30); do $LXC start gw 2>/dev/null && break; sleep 2; done   # LXD may refuse an immediate restart
for _ in $(seq 60); do $LXC exec gw -- true 2>/dev/null && break; sleep 2; done
[ "$($LXC list gw -f csv -c s)" = RUNNING ] || ko "gateway did not come back up"
expect "gateway rebooted (config not persisted) -> agents have no egress at all" \
  "$(ag a 'curl -s -m 10 -o /dev/null https://example.com; echo -n "rc=$? "; timeout 5 bash -c "</dev/tcp/1.1.1.1/53" 2>/dev/null && echo DNS-OPEN || echo DNS-CLOSED')" 'rc=(7|28) .*DNS-CLOSED'
echo "(re-run ./up.sh to restore proxy + gateway)"

echo "--- privileges"
echo "      host processes involved: mitmdump as $(ps -o user= -p "$(pgrep -f 'mitmdump --mode wireguard' | head -1)" 2>/dev/null || echo '(stopped)'); no sudo, no host iptables/nft writes by the prototype"

echo
[ $fails = 0 ] && echo "ALL CHECKS PASSED" || echo "$fails CHECK(S) FAILED"
