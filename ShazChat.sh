#!/usr/bin/env bash
# Linux source launcher for ShazChat. It uses a project-local virtual
# environment so distro-managed Python installations are left untouched.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
VENV_DIR="${SHAZCHAT_VENV:-$SCRIPT_DIR/.venv}"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -r requirements-client.txt
exec "$VENV_DIR/bin/python" main.py "$@"
