#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p logs/acceptance
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="logs/acceptance/acceptance-${STAMP}.log"
SERVER_LOG="logs/acceptance/server-${STAMP}.log"
HOST="${HOST:-127.0.0.1}"
API_PORT="${API_PORT:-8080}"
UPNP_PORT="${UPNP_PORT:-8080}"
NAME="${NAME:-Open Room Renderer}"
TEST_URL="${TEST_URL:-https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3}"
KEEP_SERVER="${KEEP_SERVER:-0}"
SERVER_PID=""
TEMP_CONFIG=""

exec > >(tee -a "$LOG") 2>&1

fail() {
  echo "FAIL: $*"
  echo "Logs: $LOG $SERVER_LOG"
  exit 1
}

if [[ ! -d .venv ]]; then
  fail "Missing .venv. Run ./scripts/dev-setup.sh first."
fi
. .venv/bin/activate

echo "[1/7] Checking or starting fake renderer"
if curl -fsS "http://${HOST}:${API_PORT}/httpapi.asp?command=getStatusEx" >/dev/null 2>&1; then
  echo "Renderer already running on ${HOST}:${API_PORT}"
else
  TEMP_CONFIG="logs/acceptance/config-${STAMP}.yaml"
  python - "$API_PORT" "$UPNP_PORT" "$TEMP_CONFIG" <<'PY' || fail "could not prepare temporary acceptance config"
from pathlib import Path
import sys
import yaml

api_port = int(sys.argv[1])
upnp_port = int(sys.argv[2])
output = Path(sys.argv[3])
data = yaml.safe_load(Path("configs/config.example.yaml").read_text()) or {}
data["http_api_port"] = api_port
data["upnp_port"] = upnp_port
data["state_dir"] = f".state/acceptance-{api_port}-{upnp_port}"
data["mpv_socket"] = f"{data['state_dir']}/mpv.sock"
data["enable_mdns"] = False
data["enable_ssdp"] = True
output.write_text(yaml.safe_dump(data, sort_keys=False))
PY
  if (( API_PORT < 1024 || UPNP_PORT < 1024 )); then
    fail "Start the explicitly authorized port-80 service first, or use API_PORT=8080 UPNP_PORT=8080. This check never invokes sudo."
  fi
  .venv/bin/open-room-renderer serve --config "$TEMP_CONFIG" >"$SERVER_LOG" 2>&1 &
  SERVER_PID="$!"
  echo "Started renderer pid=${SERVER_PID}"
fi

cleanup() {
  if [[ -n "${SERVER_PID}" && "${KEEP_SERVER}" != "1" ]]; then
    kill "${SERVER_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

wait_http() {
  local url="$1"
  for _ in $(seq 1 40); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

if [[ -n "${SERVER_PID}" ]]; then
  sleep 0.5
  if ! kill -0 "${SERVER_PID}" >/dev/null 2>&1; then
    fail "renderer exited during startup; likely port conflict. See $SERVER_LOG"
  fi
fi

echo "[2/7] Verifying /description.xml"
wait_http "http://${HOST}:${UPNP_PORT}/description.xml" || fail "description.xml unreachable"

echo "[3/7] Verifying getStatusEx"
wait_http "http://${HOST}:${API_PORT}/httpapi.asp?command=getStatusEx" || fail "getStatusEx unreachable"

echo "[4/7] Verifying local SSDP M-SEARCH"
python - "$HOST" "$UPNP_PORT" <<'PY' || fail "SSDP discovery failed"
import socket, sys, time
host, port = sys.argv[1], sys.argv[2]
msg = "\r\n".join([
    "M-SEARCH * HTTP/1.1",
    "HOST: 239.255.255.250:1900",
    'MAN: "ssdp:discover"',
    "MX: 1",
    "ST: urn:schemas-upnp-org:device:MediaRenderer:1",
    "", "",
]).encode()
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
sock.settimeout(4)
sock.sendto(msg, ("239.255.255.250", 1900))
deadline = time.time() + 4
while time.time() < deadline:
    try:
        data, addr = sock.recvfrom(65535)
    except socket.timeout:
        break
    text = data.decode(errors="ignore")
    print(text.splitlines()[0:8])
    if f":{port}/description.xml" in text and "MediaRenderer" in text:
        raise SystemExit(0)
raise SystemExit(1)
PY

echo "[5/7] Verifying pywiim is installed"
python - <<'PY' || fail "pywiim is not installed in .venv"
import pywiim
print("pywiim", getattr(pywiim, "__version__", "<unknown>"), getattr(pywiim, "__file__", "<unknown>"))
PY

echo "[6/7] Running pywiim client-level control adapter"
python tools/acceptance_pywiim.py --host "$HOST" --port "$API_PORT" --name "$NAME" --test-url "$TEST_URL" || fail "pywiim adapter failed"

echo "[7/7] Acceptance passed"
echo "Logs: $LOG $SERVER_LOG"
