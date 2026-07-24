#!/usr/bin/env python3
"""Room-scoped WebSocket server for ShazChat."""

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import time
from collections import deque

import websockets

from account_store import (
    AccountError,
    authenticate_account,
    change_password,
    create_account,
    delete_account,
    recover_password,
)
from moderation import contains_blocked_term, load_blocked_terms

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LOCKED_ROLES = ["Capper 1", "Capper 2"]
TEAM_ROLES = LOCKED_ROLES + ["Offense", "Defense"]
MAX_ROOMS = 10
ROOM_CAPACITY = 7
MAX_TIMER_SECONDS = 120
MAX_PASSWORD_ATTEMPTS = 5
PASSWORD_WINDOW_SECONDS = 60
MAX_CHAT_LENGTH = 300
CHAT_HISTORY_LIMIT = 50
CHAT_RATE_LIMIT = 6
CHAT_RATE_WINDOW_SECONDS = 10
MAX_WEBSOCKET_MESSAGE_BYTES = 16_384
SEND_TIMEOUT_SECONDS = 3

clients = set()
rooms = {room_id: set() for room_id in range(1, MAX_ROOMS + 1)}
client_room = {}
client_profiles = {}
role_claims = {room_id: {} for room_id in range(1, MAX_ROOMS + 1)}
room_passwords = {}
room_owners = {}
password_attempts = {}
account_attempts = {}
chat_attempts = {}
global_chat_history = deque(maxlen=CHAT_HISTORY_LIMIT)
room_chat_history = {room_id: deque(maxlen=CHAT_HISTORY_LIMIT) for room_id in rooms}
BLOCKED_TERMS = load_blocked_terms()


def _clean_name(value):
    name = str(value or "").strip()
    return name[:32] or "Player"


def _password_record(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1).hex()
    return salt.hex(), digest


def _password_matches(record, password):
    if not record:
        return True
    try:
        salt_hex, expected = record
        actual = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1).hex()
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def _attempt_key(websocket):
    address = websocket.remote_address
    return str(address[0] if isinstance(address, tuple) else address)


def _password_retry_after(websocket):
    key = _attempt_key(websocket)
    now = time.monotonic()
    attempts = [stamp for stamp in password_attempts.get(key, []) if now - stamp < PASSWORD_WINDOW_SECONDS]
    password_attempts[key] = attempts
    if len(attempts) < MAX_PASSWORD_ATTEMPTS:
        return 0
    return max(1, int(PASSWORD_WINDOW_SECONDS - (now - attempts[0])))


def _record_failed_password(websocket):
    key = _attempt_key(websocket)
    password_attempts.setdefault(key, []).append(time.monotonic())


def _clear_password_attempts(websocket):
    password_attempts.pop(_attempt_key(websocket), None)


def _account_retry_after(websocket):
    key = _attempt_key(websocket)
    now = time.monotonic()
    attempts = [stamp for stamp in account_attempts.get(key, []) if now - stamp < PASSWORD_WINDOW_SECONDS]
    account_attempts[key] = attempts
    if len(attempts) < MAX_PASSWORD_ATTEMPTS:
        return 0
    return max(1, int(PASSWORD_WINDOW_SECONDS - (now - attempts[0])))


def _record_failed_account(websocket):
    account_attempts.setdefault(_attempt_key(websocket), []).append(time.monotonic())


def _clear_account_attempts(websocket):
    account_attempts.pop(_attempt_key(websocket), None)


def _chat_allowed(websocket):
    now = time.monotonic()
    attempts = [stamp for stamp in chat_attempts.get(websocket, []) if now - stamp < CHAT_RATE_WINDOW_SECONDS]
    attempts.append(now)
    chat_attempts[websocket] = attempts
    return len(attempts) <= CHAT_RATE_LIMIT


def _roles_payload(room):
    return {
        role: (role_claims[room].get(role) or {}).get("id")
        for role in LOCKED_ROLES
    }


