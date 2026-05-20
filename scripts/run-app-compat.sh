#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
if [[ ! -d .venv ]]; then
  echo "Missing .venv. Run ./scripts/dev-setup.sh first." >&2
  exit 2
fi

. .venv/bin/activate

mkdir -p .state/app-compat-tmp
tmp_config=".state/app-compat-tmp/config.yaml"
base_config="configs/config.app-compat.yaml"

fresh="${APP_COMPAT_FRESH:-1}"
port80="${APP_COMPAT_PORT80:-1}"
name="${APP_COMPAT_NAME:-Open Room Renderer}"

python - "$base_config" "$tmp_config" "$fresh" "$port80" "$name" <<'PY'
from pathlib import Path
import sys
import uuid
import yaml

base = Path(sys.argv[1])
out = Path(sys.argv[2])
fresh = sys.argv[3] == "1"
port80 = sys.argv[4] == "1"
name = sys.argv[5]

data = yaml.safe_load(base.read_text()) or {}
data["friendly_name"] = name
data["mdns_instance_name"] = name
data["mirror_port_80"] = port80
if not port80:
    data["http_api_port"] = 8080
    data["upnp_port"] = 8080

if fresh:
    value = "FF31" + uuid.uuid4().hex.upper()
    data["discovery_uuid"] = value
    data["uuid"] = value
    data["state_dir"] = f".state/app-compat-{value[-8:]}"

out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(yaml.safe_dump(data, sort_keys=False))
print(out)
PY

IP="$(ip route get 8.8.8.8 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')"
IP="${IP:-127.0.0.1}"

echo "Starting Open Room Renderer app-compat mode"
echo "Laptop IP: ${IP}"
echo "Config: ${tmp_config}"
echo "mDNS: enabled"
echo "SSDP: enabled"
echo "HTTP API: http://${IP}/httpapi.asp?command=getStatusEx"
echo "Description: http://${IP}/description.xml"
if [[ "$port80" == "1" ]]; then
  echo "Port 80 mirror: enabled"
  echo "Why port 80 can help: some controller apps still probe plain http://device-ip/ during discovery or setup."
  echo "Port 80 is now the normal app-compat endpoint."
else
  echo "Port 80 mirror: disabled"
  echo "APP_COMPAT_PORT80=0 selected an unprivileged fallback on port 8080."
fi
echo
echo "Tip: force-stop/reopen the phone app if it cached an old failed device."

export WIIM_HOME_ACOUSTIC_CAPABILITY_MODE="${WIIM_HOME_ACOUSTIC_CAPABILITY_MODE:-peq}"
export WIIM_HOME_AUDIO_OUTPUT_CAPABILITY_MODE="${WIIM_HOME_AUDIO_OUTPUT_CAPABILITY_MODE:-fail}"

if [[ "$port80" == "1" ]]; then
  echo
  echo "About to use sudo for privileged port binding."
  echo "Why sudo is needed: Linux normally restricts ports below 1024, including HTTP port 80, to privileged processes."
  echo "Why port 80 is useful here: some controller apps still probe plain http://device-ip/ during discovery or setup, even when SSDP/mDNS advertises another port."
  echo "What will run with sudo: only the renderer process, so it can bind the local HTTP API on port 80 for compatibility testing."
  echo "If you do not want any sudo usage, run: APP_COMPAT_PORT80=0 ./scripts/run.sh"
  echo
  exec sudo -E .venv/bin/open-room-renderer serve --config "$tmp_config"
fi

exec .venv/bin/open-room-renderer serve --config "$tmp_config"
