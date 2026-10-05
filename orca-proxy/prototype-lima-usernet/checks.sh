#!/usr/bin/env bash
# PROTOTYPE — throwaway. Bypass checks against the running prototype VM.
# Every guest command runs as root: the threat is an agent with sudo.
set -uo pipefail

W=${JETTY_PROTO_DIR:-$HOME/.cache/jetty-lima-proto}
export LIMA_HOME=$W/lima-home PATH=$W/lima/bin:$W/qbin:$PATH
VM=proto
LOG=$W/mitmdump.log
GW_HOST=192.168.110.2   # usernet gateway; Lima NATs it to host 127.0.0.1
fails=0

g() { limactl shell "$VM" sudo bash -c "$1" 2>&1; }
check() { # name, guest-cmd, expected-regex-in-output, [proxy-log-regex]
  local out mark=0
  out=$(g "$2")
  grep -Eq -- "$3" <<<"$out" || mark=1
  if [ -n "${4:-}" ]; then sleep 0.5; grep -Eq -- "$4" "$LOG" || mark=1; fi
  if [ $mark = 0 ]; then echo "PASS  $1"; else echo "FAIL  $1"; echo "      guest: $(tr '\n' ' ' <<<"$out" | cut -c1-300)"; fails=$((fails+1)); fi
}

echo "--- guest network view"
g 'ip -br addr; ip route; env | grep -i proxy || echo "(no proxy env in guest)"'
echo

check "HTTPS passthrough via proxy, no proxy env" \
  'curl -sS -o /dev/null -w "%{http_code}" https://example.com' '^200$' \
  'PASSTHROUGH vm=proto sni=example.com'
check "plain HTTP :80 goes through proxy" \
  'curl -sS -o /dev/null -w "%{http_code}" http://example.com/' '^200$' \
  'REQUEST vm=proto GET http://example.com/'
check "credential injected on matching host+path" \
  'curl -sS https://postman-echo.com/headers' 'Bearer PROTOTYPE-HOST-HELD-SECRET' \
  'REQUEST vm=proto GET https://postman-echo.com/headers injected=True'
check "no injection outside path prefix" \
  'curl -sS https://postman-echo.com/get' '"host"' \
  'REQUEST vm=proto GET https://postman-echo.com/get injected=False'
check "root adds a second IP and sources from it -> still proxied" \
  'ip addr add 192.168.110.77/24 dev eth0 2>/dev/null; curl -sS -o /dev/null -w "%{http_code}" --interface 192.168.110.77 https://www.wikipedia.org' '^200$' \
  'PASSTHROUGH vm=proto sni=www.wikipedia.org'
check "root changes MAC+IP entirely -> still proxied" \
  'GW=192.168.110.2; mac=$(cat /sys/class/net/eth0/address); ip link set eth0 down; ip link set eth0 address 52:54:00:de:ad:01; ip link set eth0 up; ip addr flush dev eth0; ip addr add 192.168.110.99/24 dev eth0; ip route add default via $GW; sleep 1; curl -sS -o /dev/null -w "%{http_code}" https://www.debian.org; ip link set eth0 down; ip link set eth0 address $mac; ip link set eth0 up; ip addr flush dev eth0; networkctl reconfigure eth0; sleep 3' '^200' \
  'PASSTHROUGH vm=proto sni=www.debian.org'
check "SNI-less HTTPS to a bare IP is still forced through proxy" \
  'curl -sS -k -o /dev/null -w "%{http_code}" https://1.1.1.1/' '^[0-9]{3}$' \
  'CONNECT vm=proto dst=1.1.1.1:443'
check "host loopback via gateway NAT refused (Management API :8080)" \
  "curl -sS -m 5 http://$GW_HOST:8080/readyz; echo rc=\$?" 'rc=(7|56|52)'
check "host loopback via gateway NAT refused (sshd :22)" \
  "timeout 5 bash -c '</dev/tcp/$GW_HOST/22' && echo OPEN || echo CLOSED" 'CLOSED'
check "non-web TCP still allowed (today's semantics): github.com:22" \
  "timeout 5 bash -c '</dev/tcp/github.com/22' && echo OPEN || echo CLOSED" 'OPEN'
check "IPv6 egress has no route" \
  'curl -6 -sS -m 5 https://example.com; echo rc=$?' 'rc=[1-9]'

echo
echo "--- fail-closed: stop the proxy, retry"
kill "$(cat "$W/mitmdump.pid")"; sleep 1
check "proxy down -> HTTPS fails, does not go direct" \
  'curl -sS -m 10 -o /dev/null -w "%{http_code}" https://example.com; echo " rc=$?"' 'rc=(7|35|56|52)'
echo "(restart the proxy with ./run.sh)"

echo
echo "--- usernet process privileges"
pid=$(pgrep -f "limactl usernet" | head -1)
ps -o user=,pid=,args= -p "$pid" | cut -c1-160
grep -E '^Cap(Prm|Eff|Amb)' "/proc/$pid/status"

echo
[ $fails = 0 ] && echo "ALL CHECKS PASSED" || echo "$fails CHECK(S) FAILED"
