"""Server-host-only player password reset utility."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from account_store import AccountError, reset_password


parser = argparse.ArgumentParser(description="Reset a ShazChat player password on this server host")
parser.add_argument("username", nargs="?", help="Registered player name")
args = parser.parse_args()
username = args.username or input("Player name: ").strip()
first = getpass.getpass("New password: ")
second = getpass.getpass("Confirm new password: ")
if first != second:
    raise SystemExit("Passwords did not match. Nothing changed.")
try:
    account = reset_password(username, first)
except AccountError as exc:
    raise SystemExit(f"Password was not reset: {exc}") from exc
print(f"Password reset for {account['name']}. They can sign in immediately with the new password.")