def _roster_payload(room):
    role_by_ws = {
        claim["ws"]: role for role, claim in role_claims[room].items()
    }
    members = []
    for ws in rooms[room]:
        profile = client_profiles.get(ws, {})
        members.append(
            {
                "id": profile.get("id"),
                "name": profile.get("name", "Player"),
                "role": role_by_ws.get(ws, profile.get("role", "Player")),
            }
        )
    return sorted(members, key=lambda member: (member["role"], member["name"].lower()))


def _team_directory_payload():
    directory = []
    for room in rooms:
        first_player = room_owners.get(room) or next(iter(rooms[room]), None)
        owner_name = _clean_name((client_profiles.get(first_player, {}) or {}).get("name")) if first_player else ""
        directory.append({"room": room, "count": len(rooms[room]), "first_name": owner_name})
    return directory


async def _send_with_timeout(websocket, message):
    try:
        await asyncio.wait_for(websocket.send(message), timeout=SEND_TIMEOUT_SECONDS)
        return None
    except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
        return websocket


async def _broadcast(room, payload):
    message = json.dumps(payload)
    recipients = list(rooms[room])
    results = await asyncio.gather(*(_send_with_timeout(ws, message) for ws in recipients), return_exceptions=True)
    disconnected = {result for result in results if result in recipients}
    for ws in disconnected:
        await _remove_from_room(ws, client_room.get(ws))


async def _broadcast_global(payload):
    message = json.dumps(payload)
    recipients = list(client_room)
    results = await asyncio.gather(*(_send_with_timeout(ws, message) for ws in recipients), return_exceptions=True)
    disconnected = {result for result in results if result in recipients}
    for ws in disconnected:
        await _remove_from_room(ws, client_room.get(ws))


async def _broadcast_team_directory():
    await _broadcast_global({"cmd": "team_directory", "teams": _team_directory_payload()})


def _chat_payload(scope, websocket, text, room=None):
    profile = client_profiles.get(websocket, {})
    return {
        "cmd": "chat_message",
        "scope": scope,
        "room": room,
        "name": _clean_name(profile.get("name")),
        "text": text,
        "timestamp": time.strftime("%H:%M", time.localtime()),
    }


async def _broadcast_room_state(room):
    if room not in rooms:
        return
    await _broadcast(
        room,
        {
            "cmd": "room_state",
            "room": room,
            "members": _roster_payload(room),
            "count": len(rooms[room]),
            "capacity": ROOM_CAPACITY,
            "locked": room in room_passwords,
            "owner_id": (client_profiles.get(room_owners.get(room), {}) or {}).get("id"),
        },
    )


async def _broadcast_role_status(room):
    await _broadcast(room, {"cmd": "role_status", "roles": _roles_payload(room)})
    await _broadcast_room_state(room)


async def _remove_from_room(websocket, room=None):
    room = room or client_room.pop(websocket, None)
    if room not in rooms:
        return
    rooms[room].discard(websocket)
    client_room.pop(websocket, None)
    for role in list(role_claims[room]):
        if role_claims[room][role].get("ws") == websocket:
            role_claims[room].pop(role, None)
    if not rooms[room]:
        room_passwords.pop(room, None)
        room_owners.pop(room, None)
        room_chat_history[room].clear()
        logger.info("Room %s emptied; its password and team chat history were cleared", room)
    else:
        if room_owners.get(room) == websocket and room not in room_passwords:
            room_owners[room] = next(iter(rooms[room]))
        await _broadcast_role_status(room)
    await _broadcast_team_directory()


