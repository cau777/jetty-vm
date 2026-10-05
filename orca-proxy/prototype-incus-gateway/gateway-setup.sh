#!/usr/bin/env bash
# PROTOTYPE — runs INSIDE the gateway VM as root (piped via `lxc exec`).
# All netfilter state here lives in the gateway's own kernel, not the host's.
# Inputs (env): UP_MAC PRIV_MAC SERVER_PUB ; client private key on stdin fd 3.
set -euo pipefail

ifname() { for d in /sys/class/net/*; do [ "$(cat "$d/address")" = "$1" ] && basename "$d"; done; true; }
UP=$(ifname "$UP_MAC"); PRIV=$(ifname "$PRIV_MAC")
echo "uplink=$UP private=$PRIV"

sysctl -qw net.ipv4.ip_forward=1 net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.default.rp_filter=0
ip addr replace 10.202.0.1/24 dev "$PRIV"; ip link set "$PRIV" up

# WireGuard client to the host's unprivileged mitmproxy. Key arrives on fd 3 and
# goes straight to a root-only file; it never appears in argv or logs.
install -m 600 /dev/null /etc/wireguard/client.key; cat <&3 >/etc/wireguard/client.key
ip link del wg0 2>/dev/null || true
ip link add wg0 type wireguard
wg set wg0 private-key /etc/wireguard/client.key \
  peer "$SERVER_PUB" endpoint 10.201.0.1:51820 allowed-ips 0.0.0.0/0 persistent-keepalive 25
ip link set wg0 mtu 1420 up

# Policy routing: marked (agent tcp/80,443) packets use table 100 only.
# The unreachable route keeps it fail-closed when wg0 is gone.
ip rule del fwmark 0x1 lookup 100 2>/dev/null || true
ip rule add fwmark 0x1 lookup 100 priority 100
ip route flush table 100 2>/dev/null || true
ip route add default dev wg0 table 100
ip route add unreachable default metric 4000 table 100

iptables -t mangle -F; iptables -t nat -F; iptables -F
iptables -t mangle -A PREROUTING -i "$PRIV" -p tcp -m multiport --dports 80,443 -j MARK --set-mark 1
iptables -t mangle -A FORWARD -o wg0 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu

iptables -P FORWARD DROP
iptables -A FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A FORWARD -i "$PRIV" -o wg0 -p tcp -m multiport --dports 80,443 -j ACCEPT
iptables -A FORWARD -i "$PRIV" -p tcp -m multiport --dports 80,443 -j DROP   # never direct
# Agents may not reach the host or any private/VPN/link-local range directly.
iptables -A FORWARD -i "$PRIV" -d 10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.0.0/16,100.64.0.0/10 -j DROP
iptables -A FORWARD -i "$PRIV" -o "$UP" -j ACCEPT   # other egress: today's semantics
iptables -t nat -A POSTROUTING -o "$UP" -j MASQUERADE   # uplink only; wg0 keeps agent source IPs

# Agents may not talk to the gateway itself (no SSH, no services).
iptables -A INPUT -i "$PRIV" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A INPUT -i "$PRIV" -p icmp -j ACCEPT
iptables -A INPUT -i "$PRIV" -j DROP

ip6tables -P FORWARD DROP
echo "gateway configured"
