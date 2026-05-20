#!/usr/bin/env bash
set -euo pipefail

HOST="${HOST:-127.0.0.1}"
API_PORT="${API_PORT:-80}"
UPNP_PORT="${UPNP_PORT:-80}"
TEST_URL="${TEST_URL:-https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3}"
BASE="http://${HOST}:${API_PORT}/httpapi.asp"

curl -fsS "${BASE}?command=getStatusEx" | jq .
curl -fsS "${BASE}?command=getPlayerStatus" | jq .
curl -fsS "http://${HOST}:${UPNP_PORT}/description.xml" >/dev/null
curl -fsS --get "${BASE}" --data-urlencode "command=setPlayerCmd:play:${TEST_URL}"
sleep 1
curl -fsS "${BASE}?command=setPlayerCmd:pause"
curl -fsS "${BASE}?command=setPlayerCmd:resume"
curl -fsS "${BASE}?command=setPlayerCmd:stop"
echo
echo "curl diagnostics completed; this is not the acceptance gate."