async def _handle_join(websocket, data):
    room = int(data.get("room", 0))
    if room not in rooms:
        await websocket.send(json.dumps({"cmd": "join_result", "ok": False, "reason": "invalid", "room": room}))
        return

    profile = client_profiles.setdefault(websocket, {})
    if not profile.get("authenticated"):
        await websocket.send(json.dumps({"cmd": "join_result", "ok": False, "reason": "auth_required", "room": room}))
        return
    requested_role = data.get("role")
    profile["role"] = requested_role if requested_role in ("Offense", "Defense") else profile.get("role", "Player")
    provided_password = str(data.get("password") or "")
    retry_after = _password_retry_after(websocket)
    if retry_after:
        await websocket.send(json.dumps({"cmd": "join_result", "ok": False, "reason": "throttled", "room": room, "retry_after": retry_after}))
        return
    if room in room_passwords and not _password_matches(room_passwords[room], provided_password):
        _record_failed_password(websocket)
        await websocket.send(
            json.dumps({"cmd": "join_result", "ok": False, "reason": "password_required", "room": room})
        )
        return
    _clear_password_attempts(websocket)

    current_room = client_room.get(websocket)
    if current_room == room:
        await websocket.send(
            json.dumps(
                {
                    "cmd": "join_result",
                    "ok": True,
                    "room": room,
                    "count": len(rooms[room]),
                    "capacity": ROOM_CAPACITY,
                    "locked": room in room_passwords,
                    "can_set_password": room_owners.get(room) == websocket and room not in room_passwords,
                }
            )
        )
        await websocket.send(json.dumps({"cmd": "team_directory", "teams": _team_directory_payload()}))
        await _broadcast_role_status(room)
        return
    if len(rooms[room]) >= ROOM_CAPACITY:
        await websocket.send(json.dumps({"cmd": "join_result", "ok": False, "reason": "full", "room": room, "capacity": ROOM_CAPACITY}))
        return

    if current_room:
        await _remove_from_room(websocket, current_room)
    was_empty = not rooms[room]
    rooms[room].add(websocket)
    client_room[websocket] = room
    if was_empty:
        room_owners[room] = websocket
    await websocket.send(
        json.dumps(
            {
                "cmd": "join_result",
                "ok": True,
                "room": room,
                "count": len(rooms[room]),
                "capacity": ROOM_CAPACITY,
                "locked": room in room_passwords,
                "can_set_password": room_owners.get(room) == websocket and room not in room_passwords,
            }
        )
    )
    await websocket.send(json.dumps({"cmd": "chat_history", "scope": "global", "messages": list(global_chat_history)}))
    await websocket.send(json.dumps({"cmd": "chat_history", "scope": "team", "room": room, "messages": list(room_chat_history[room])}))
    await _broadcast_team_directory()
    await _broadcast_role_status(room)


async def _handle_auth(websocket, data):
    retry_after = _account_retry_after(websocket)
    if retry_after:
        await websocket.send(json.dumps({"cmd": "auth_result", "ok": False, "reason": "throttled", "retry_after": retry_after}))
        return
    username = str(data.get("username") or "")
    password = str(data.get("password") or "")
    create = bool(data.get("create"))
    try:
        account = create_account(username, password, str(data.get("recovery_pin") or "")) if create else authenticate_account(username, password)
    except AccountError as exc:
        await websocket.send(json.dumps({"cmd": "auth_result", "ok": False, "reason": "invalid", "message": str(exc)}))
        return
    if not account:
        _record_failed_account(websocket)
        await websocket.send(json.dumps({"cmd": "auth_result", "ok": False, "reason": "invalid_credentials", "message": "Player name or password is incorrect."}))
        return
    _clear_account_attempts(websocket)
    profile = client_profiles.setdefault(websocket, {})
    profile.update(
        {
            "authenticated": True,
            "id": account["id"],
            "name": account["name"],
            "role": "Player",
        }
    )
    await websocket.send(json.dumps({"cmd": "auth_result", "ok": True, "id": account["id"], "name": account["name"]}))
    logger.info("Authenticated player: %s", account["name"])


