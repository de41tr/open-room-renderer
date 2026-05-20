#!/usr/bin/env bash
set -euo pipefail

echo "Watching UDP SSDP traffic on 239.255.255.250:1900"
if command -v tcpdump >/dev/null 2>&1; then
  echo
  echo "About to use sudo for packet capture."
  echo "Why sudo is needed: tcpdump usually needs elevated privileges to put the network interface into capture mode and read multicast UDP traffic."
  echo "What will be captured: only UDP port 1900 SSDP discovery packets on local interfaces."
  echo "No files are written by this script unless you redirect the output yourself."
  echo
  echo "Command: sudo tcpdump -ni any -s0 -A udp port 1900"
  exec sudo tcpdump -ni any -s0 -A udp port 1900
fi

python - <<'PY'
import socket, struct
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("", 1900))
mreq = struct.pack("4s4s", socket.inet_aton("239.255.255.250"), socket.inet_aton("0.0.0.0"))
sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
print("tcpdump unavailable; Python listener active")
while True:
    data, addr = sock.recvfrom(65535)
    print("from", addr)
    print(data.decode(errors="ignore"))
PY
