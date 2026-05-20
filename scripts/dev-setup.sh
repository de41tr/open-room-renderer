#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "[setup] Open Room Renderer needs these system programs:"
echo "        python3, python3-venv or equivalent venv support, mpv, curl, jq, ip/iproute2"
echo "        optional for packet sniffing: tcpdump"
echo
echo "[setup] Please install them with your distro's package manager."
echo "        Package names vary by distribution, especially Python venv support and iproute2."
echo

missing=0
for tool in python mpv curl jq ip; do
  if command -v "$tool" >/dev/null 2>&1; then
    echo "[setup] found: $tool"
  else
    echo "[setup] missing: $tool"
    missing=1
  fi
done

if ! python -m venv --help >/dev/null 2>&1; then
  echo "[setup] missing: Python venv support"
  missing=1
else
  echo "[setup] found: Python venv support"
fi

if [[ "$missing" == "1" ]]; then
  echo
  echo "[setup] Some system dependencies are missing."
  echo "        Install them first, then run this script again."
  exit 2
fi

echo
echo "[setup] Creating virtual environment"
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

echo
echo "Next steps:"
echo "  ./scripts/run.sh"
echo "  ./scripts/test-curl.sh"
echo "  ./scripts/acceptance-open-source-client.sh"
echo
echo "Developer diagnostics only:"
echo "  ./scripts/run-dev.sh"
echo
echo "The acceptance gate is acceptance-open-source-client.sh; curl-only success is diagnostics only."