async def _handle_password_recovery(websocket, data):
    retry_after = _account_retry_after(websocket)
    if retry_after:
        await websocket.send(json.dumps({"cmd": "account_reset_result", "ok": False, "reason": "throttled", "retry_after": retry_after}))
        return
    try:
        account = recover_password(
            str(data.get("username") or ""),
            str(data.get("recovery_pin") or ""),
            str(data.get("new_password") or ""),
        )
    except AccountError as exc:
        _record_failed_account(websocket)
        await websocket.send(json.dumps({"cmd": "account_reset_result", "ok": False, "message": str(exc)}))
        return
    _clear_account_attempts(websocket)
    await websocket.send(json.dumps({"cmd": "account_reset_result", "ok": True, "message": "Password reset. You can now sign in."}))
    await _end_other_account_sessions(account["id"], websocket)
    logger.info("Password recovered for player: %s", account["name"])


async def _end_other_account_sessions(account_id, current_websocket, command="account_session_ended"):
    """Sign out duplicate sessions after a sensitive account action."""
    sessions = [
        ws for ws, profile in client_profiles.items()
        if ws != current_websocket and profile.get("id") == account_id
    ]
    for session in sessions:
        try:
            await session.send(json.dumps({"cmd": command}))
        except Exception:
            pass
        try:
            await session.close(code=4001, reason="Account session ended")
        except Exception:
            pass


async def _handle_change_password(websocket, data):
    profile = client_profiles[websocket]
    retry_after = _account_retry_after(websocket)
    if retry_after:
        await websocket.send(json.dumps({"cmd": "account_password_result", "ok": False, "reason": "throttled", "retry_after": retry_after}))
        return
    try:
        account = change_password(
            profile.get("name", ""),
            str(data.get("current_password") or ""),
            str(data.get("new_password") or ""),
        )
    except AccountError as exc:
        _record_failed_account(websocket)
        await websocket.send(json.dumps({"cmd": "account_password_result", "ok": False, "message": str(exc)}))
        return
    _clear_account_attempts(websocket)
    await websocket.send(json.dumps({"cmd": "account_password_result", "ok": True, "message": "Password changed."}))
    await _end_other_account_sessions(account["id"], websocket)
    logger.info("Password changed for player: %s", account["name"])


async def _handle_delete_account(websocket, data):
    profile = client_profiles[websocket]
    retry_after = _account_retry_after(websocket)
    if retry_after:
        await websocket.send(json.dumps({"cmd": "account_delete_result", "ok": False, "reason": "throttled", "retry_after": retry_after}))
        return
    try:
        account = delete_account(profile.get("name", ""), str(data.get("password") or ""))
    except AccountError as exc:
        _record_failed_account(websocket)
        await websocket.send(json.dumps({"cmd": "account_delete_result", "ok": False, "message": str(exc)}))
        return
    _clear_account_attempts(websocket)
    await websocket.send(json.dumps({"cmd": "account_delete_result", "ok": True, "message": "Account deleted."}))
    await _end_other_account_sessions(account["id"], websocket, command="account_deleted")
    logger.info("Deleted player account: %s", account["name"])
    await websocket.close(code=4001, reason="Account deleted")


