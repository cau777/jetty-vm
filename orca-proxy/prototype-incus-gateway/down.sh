#!/usr/bin/env bash
# PROTOTYPE — remove everything up.sh created (project, VMs, networks, proxy).
W=${JETTY_PROTO_DIR:-$HOME/.cache/jetty-incus-proto}
P=jetty-proto
for vm in a b gw; do lxc --project $P delete -f $vm 2>/dev/null; done
lxc --project $P profile device remove default root 2>/dev/null
lxc project delete $P 2>/dev/null
lxc network delete jpriv 2>/dev/null; lxc network delete jpup 2>/dev/null
[ -f "$W/mitmdump.pid" ] && kill "$(cat "$W/mitmdump.pid")" 2>/dev/null
echo "down (state left in $W; rm -rf it to wipe the test CA/DB/key)"
