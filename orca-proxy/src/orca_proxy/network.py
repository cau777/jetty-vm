"""Linux interface helpers for the WireGuard listener."""

import fcntl
import socket
import struct

TUNNEL_HOST = "10.201.0.1"
TUNNEL_PORT = 51820


def host_has_ipv4(address: str) -> bool:
    target = socket.inet_aton(address)
    for _index, name in socket.if_nameindex():
        try:
            request = struct.pack("256s", name.encode()[:15])
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                result = fcntl.ioctl(sock.fileno(), 0x8915, request)  # SIOCGIFADDR
            if result[20:24] == target:
                return True
        except OSError:
            continue
    return False