async def handle_client(websocket):
    clients.add(websocket)
    logger.info("Client connected: %s", websocket.remote_address)
    try:
        await websocket.send(json.dumps({"cmd": "connected", "clients": len(clients)}))
        async for message in websocket:
            try:
                data = json.loads(message)
                cmd = data.get("cmd")
                profile = client_profiles.setdefault(websocket, {})
                if cmd == "auth":
                    await _handle_auth(websocket, data)
                elif cmd == "account_reset_password":
                    await _handle_password_recovery(websocket, data)
                elif not profile.get("authenticated"):
                    await websocket.send(json.dumps({"cmd": "auth_required"}))
                elif cmd == "account_change_password":
                    await _handle_change_password(websocket, data)
                elif cmd == "account_delete":
                    await _handle_delete_account(websocket, data)
                elif cmd == "join":
                    await _handle_join(websocket, data)
                elif cmd == "start":
                    room = client_room.get(websocket)
                    try:
                        capper = int(data.get("capper"))
                        seconds = float(data.get("seconds"))
                    except (TypeError, ValueError):
                        capper, seconds = 0, 0
                    role = f"Capper {capper}"
                    owner = role_claims.get(room, {}).get(role) if room else None
                    if not room or capper not in (1, 2) or not math.isfinite(seconds) or not 0 < seconds <= MAX_TIMER_SECONDS or not owner or owner.get("ws") != websocket:
                        await websocket.send(json.dumps({"cmd": "timer_rejected"}))
                        continue
                    await _broadcast(room, {"cmd": "start", "seconds": seconds, "sender": client_profiles[websocket].get("id"), "capper": capper})
                elif cmd == "role_claim":
                    room = client_room.get(websocket)
                    role = data.get("role")
                    if room and role in LOCKED_ROLES:
                        owner = role_claims[room].get(role)
                        ok = owner is None or owner.get("ws") == websocket
                        if ok:
                            role_claims[room][role] = {"id": client_profiles[websocket].get("id"), "ws": websocket}
                            client_profiles[websocket]["role"] = role
                            await _broadcast_role_status(room)
                        await websocket.send(json.dumps({"cmd": "role_result", "role": role, "ok": ok}))
                elif cmd == "role_release":
                    room = client_room.get(websocket)
                    role = data.get("role")
                    if room and role in LOCKED_ROLES and (role_claims[room].get(role) or {}).get("ws") == websocket:
                        role_claims[room].pop(role, None)
                        client_profiles[websocket]["role"] = "Player"
                        await _broadcast_role_status(room)
                elif cmd == "role_update":
                    room = client_room.get(websocket)
                    role = data.get("role")
                    if room and role in ("Offense", "Defense"):
                        client_profiles[websocket]["role"] = role
                        await _broadcast_room_state(room)
                elif cmd == "set_room_password":
                    room = client_room.get(websocket)
                    password = str(data.get("password") or "").strip()
                    ok = bool(room and room_owners.get(room) == websocket and room not in room_passwords)
                    if ok and password:
                        room_passwords[room] = _password_record(password)
                    await websocket.send(json.dumps({"cmd": "room_password_result", "ok": ok, "locked": bool(room in room_passwords) if room else False}))
                    if room:
                        await _broadcast_room_state(room)
                elif cmd == "chat_send":
                    room = client_room.get(websocket)
                    scope = data.get("scope")
                    text = str(data.get("text") or "").strip()[:MAX_CHAT_LENGTH]
                    if not room or scope not in ("global", "team") or not text:
                        continue
                    if not _chat_allowed(websocket):
                        await websocket.send(json.dumps({"cmd": "chat_rejected", "reason": "rate_limited"}))
                        continue
                    if contains_blocked_term(text, BLOCKED_TERMS):
                        logger.info("Blocked chat message from an authenticated player")
                        await websocket.send(json.dumps({"cmd": "chat_rejected", "reason": "blocked_content"}))
                        continue
                    payload = _chat_payload(scope, websocket, text, room if scope == "team" else None)
                    if scope == "global":
                        global_chat_history.append(payload)
                        await _broadcast_global(payload)
                    else:
                        room_chat_history[room].append(payload)
                        await _broadcast(room, payload)
            except (ValueError, TypeError, json.JSONDecodeError):
                logger.warning("Invalid client message")
            except Exception:
                logger.exception("Error handling client message")
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        clients.discard(websocket)
        await _remove_from_room(websocket)
        client_profiles.pop(websocket, None)
        chat_attempts.pop(websocket, None)
        account_attempts.pop(_attempt_key(websocket), None)
        logger.info("Active clients: %s", len(clients))


async def main():
    port = int(os.environ.get("PORT", 8765))
    host = os.environ.get("HOST", "0.0.0.0")
    logger.info("Starting ShazChat WebSocket server on %s:%s", host, port)
    async with websockets.serve(
        handle_client,
        host,
        port,
        ping_interval=20,
        ping_timeout=10,
        close_timeout=10,
        max_size=MAX_WEBSOCKET_MESSAGE_BYTES,
        max_queue=32,
    ):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
