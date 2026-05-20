#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
echo "This release no longer installs distro packages automatically."
echo "Use your distro's package manager to install the dependencies, then run:"
echo "  ./scripts/dev-setup.sh"
echo
exec ./scripts/dev-setup.sh
