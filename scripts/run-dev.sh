#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
if [[ ! -d .venv ]]; then
  echo "Missing .venv. Run ./scripts/dev-setup.sh first." >&2
  exit 2
fi
. .venv/bin/activate
IP="$(ip route get 8.8.8.8 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')"
IP="${IP:-127.0.0.1}"
echo "Starting Open Room Renderer developer diagnostics mode"
echo "Laptop IP: ${IP}"
echo "Status: curl \"http://${IP}/httpapi.asp?command=getStatusEx\" | jq"
echo "Description: curl \"http://${IP}/description.xml\""
echo "Acceptance: ./scripts/acceptance-open-source-client.sh"
echo "For normal app discovery, prefer: ./scripts/run.sh"
echo
echo "About to use sudo for privileged port binding."
echo "Why sudo is needed: Open Room Renderer now defaults to HTTP port 80, and Linux normally restricts ports below 1024 to privileged processes."
echo "Why port 80 is the default: some controller apps probe plain http://device-ip/ during discovery or setup even when discovery advertises another port."
echo "What will run with sudo: only the renderer process, so it can bind port 80."
echo
if command -v open-room-renderer >/dev/null 2>&1; then
  exec sudo -E .venv/bin/open-room-renderer serve --config configs/config.example.yaml
fi
exec sudo -E .venv/bin/fake-linkplay serve --config configs/config.example.yaml
