#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Preserve explicitly selected older app-compat invocations during migration.
if [[ -n "${APP_COMPAT_PORT80+x}${APP_COMPAT_FRESH+x}${APP_COMPAT_NAME+x}" ]]; then
  echo "Explicit APP_COMPAT_* option detected; using the separate app-compat runner."
  exec ./scripts/run-app-compat.sh
fi
exec ./scripts/run-dev.sh
