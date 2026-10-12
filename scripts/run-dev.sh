#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -x .venv/bin/python ]]; then
  echo "Missing .venv. Run ./scripts/dev-setup.sh first." >&2
  exit 2
fi
exec .venv/bin/python scripts/run-profile.py portable
