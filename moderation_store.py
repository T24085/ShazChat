"""Persistent, server-owner-only moderation state for ShazChat."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from account_store import AccountError, normalize_name

DATA_DIR = Path(os.environ.get("CAPPERTIMER_DATA_DIR", Path(__file__).resolve().parent / "server-data"))
MODERATION_FILE = DATA_DIR / "moderation.json"
CHAT_LOG_FILE = DATA_DIR / "chat-log.json"
CHAT_LOG_LIMIT = 50


def _load(path: Path, default: dict) -> dict:
    try:
        with path.open("r", encoding="utf-8") as source:
            payload = json.load(source)
        return payload if isinstance(payload, dict) else default.copy()
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default.copy()


def _save(path: Path, payload: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f"{path.stem}-", suffix=".json", dir=DATA_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as destination:
            json.dump(payload, destination, indent=2, sort_keys=True)
            destination.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _state() -> dict:
    payload = _load(MODERATION_FILE, {"version": 1, "bans": {}, "mutes": {}})
    payload.setdefault("version", 1)
    payload.setdefault("bans", {})
    payload.setdefault("mutes", {})
    return payload


def _key(player_name: str) -> tuple[str, str]:
    display_name = normalize_name(player_name)
    return display_name.casefold(), display_name


def ban_player(player_name: str, reason: str = "") -> dict:
    key, display_name = _key(player_name)
    state = _state()
    state["bans"][key] = {"name": display_name, "reason": str(reason or "")[:200], "created_at": int(time.time())}
    state["mutes"].pop(key, None)
    _save(MODERATION_FILE, state)
    return state["bans"][key]


def unban_player(player_name: str) -> bool:
    key, _ = _key(player_name)
    state = _state()
    removed = state["bans"].pop(key, None) is not None
    _save(MODERATION_FILE, state)
    return removed


def mute_player(player_name: str, minutes: int = 0, reason: str = "") -> dict:
    key, display_name = _key(player_name)
    duration = max(0, int(minutes or 0))
    state = _state()
    state["mutes"][key] = {
        "name": display_name,
        "reason": str(reason or "")[:200],
        "created_at": int(time.time()),
        "expires_at": int(time.time()) + duration * 60 if duration else None,
    }
    _save(MODERATION_FILE, state)
    return state["mutes"][key]


def unmute_player(player_name: str) -> bool:
    key, _ = _key(player_name)
    state = _state()
    removed = state["mutes"].pop(key, None) is not None
    _save(MODERATION_FILE, state)
    return removed


def ban_status(player_name: str) -> dict | None:
    key, _ = _key(player_name)
    return _state()["bans"].get(key)


def mute_status(player_name: str) -> dict | None:
    key, _ = _key(player_name)
    state = _state()
    record = state["mutes"].get(key)
    if record and record.get("expires_at") and int(record["expires_at"]) <= int(time.time()):
        state["mutes"].pop(key, None)
        _save(MODERATION_FILE, state)
        return None
    return record


def moderation_state() -> dict:
    """Return a copy suitable for the server-owner console only."""
    state = _state()
    for key in list(state["mutes"]):
        mute_status(state["mutes"][key].get("name", key))
    return _state()


def append_chat_log(payload: dict) -> None:
    """Persist a compact owner-only rolling audit log of the last 50 messages."""
    log = _load(CHAT_LOG_FILE, {"version": 1, "messages": []})
    messages = log.setdefault("messages", [])
    messages.append(
        {
            "scope": payload.get("scope"),
            "room": payload.get("room"),
            "name": str(payload.get("name") or "Player")[:32],
            "text": str(payload.get("text") or "")[:300],
            "timestamp": payload.get("timestamp"),
            "logged_at": int(time.time()),
        }
    )
    log["messages"] = messages[-CHAT_LOG_LIMIT:]
    log["version"] = 1
    _save(CHAT_LOG_FILE, log)


def chat_logs(limit: int = CHAT_LOG_LIMIT) -> list[dict]:
    messages = _load(CHAT_LOG_FILE, {"version": 1, "messages": []}).get("messages", [])
    return list(messages[-max(1, min(int(limit), CHAT_LOG_LIMIT)):])


def clear_chat_logs() -> None:
    _save(CHAT_LOG_FILE, {"version": 1, "messages": []})
