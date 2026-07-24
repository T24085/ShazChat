#!/usr/bin/env bash
# Linux source launcher for ShazChat.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
python3 -m pip install -r requirements-client.txt
exec python3 main.py "$@"
