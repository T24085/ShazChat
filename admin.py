#!/usr/bin/env python3
"""Local, server-owner-only ShazChat moderation console.

Run this only on the machine that hosts the ShazChat server. It never opens an
admin endpoint to players or the public internet.
"""

from __future__ import annotations

import argparse
import getpass
from datetime import datetime

from account_store import list_accounts, reset_password
from moderation_store import (
    ban_player,
    chat_logs,
    clear_chat_logs,
    moderation_state,
    mute_player,
    unban_player,
    unmute_player,
)


def _when(timestamp):
    return datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M") if timestamp else "—"


def show_players():
    state = moderation_state()
    print("PLAYER                            STATUS")
    print("-" * 58)
    for account in list_accounts():
        key = account["name"].casefold()
        if key in state["bans"]:
            status = "BANNED"
        elif key in state["mutes"]:
            expires = state["mutes"][key].get("expires_at")
            status = f"MUTED until {_when(expires)}" if expires else "MUTED permanently"
        else:
            status = "OK"
        print(f"{account['name']:<32}  {status}")


def show_actions(kind):
    records = moderation_state()[kind]
    if not records:
        print(f"No active {kind}.")
        return
    for record in records.values():
        expiry = record.get("expires_at")
        detail = f", expires {_when(expiry)}" if expiry else ""
        reason = f" — {record.get('reason')}" if record.get("reason") else ""
        print(f"{record['name']}{detail}{reason}")


def main():
    parser = argparse.ArgumentParser(description="Local ShazChat server moderation console")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("players", help="List registered players and moderation status")
    commands.add_parser("bans", help="List banned players")
    commands.add_parser("mutes", help="List muted players")
    for name in ("ban", "unban", "unmute", "reset-password"):
        action = commands.add_parser(name)
        action.add_argument("player")
        if name == "ban":
            action.add_argument("--reason", default="")
        if name == "reset-password":
            action.add_argument("--password", help="Optional; omit to enter it privately")
    mute = commands.add_parser("mute")
    mute.add_argument("player")
    mute.add_argument("--minutes", type=int, default=0, help="0 means permanent")
    mute.add_argument("--reason", default="")
    logs = commands.add_parser("logs", help="Show the last 50 retained chat messages")
    logs.add_argument("--limit", type=int, default=50)
    commands.add_parser("clear-logs", help="Permanently clear the compact chat log")
    args = parser.parse_args()

    if args.command == "players":
        show_players()
    elif args.command in ("bans", "mutes"):
        show_actions(args.command)
    elif args.command == "ban":
        print(f"Banned {ban_player(args.player, args.reason)['name']}.")
    elif args.command == "unban":
        print("Ban removed." if unban_player(args.player) else "Player was not banned.")
    elif args.command == "mute":
        record = mute_player(args.player, args.minutes, args.reason)
        print(f"Muted {record['name']}." if record["expires_at"] else f"Muted {record['name']} permanently.")
    elif args.command == "unmute":
        print("Mute removed." if unmute_player(args.player) else "Player was not muted.")
    elif args.command == "reset-password":
        password = args.password or getpass.getpass("Temporary new password: ")
        print(f"Password reset for {reset_password(args.player, password)['name']}.")
    elif args.command == "logs":
        for entry in chat_logs(args.limit):
            target = f"Team {entry['room']}" if entry.get("scope") == "team" else "Global"
            print(f"[{entry.get('timestamp', '—')}] {target} {entry['name']}: {entry['text']}")
    elif args.command == "clear-logs":
        clear_chat_logs()
        print("Chat log cleared.")


if __name__ == "__main__":
    main()
