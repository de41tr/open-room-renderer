#!/usr/bin/env bash
set -euo pipefail

echo "Watching mDNS/DNS-SD traffic on 224.0.0.251:5353"
if command -v tcpdump >/dev/null 2>&1; then
  echo
  echo "About to use sudo for packet capture."
  echo "Why sudo is needed: tcpdump usually needs elevated privileges to put the network interface into capture mode and read multicast UDP traffic."
  echo "What will be captured: only UDP port 5353 mDNS/DNS-SD packets on local interfaces."
  echo "No files are written by this script unless you redirect the output yourself."
  echo
  echo "Command: sudo tcpdump -ni any -s0 -A udp port 5353"
  exec sudo tcpdump -ni any -s0 -A udp port 5353
fi

python - <<'PY'
import socket, struct
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("", 5353))
mreq = struct.pack("4s4s", socket.inet_aton("224.0.0.251"), socket.inet_aton("0.0.0.0"))
sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
print("tcpdump unavailable; Python mDNS listener active")
while True:
    data, addr = sock.recvfrom(65535)
    print("from", addr, data)
PY
