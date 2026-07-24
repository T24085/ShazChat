"""Persistent, local account storage for the ShazChat server."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path

from moderation import contains_blocked_term

ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
RECOVERY_PIN_RE = re.compile(r"^\d{4,12}$")
DATA_DIR = Path(os.environ.get("CAPPERTIMER_DATA_DIR", Path(__file__).resolve().parent / "server-data"))
ACCOUNTS_FILE = DATA_DIR / "accounts.json"


class AccountError(ValueError):
    pass


def normalize_name(username: str) -> str:
    name = str(username or "").strip()
    if not ACCOUNT_NAME_RE.fullmatch(name):
        raise AccountError("Use 3–32 letters, numbers, dots, dashes, or underscores for your player name.")
    if contains_blocked_term(name):
        raise AccountError("That player name is not allowed.")
    return name


def _validate_password(password: str) -> str:
    password = str(password or "")
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise AccountError(f"Password must be {MIN_PASSWORD_LENGTH}–{MAX_PASSWORD_LENGTH} characters.")
    return password


def _validate_recovery_pin(pin: str) -> str:
    pin = str(pin or "")
    if not RECOVERY_PIN_RE.fullmatch(pin):
        raise AccountError("Recovery PIN must be 4–12 digits.")
    return pin


def _record_password(password: str, validate_password: bool = True) -> dict[str, str]:
    password = _validate_password(password) if validate_password else str(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1).hex()
    return {"salt": salt.hex(), "digest": digest}


def _matches(record: dict[str, str], password: str) -> bool:
    try:
        salt = bytes.fromhex(record["salt"])
        expected = record["digest"]
        actual = hashlib.scrypt(str(password or "").encode("utf-8"), salt=salt, n=2**14, r=8, p=1).hex()
        return hmac.compare_digest(actual, expected)
    except (KeyError, TypeError, ValueError):
        return False


def _load() -> dict:
    try:
        with ACCOUNTS_FILE.open("r", encoding="utf-8") as source:
            payload = json.load(source)
        if isinstance(payload, dict) and isinstance(payload.get("accounts"), dict):
            return payload
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as exc:
        raise AccountError("The server account file cannot be read. Contact the server owner.") from exc
    if ACCOUNTS_FILE.exists():
        raise AccountError("The server account file has an invalid format. Contact the server owner.")
    return {"version": 1, "accounts": {}}


def _save(payload: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="accounts-", suffix=".json", dir=DATA_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as destination:
            json.dump(payload, destination, indent=2, sort_keys=True)
            destination.write("\n")
        os.replace(temporary, ACCOUNTS_FILE)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def create_account(username: str, password: str, recovery_pin: str) -> dict[str, str]:
    display_name = normalize_name(username)
    key = display_name.casefold()
    payload = _load()
    if key in payload["accounts"]:
        raise AccountError("That player name is already registered. Sign in instead.")
    payload["accounts"][key] = {
        "username": display_name,
        "password": _record_password(password),
        "recovery_pin": _record_password(_validate_recovery_pin(recovery_pin), validate_password=False),
        "created_at": int(time.time()),
    }
    _save(payload)
    return {"id": f"account:{key}", "name": display_name}


def authenticate_account(username: str, password: str) -> dict[str, str] | None:
    display_name = normalize_name(username)
    account = _load()["accounts"].get(display_name.casefold())
    if not account or not _matches(account.get("password", {}), password):
        return None
    return {"id": f"account:{display_name.casefold()}", "name": account["username"]}


def reset_password(username: str, password: str) -> dict[str, str]:
    display_name = normalize_name(username)
    key = display_name.casefold()
    payload = _load()
    account = payload["accounts"].get(key)
    if not account:
        raise AccountError("No account exists with that player name.")
    account["password"] = _record_password(password)
    account["password_reset_at"] = int(time.time())
    _save(payload)
    return {"id": f"account:{key}", "name": account["username"]}


def recover_password(username: str, recovery_pin: str, new_password: str) -> dict[str, str]:
    """Reset a password by matching the account's separately stored recovery PIN."""
    display_name = normalize_name(username)
    key = display_name.casefold()
    payload = _load()
    account = payload["accounts"].get(key)
    if not account or not _matches(account.get("recovery_pin", {}), _validate_recovery_pin(recovery_pin)):
        raise AccountError("Player name or recovery PIN is incorrect.")
    account["password"] = _record_password(new_password)
    account["password_recovered_at"] = int(time.time())
    _save(payload)
    return {"id": f"account:{key}", "name": account["username"]}


def change_password(username: str, current_password: str, new_password: str) -> dict[str, str]:
    """Change a player's password after proving possession of the current one."""
    display_name = normalize_name(username)
    key = display_name.casefold()
    payload = _load()
    account = payload["accounts"].get(key)
    if not account or not _matches(account.get("password", {}), current_password):
        raise AccountError("Current password is incorrect.")
    account["password"] = _record_password(new_password)
    account["password_changed_at"] = int(time.time())
    _save(payload)
    return {"id": f"account:{key}", "name": account["username"]}


def delete_account(username: str, password: str) -> dict[str, str]:
    """Permanently remove an account after password confirmation."""
    display_name = normalize_name(username)
    key = display_name.casefold()
    payload = _load()
    account = payload["accounts"].get(key)
    if not account or not _matches(account.get("password", {}), password):
        raise AccountError("Password is incorrect.")
    deleted = {"id": f"account:{key}", "name": account["username"]}
    del payload["accounts"][key]
    _save(payload)
    return deleted


def list_accounts() -> list[dict[str, str | int]]:
    """Return only non-sensitive account metadata for the server-owner console."""
    accounts = _load()["accounts"]
    return sorted(
        (
            {"id": f"account:{key}", "name": account.get("username", key), "created_at": account.get("created_at", 0)}
            for key, account in accounts.items()
        ),
        key=lambda account: str(account["name"]).casefold(),
    )
