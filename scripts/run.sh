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
data["enable_mdns"] = True
data["enable_ssdp"] = True
data["http_api_port"] = 80 if port80 else 8080
data["upnp_port"] = 80 if port80 else 8080
data["mirror_port_80"] = port80
data["source_experiment_variant"] = "tunein"
data["tunein_streams_value"] = str(data.get("tunein_streams_value") or "200")
data["enable_plex_service"] = False
data["enable_stream_service_sentinels"] = False

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

echo "Starting Open Room Renderer"
echo "Laptop IP: ${IP}"
echo "Config: ${tmp_config}"
echo "SSDP: enabled"
echo "mDNS: enabled"
if [[ "$port80" == "1" ]]; then
  echo "HTTP API: http://${IP}/httpapi.asp?command=getStatusEx"
  echo "Description: http://${IP}/description.xml"
else
  echo "HTTP API: http://${IP}:8080/httpapi.asp?command=getStatusEx"
  echo "Description: http://${IP}:8080/description.xml"
fi
echo "App-facing services: TuneIn-style playback only"
echo "Tip: force-stop/reopen the phone app if it cached an old failed device."

export WIIM_HOME_ACOUSTIC_CAPABILITY_MODE="${WIIM_HOME_ACOUSTIC_CAPABILITY_MODE:-peq}"
export WIIM_HOME_AUDIO_OUTPUT_CAPABILITY_MODE="${WIIM_HOME_AUDIO_OUTPUT_CAPABILITY_MODE:-fail}"

if [[ "$port80" == "1" ]]; then
  echo
  echo "About to use sudo for privileged port binding."
  echo "Why sudo is needed: Open Room Renderer defaults to HTTP port 80, and Linux normally restricts ports below 1024 to privileged processes."
  echo "Why port 80 is useful: some controller apps probe plain http://device-ip/ during discovery or setup."
  echo "What will run with sudo: only the renderer process, so it can bind port 80."
  echo "If you do not want sudo, run: APP_COMPAT_PORT80=0 ./scripts/run.sh"
  echo
  exec sudo -E .venv/bin/open-room-renderer serve --config "$tmp_config"
fi

exec .venv/bin/open-room-renderer serve --config "$tmp_config"
