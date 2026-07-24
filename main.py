#!/usr/bin/env python3
# main.py
# ShazChat: team-scoped overlay countdown timer with WebSocket sync
# Cross-platform PyQt6 client (Windows registered hotkeys; Linux X11/XWayland hotkeys)
#
# Usage:
#   python main.py        # runs with network sync enabled
#   python main.py --no-network  # run local-only
#
# Requirements (pip):
#   pip install PyQt6 pywin32

import sys
import time
import json
import uuid
import threading
import argparse
import os
import ctypes
import subprocess
import shutil
import html
from ctypes import wintypes
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional

from update_service import UpdateError, download_verified_installer, fetch_update, is_newer
from updater_config import APP_VERSION

# Suppress Qt warnings/errors to console
os.environ['QT_LOGGING_RULES'] = '*.debug=false'

from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtCore import pyqtSignal

try:
    import websockets
    import asyncio
except ImportError:
    websockets = None
    asyncio = None

# Linux global hotkeys. pynput uses an X11/XWayland listener; Wayland-only
# desktops intentionally restrict global keyboard capture.
try:
    from pynput import keyboard as pynput_keyboard
    from pynput import mouse as pynput_mouse
except ImportError:
    pynput_keyboard = None
    pynput_mouse = None

# For click-through window on Windows:
try:
    import win32con
    import win32gui
    import win32api
except Exception:
    win32gui = None
    win32con = None
    win32api = None

# Config
HOTKEY_1 = "v"             # key to press for capper 1
HOTKEY_2 = "b"             # key to press for capper 2
CHAT_HOTKEY = "enter"      # key to open the chat composer during gameplay
OVERLAY_TOGGLE_HOTKEY = "f10"  # pause/resume gameplay overlays and global bindings
TIMER_OPTIONS_1 = [35, 25, 20]  # cycle order as requested
TIMER_OPTIONS_2 = [35, 25, 20]
CAP_COLORS = ["#00FF00", "#7A3DF0"]
DEFAULT_ROLE = "Capper 1"
LOCKED_ROLES = ["Capper 1", "Capper 2"]
TEAM_ROLES = LOCKED_ROLES + ["Offense", "Defense"]
MAX_ROOMS = 10
ROOM_CAPACITY = 7
DEFAULT_ROOM = 1
# Keep the gameplay overlay compact: two readable timer cards without
# taking over the top of the screen.
WINDOW_WIDTH = 520
WINDOW_HEIGHT = 160
TIMER_WIDTH = 520
DEFAULT_SERVER_URL = os.environ.get(
    "CAPTIMER_SERVER",
    "wss://capper.novatec.casa",
)
_APPDATA_ROOT = os.environ.get("APPDATA") or os.path.expanduser("~")
_LEGACY_PRESET_DIR = os.path.join(_APPDATA_ROOT, "CapperTimer")
PRESET_DIR = os.path.join(_APPDATA_ROOT, "ShazChat")
PRESET_FILE = os.path.join(PRESET_DIR, "capper-presets.json")
DIAGNOSTIC_LOG = os.path.join(PRESET_DIR, "capper-times.log")
UPDATE_CACHE_DIR = os.path.join(PRESET_DIR, "updates")
MAP_PRESETS = [
    "Custom",
    "DX",
    "Hollow",
    "Raindance",
    "Wavemist",
    "Torment",
    "Katabatic",
    "Dry Dock",
]

MY_ID = str(uuid.uuid4())


class UiDispatcher(QtCore.QObject):
    """Safely deliver network callbacks to the Qt UI thread."""

    callback = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.callback.connect(self._run)

    def _run(self, fn):
        fn()


class NativeHotkeyFilter(QtCore.QAbstractNativeEventFilter):
    """Receive Windows registered-hotkey messages without a low-level hook."""

    WM_HOTKEY = 0x0312

    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def nativeEventFilter(self, event_type, message):
        if event_type not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            return False, 0
        try:
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == self.WM_HOTKEY:
                self._callback(int(msg.wParam))
                return True, 0
        except Exception:
            logging.getLogger("capper_times").exception("Unable to process Windows hotkey message")
        return False, 0


class NativeHotkeyManager:
    """Global hotkeys via Windows RegisterHotKey or a Linux X11 listener."""

    MOD_NOREPEAT = 0x4000

    def __init__(self, qt_app, callback):
        self._registered = {}
        self._callback = callback
        self._qt_app = qt_app
        self._filter = None
        self._linux_listener = None
        self._linux_mouse_listener = None
        self._linux_down = set()
        self._poll_only = set()
        if sys.platform.startswith("win"):
            self._filter = NativeHotkeyFilter(callback)
            self._qt_app.installNativeEventFilter(self._filter)

    @staticmethod
    def _virtual_key(key):
        key = str(key or "").strip().upper()
        named_keys = {
            "ENTER": 0x0D,
            "RETURN": 0x0D,
            "SPACE": 0x20,
            "TAB": 0x09,
            "NUMPAD_ADD": 0x6B,
            "NUMPAD_SUBTRACT": 0x6D,
            "NUMPAD_MULTIPLY": 0x6A,
            "NUMPAD_DIVIDE": 0x6F,
            "NUMPAD_DECIMAL": 0x6E,
            "MOUSE_LEFT": 0x01,
            "MOUSE_RIGHT": 0x02,
            "MOUSE_MIDDLE": 0x04,
            "MOUSE4": 0x05,
            "MOUSE5": 0x06,
        }
        if key in named_keys:
            return named_keys[key]
        if key.startswith("NUMPAD") and key[6:].isdigit():
            number = int(key[6:])
            if 0 <= number <= 9:
                return 0x60 + number
        if len(key) == 1 and ("A" <= key <= "Z" or "0" <= key <= "9"):
            return ord(key)
        if key.startswith("F") and key[1:].isdigit():
            number = int(key[1:])
            if 1 <= number <= 24:
                return 0x70 + number - 1
        return None

    @staticmethod
    def _is_mouse_hotkey(key):
        return str(key or "").strip().lower() in {
            "mouse_left", "mouse_right", "mouse_middle", "mouse4", "mouse5"
        }

    def register(self, hotkey_id, key):
        self.unregister(hotkey_id)
        if sys.platform.startswith("win"):
            virtual_key = self._virtual_key(key)
            if virtual_key is None:
                return False, "Use a key, numpad key, mouse button, or F1–F24 key."
            if self._is_mouse_hotkey(key):
                # RegisterHotKey does not accept mouse buttons. The existing
                # physical-state polling path handles them reliably in games.
                self._registered[hotkey_id] = key
                self._poll_only.add(hotkey_id)
                return True, None
            if not ctypes.windll.user32.RegisterHotKey(None, hotkey_id, self.MOD_NOREPEAT, virtual_key):
                return False, f"'{key}' is already in use by Windows or another app."
            self._registered[hotkey_id] = key
            return True, None
        if sys.platform.startswith("linux"):
            is_keyboard = self._linux_key(key) is not None
            is_mouse = self._linux_mouse_button(key) is not None
            if not is_keyboard and not is_mouse:
                return False, "Use a key, numpad key, mouse button, or F1–F24 key."
            if (is_keyboard and pynput_keyboard is None) or (is_mouse and pynput_mouse is None):
                return False, "Linux hotkeys need pynput. Run: python3 -m pip install pynput"
            self._registered[hotkey_id] = key
            try:
                self._restart_linux_listener()
            except Exception as exc:
                self._registered.pop(hotkey_id, None)
                return False, f"Linux global hotkeys need an X11 or XWayland session ({exc})."
            return True, None
        return False, "Global hotkeys are currently supported on Windows and Linux."

    @staticmethod
    def _linux_key(key):
        normalized = str(key or "").strip().lower()
        if len(normalized) == 1 and (normalized.isalnum() or normalized == " "):
            return normalized
        named_keys = {
            "enter": "enter",
            "return": "enter",
            "space": "space",
            "tab": "tab",
        }
        if normalized in named_keys:
            return named_keys[normalized]
        if normalized.startswith("f") and normalized[1:].isdigit() and 1 <= int(normalized[1:]) <= 24:
            return normalized
        if normalized.startswith("numpad") and normalized[6:].isdigit() and 0 <= int(normalized[6:]) <= 9:
            return normalized
        if normalized in {"numpad_decimal", "numpad_add", "numpad_subtract", "numpad_multiply", "numpad_divide"}:
            return normalized
        return None

    @staticmethod
    def _linux_mouse_button(key):
        return {
            "mouse_left": "left",
            "mouse_right": "right",
            "mouse_middle": "middle",
            "mouse4": "x1",
            "mouse5": "x2",
        }.get(str(key or "").strip().lower())

    def _matches_linux_key(self, pressed, configured):
        expected = self._linux_key(configured)
        if expected is None:
            return False
        if expected.startswith("numpad"):
            keypad_vks = {
                "numpad_decimal": 65454,
                "numpad_add": 65451,
                "numpad_subtract": 65453,
                "numpad_multiply": 65450,
                "numpad_divide": 65455,
            }
            expected_vk = keypad_vks.get(expected)
            if expected_vk is None:
                expected_vk = 65456 + int(expected[6:])
            return getattr(pressed, "vk", None) == expected_vk
        if isinstance(pressed, pynput_keyboard.KeyCode):
            return pressed.char is not None and pressed.char.lower() == expected
        return getattr(pressed, "name", "").lower() == expected

    def _on_linux_press(self, pressed):
        for hotkey_id, configured in tuple(self._registered.items()):
            if self._matches_linux_key(pressed, configured) and hotkey_id not in self._linux_down:
                self._linux_down.add(hotkey_id)
                self._callback(hotkey_id)

    def _on_linux_release(self, released):
        for hotkey_id, configured in tuple(self._registered.items()):
            if self._matches_linux_key(released, configured):
                self._linux_down.discard(hotkey_id)

    def _on_linux_click(self, _x, _y, button, pressed):
        button_name = getattr(button, "name", "").lower()
        for hotkey_id, configured in tuple(self._registered.items()):
            if self._linux_mouse_button(configured) != button_name:
                continue
            if pressed and hotkey_id not in self._linux_down:
                self._linux_down.add(hotkey_id)
                self._callback(hotkey_id)
            elif not pressed:
                self._linux_down.discard(hotkey_id)

    def _stop_linux_listener(self):
        listener, self._linux_listener = self._linux_listener, None
        mouse_listener, self._linux_mouse_listener = self._linux_mouse_listener, None
        self._linux_down.clear()
        if listener is not None:
            listener.stop()
        if mouse_listener is not None:
            mouse_listener.stop()

    def _restart_linux_listener(self):
        self._stop_linux_listener()
        if not self._registered:
            return
        if any(self._linux_key(key) is not None for key in self._registered.values()):
            self._linux_listener = pynput_keyboard.Listener(
                on_press=self._on_linux_press,
                on_release=self._on_linux_release,
            )
            self._linux_listener.start()
            self._linux_listener.wait()
        if any(self._linux_mouse_button(key) is not None for key in self._registered.values()):
            self._linux_mouse_listener = pynput_mouse.Listener(on_click=self._on_linux_click)
            self._linux_mouse_listener.start()
            self._linux_mouse_listener.wait()

    def unregister(self, hotkey_id):
        if hotkey_id in self._registered and sys.platform.startswith("win") and hotkey_id not in self._poll_only:
            ctypes.windll.user32.UnregisterHotKey(None, hotkey_id)
        self._registered.pop(hotkey_id, None)
        self._poll_only.discard(hotkey_id)
        if sys.platform.startswith("linux"):
            self._restart_linux_listener()

    def close(self):
        for hotkey_id in list(self._registered):
            self.unregister(hotkey_id)
        self._stop_linux_listener()
        if self._filter is not None:
            self._qt_app.removeNativeEventFilter(self._filter)


class HotkeyCaptureEdit(QtWidgets.QLineEdit):
    """A read-only field that records the next keyboard or mouse input."""

    _MOUSE_BUTTONS = {
        QtCore.Qt.MouseButton.LeftButton: "mouse_left",
        QtCore.Qt.MouseButton.RightButton: "mouse_right",
        QtCore.Qt.MouseButton.MiddleButton: "mouse_middle",
        QtCore.Qt.MouseButton.BackButton: "mouse4",
        QtCore.Qt.MouseButton.ForwardButton: "mouse5",
    }

    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setPlaceholderText("Click, then press a key or mouse button")
        self.setToolTip("Click this field, then press the key, numpad key, or mouse button you want to use.")

    def _capture(self, value):
        self.setText(value)
        self.selectAll()

    def keyPressEvent(self, event):
        key = event.key()
        modifiers = event.modifiers()
        is_keypad = bool(modifiers & QtCore.Qt.KeyboardModifier.KeypadModifier)
        value = None
        if is_keypad:
            digit_keys = {
                QtCore.Qt.Key.Key_0: "numpad0", QtCore.Qt.Key.Key_1: "numpad1",
                QtCore.Qt.Key.Key_2: "numpad2", QtCore.Qt.Key.Key_3: "numpad3",
                QtCore.Qt.Key.Key_4: "numpad4", QtCore.Qt.Key.Key_5: "numpad5",
                QtCore.Qt.Key.Key_6: "numpad6", QtCore.Qt.Key.Key_7: "numpad7",
                QtCore.Qt.Key.Key_8: "numpad8", QtCore.Qt.Key.Key_9: "numpad9",
                QtCore.Qt.Key.Key_Period: "numpad_decimal",
                QtCore.Qt.Key.Key_Plus: "numpad_add",
                QtCore.Qt.Key.Key_Minus: "numpad_subtract",
                QtCore.Qt.Key.Key_Asterisk: "numpad_multiply",
                QtCore.Qt.Key.Key_Slash: "numpad_divide",
            }
            value = digit_keys.get(key)
        if value is None:
            named_keys = {
                QtCore.Qt.Key.Key_Return: "enter",
                QtCore.Qt.Key.Key_Enter: "enter",
                QtCore.Qt.Key.Key_Space: "space",
                QtCore.Qt.Key.Key_Tab: "tab",
            }
            value = named_keys.get(key)
        if value is None and QtCore.Qt.Key.Key_F1 <= key <= QtCore.Qt.Key.Key_F24:
            value = f"f{int(key - QtCore.Qt.Key.Key_F1) + 1}"
        if value is None:
            text = event.text().strip().lower()
            if len(text) == 1 and text.isalnum():
                value = text
        if value:
            self._capture(value)
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        # The click used to focus the field should not itself become the bind.
        was_focused = self.hasFocus()
        super().mousePressEvent(event)
        value = self._MOUSE_BUTTONS.get(event.button()) if was_focused else None
        if value:
            self._capture(value)
            event.accept()


def configure_diagnostics():
    # Preserve existing player presets and update data during the ShazChat
    # rebrand without keeping the old product name in the active folder.
    if not os.path.exists(PRESET_DIR) and os.path.isdir(_LEGACY_PRESET_DIR):
        try:
            shutil.copytree(_LEGACY_PRESET_DIR, PRESET_DIR)
        except OSError:
            pass
    os.makedirs(PRESET_DIR, exist_ok=True)
    logger = logging.getLogger("capper_times")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(DIAGNOSTIC_LOG, maxBytes=512_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.info("ShazChat started")
    return logger


class WebSocketClient:
    """WebSocket client for connecting to remote server"""
    def __init__(self, server_url, app_instance):
        self.server_url = server_url
        self.app = app_instance
        self.websocket = None
        self.running = False
        self.should_run = True
        self.loop = None

    async def run(self):
        """Maintain a reconnecting WebSocket connection without blocking Qt."""
        delay = 1
        while self.should_run:
            try:
                self.app.dispatch_ui(lambda: self.app.set_connection_state("connecting"))
                self.websocket = await websockets.connect(
                    self.server_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=10,
                )
                self.running = True
                delay = 1
                self.app.logger.info("WebSocket connected: %s", self.server_url)
                self.app.dispatch_ui(lambda: self.app.set_connection_state("connected"))
                self.app.dispatch_ui(lambda: self.app.update_status("WebSocket: connected"))
                self.app.dispatch_ui(self.app.on_ws_connected)
                await self._listen()
            except Exception as exc:
                self.app.logger.warning("WebSocket connection failed: %s", exc)
            finally:
                self.running = False
                self.websocket = None
            if self.should_run:
                self.app.dispatch_ui(lambda: self.app.set_connection_state("reconnecting"))
                self.app.dispatch_ui(lambda: self.app.update_status(f"WebSocket: reconnecting in {delay}s"))
                await asyncio.sleep(delay)
                delay = min(delay * 2, 15)

    async def _listen(self):
        """Listen for messages from server"""
        try:
            async for message in self.websocket:
                try:
                    data = json.loads(message)
                    cmd = data.get("cmd")
                    if cmd == "auth_result":
                        self.app.dispatch_ui(lambda data=data: self.app.handle_auth_result(data))
                    elif cmd == "auth_required":
                        self.app.dispatch_ui(self.app.handle_auth_required)
                    elif cmd == "account_password_result":
                        self.app.dispatch_ui(lambda data=data: self.app.handle_account_password_result(data))
                    elif cmd == "account_delete_result":
                        self.app.dispatch_ui(lambda data=data: self.app.handle_account_delete_result(data))
                    elif cmd == "account_reset_result":
                        self.app.dispatch_ui(lambda data=data: self.app.handle_account_reset_result(data))
                    elif cmd in ("account_session_ended", "account_deleted"):
                        self.app.dispatch_ui(
                            lambda cmd=cmd: self.app.handle_account_session_ended(cmd)
                        )
                    elif cmd == "start" and "seconds" in data:
                        # Ignore our own messages
                        if data.get("sender") == self.app.player_id:
                            continue
                        capper = int(data.get("capper", 1))
                        index = capper - 1
                        if index not in (0, 1):
                            continue
                        sec = float(data["seconds"])
                        print(f"Received timer start from remote (capper {capper}): {sec}s")
                        # Update timer in Qt thread using signal (thread-safe)
                        # Capture sec in lambda to avoid closure issues
                        self.app.window.start_timer_signal.emit(index, float(sec))
                    elif cmd == "role_status":
                        roles = data.get("roles", {})
                        if isinstance(roles, dict):
                            self.app.dispatch_ui(lambda roles=roles: self.app.handle_role_status(roles))
                    elif cmd == "role_result":
                        role = data.get("role")
                        ok = bool(data.get("ok"))
                        self.app.dispatch_ui(lambda role=role, ok=ok: self.app.handle_role_result(role, ok))
                    elif cmd == "join_result":
                        room = data.get("room")
                        ok = bool(data.get("ok"))
                        reason = data.get("reason")
                        count = data.get("count")
                        capacity = data.get("capacity")
                        can_set_password = bool(data.get("can_set_password"))
                        locked = bool(data.get("locked"))
                        self.app.dispatch_ui(lambda room=room, ok=ok, reason=reason, count=count, capacity=capacity, can_set_password=can_set_password, locked=locked: self.app.handle_join_result(room, ok, reason, count, capacity, can_set_password, locked))
                    elif cmd == "room_state":
                        members = data.get("members", [])
                        room = data.get("room")
                        locked = bool(data.get("locked"))
                        owner_id = data.get("owner_id")
                        if isinstance(members, list):
                            self.app.dispatch_ui(lambda room=room, members=members, locked=locked, owner_id=owner_id: self.app.handle_room_state(room, members, locked, owner_id))
                    elif cmd == "room_password_result":
                        ok = bool(data.get("ok"))
                        locked = bool(data.get("locked"))
                        self.app.dispatch_ui(lambda ok=ok, locked=locked: self.app.handle_room_password_result(ok, locked))
                    elif cmd == "timer_rejected":
                        self.app.dispatch_ui(lambda: self.app.update_status("Timer was rejected: claim the matching Capper role first."))
                    elif cmd == "chat_rejected":
                        message = (
                            "That message is not allowed."
                            if data.get("reason") == "blocked_content"
                            else "Chat is moving too fast. Please wait a few seconds."
                        )
                        self.app.dispatch_ui(lambda message=message: self.app.update_status(message))
                    elif cmd == "chat_history":
                        scope = data.get("scope")
                        messages = data.get("messages", [])
                        if scope in ("global", "team") and isinstance(messages, list):
                            self.app.dispatch_ui(lambda scope=scope, messages=messages: self.app.handle_chat_history(scope, messages))
                    elif cmd == "chat_message":
                        scope = data.get("scope")
                        if scope in ("global", "team"):
                            self.app.dispatch_ui(lambda scope=scope, data=data: self.app.handle_chat_message(scope, data))
                    elif cmd == "team_directory":
                        teams = data.get("teams", [])
                        if isinstance(teams, list):
                            self.app.dispatch_ui(lambda teams=teams: self.app.handle_team_directory(teams))
                except Exception as e:
                    print(f"Error processing WebSocket message: {e}")
                    continue
        except websockets.exceptions.ConnectionClosed:
            self.app.logger.info("WebSocket disconnected")
        except Exception as e:
            self.app.logger.warning("WebSocket listener error: %s", e)

    async def send_timer(self, seconds, sender_id, capper):
        """Send timer start to server"""
        if self.websocket and self.running:
            try:
                msg = json.dumps(
                    {"cmd": "start", "seconds": seconds, "sender": sender_id, "capper": capper}
                )
                await self.websocket.send(msg)
            except Exception as e:
                print(f"Failed to send: {e}")

    async def send_auth(self, username, password, create=False, recovery_pin=""):
        if self.websocket and self.running:
            try:
                await self.websocket.send(json.dumps({"cmd": "auth", "username": username, "password": password, "create": bool(create), "recovery_pin": recovery_pin}))
            except Exception as e:
                print(f"Failed to authenticate: {e}")

    async def send_password_reset(self, username, recovery_pin, new_password):
        if self.websocket and self.running:
            try:
                await self.websocket.send(
                    json.dumps(
                        {
                            "cmd": "account_reset_password",
                            "username": username,
                            "recovery_pin": recovery_pin,
                            "new_password": new_password,
                        }
                    )
                )
            except Exception as e:
                print(f"Failed to reset password: {e}")

    async def send_change_password(self, current_password, new_password):
        if self.websocket and self.running:
            try:
                await self.websocket.send(
                    json.dumps(
                        {
                            "cmd": "account_change_password",
                            "current_password": current_password,
                            "new_password": new_password,
                        }
                    )
                )
            except Exception as e:
                print(f"Failed to change password: {e}")

    async def send_delete_account(self, password):
        if self.websocket and self.running:
            try:
                await self.websocket.send(json.dumps({"cmd": "account_delete", "password": password}))
            except Exception as e:
                print(f"Failed to delete account: {e}")

    async def send_role_claim(self, role, sender_id):
        if self.websocket and self.running:
            try:
                msg = json.dumps({"cmd": "role_claim", "role": role, "sender": sender_id})
                await self.websocket.send(msg)
            except Exception as e:
                print(f"Failed to send: {e}")

    async def send_role_release(self, role, sender_id):
        if self.websocket and self.running:
            try:
                msg = json.dumps({"cmd": "role_release", "role": role, "sender": sender_id})
                await self.websocket.send(msg)
            except Exception as e:
                print(f"Failed to send: {e}")

    async def send_role_update(self, role):
        if self.websocket and self.running:
            try:
                await self.websocket.send(json.dumps({"cmd": "role_update", "role": role}))
            except Exception as e:
                print(f"Failed to update role: {e}")

    async def send_join(self, room, sender_id, name, role, password=""):
        if self.websocket and self.running:
            try:
                msg = json.dumps({"cmd": "join", "room": room, "sender": sender_id, "name": name, "role": role, "password": password})
                await self.websocket.send(msg)
            except Exception as e:
                print(f"Failed to send join: {e}")

    async def send_room_password(self, password):
        if self.websocket and self.running:
            try:
                await self.websocket.send(json.dumps({"cmd": "set_room_password", "password": password}))
            except Exception as e:
                print(f"Failed to set room password: {e}")

    async def send_chat(self, scope, text):
        if self.websocket and self.running:
            try:
                await self.websocket.send(json.dumps({"cmd": "chat_send", "scope": scope, "text": text}))
            except Exception as e:
                print(f"Failed to send chat: {e}")

    def close(self):
        """Close connection"""
        self.should_run = False
        self.running = False
        self.app.dispatch_ui(lambda: self.app.set_connection_state("offline"))
        if self.websocket and self.loop:
            asyncio.run_coroutine_threadsafe(self.websocket.close(), self.loop)


class OverlayLabel(QtWidgets.QWidget):
    def __init__(self, lines=2, parent=None):
        super().__init__(parent)
        self._texts = [""] * lines
        self._colors = CAP_COLORS[:lines]
        self._name_font = QtGui.QFont("Segoe UI", 12, QtGui.QFont.Weight.DemiBold)
        self._timer_font = QtGui.QFont("Segoe UI", 40, QtGui.QFont.Weight.Bold)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)

    def set_text(self, index: int, text: str, color: Optional[str] = None):
        if 0 <= index < len(self._texts):
            if color is not None:
                self._colors[index] = color
            self._texts[index] = text
            self.update()

    def texts(self):
        return list(self._texts)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        # Clear previous frame fully on a translucent surface
        painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), QtCore.Qt.GlobalColor.transparent)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceOver)

        line_count = max(len(self._texts), 1)
        col_width = int(self.rect().width() / line_count)
        for i, text in enumerate(self._texts):
            card = QtCore.QRect(i * col_width + 4, 4, col_width - 8, self.rect().height() - 8)
            accent = QtGui.QColor(self._colors[i])
            painter.setBrush(QtGui.QColor(14, 20, 28, 185))
            painter.setPen(QtGui.QPen(QtGui.QColor(accent.red(), accent.green(), accent.blue(), 225), 2))
            painter.drawRoundedRect(card, 10, 10)
            painter.fillRect(QtCore.QRect(card.x(), card.y(), 5, card.height()), accent)

            name, separator, timer_text = str(text).partition("\n")
            if not separator:
                timer_text = name
                name = f"Capper {i + 1}"
            name_rect = card.adjusted(14, 8, -12, -card.height() + 34)
            timer_rect = card.adjusted(12, 28, -10, -8)
            painter.setFont(self._name_font)
            painter.setPen(QtGui.QColor("#D7E5F4"))
            painter.drawText(
                name_rect,
                QtCore.Qt.AlignmentFlag.AlignHCenter | QtCore.Qt.AlignmentFlag.AlignVCenter,
                QtGui.QFontMetrics(self._name_font).elidedText(
                    name, QtCore.Qt.TextElideMode.ElideRight, name_rect.width()
                ),
            )
            painter.setFont(self._timer_font)
            painter.setPen(accent)
            painter.drawText(
                timer_rect,
                QtCore.Qt.AlignmentFlag.AlignCenter,
                timer_text or "READY",
            )
        painter.end()


class OverlayWindow(QtWidgets.QMainWindow):
    # Signal to start timer from any thread
    start_timer_signal = QtCore.pyqtSignal(int, float)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ShazChat Overlay")
        # Use simpler window flags first to ensure visibility
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
        )
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        # Ensure transparent window background (required in PyQt6)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setStyleSheet("background-color: transparent;")

        # Connect signal to start method
        self.start_timer_signal.connect(self.start_timer)
        container = QtWidgets.QWidget(self)
        layout = QtWidgets.QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # central display widget
        self.label = OverlayLabel(lines=2, parent=container)
        self.label.setMinimumSize(TIMER_WIDTH, WINDOW_HEIGHT)
        self.label.setMaximumSize(TIMER_WIDTH, WINDOW_HEIGHT)
        self.label.resize(TIMER_WIDTH, WINDOW_HEIGHT)

        layout.addWidget(self.label)

        self.setCentralWidget(container)
        # Do not assign a visible fallback position. The owner explicitly places
        # this overlay on the selected monitor before it is ever shown.
        self._positioned = False
        self._click_through_pending = False
        # Set initial text
        self._capper_names = ["Capper 1", "Capper 2"]
        self._ready_texts = [f"{name}\nREADY" for name in self._capper_names]
        self.label.set_text(0, self._ready_texts[0])
        self.label.set_text(1, self._ready_texts[1])
        self.label.show()

        # background opacity widget to improve visibility
        self.bg = None

        # timer
        self._remaining = [0.0, 0.0]
        self._qtimer = QtCore.QTimer()
        self._qtimer.setInterval(50)  # 20 Hz
        self._qtimer.timeout.connect(self._tick)

        # Flash timer for red warning
        self._flash_timer = [QtCore.QTimer(), QtCore.QTimer()]
        self._flash_state = [False, False]
        for i, timer in enumerate(self._flash_timer):
            timer.setInterval(250)  # Flash every 250ms
            timer.timeout.connect(lambda idx=i: self._flash_tick(idx))

    def _set_label_text(self, index: int, text: str, color: Optional[str] = None):
        self.label.set_text(index, text, color=color)

    def _display_text(self, index: int, timer_text: str):
        return f"{self._capper_names[index]}\n{timer_text}"

    def set_capper_names(self, names):
        for index in range(2):
            name = str(names[index] if index < len(names) else "").strip()
            self._capper_names[index] = name or f"Capper {index + 1}"
            if self._remaining[index] <= 0:
                self._set_label_text(index, self._display_text(index, "READY"), color=CAP_COLORS[index])

    def place_on_screen(self, x, y, width, height):
        """Place before showing so startup cannot expose a top-left fallback."""
        self.setGeometry(int(x), int(y), int(width), int(height))
        self._positioned = True

    def enable_click_through_after_show(self):
        """Only initialize Windows click-through after the final geometry exists."""
        if self._click_through_pending or not self._positioned:
            return
        if sys.platform.startswith("win") and win32gui:
            self._click_through_pending = True
            QtCore.QTimer.singleShot(200, self._make_click_through)

    def _make_click_through(self):
        """Make window click-through after ensuring it's rendered"""
        try:
            # Never show the overlay from this helper: doing so before monitor
            # placement was what could leave it anchored at (0, 0).
            if not self.isVisible() or not self._positioned:
                self._click_through_pending = False
                return

            # Don't use WA_TranslucentBackground - it conflicts with SetLayeredWindowAttributes
            # Instead, use Windows API directly for transparency

            # Wait a bit for window to be fully rendered
            QtCore.QTimer.singleShot(200, lambda: self._setup_layered_window())
        except Exception as e:
            print(f"Warning: Could not setup click-through: {e}")

    def _setup_layered_window(self):
        """Setup layered window attributes after window is rendered"""
        try:
            hwnd = int(self.winId())
            if hwnd == 0:
                # Window not ready yet, try again
                QtCore.QTimer.singleShot(100, lambda: self._setup_layered_window())
                return

            # Enable translucent background FIRST (before Windows API calls)
            self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)

            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)

            # Enable layered window style
            ex_style |= win32con.WS_EX_LAYERED
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style)

            # Set window to be fully opaque (255 = fully opaque)
            # Don't call SetLayeredWindowAttributes if WA_TranslucentBackground is set
            # This avoids UpdateLayeredWindowIndirect errors

            # Wait a bit to ensure rendering is complete, then make click-through
            QtCore.QTimer.singleShot(500, lambda: self._enable_click_through(hwnd))
        except Exception as e:
            print(f"Warning: Could not setup layered window: {e}")

    def _enable_click_through(self, hwnd):
        """Enable click-through after window is fully rendered"""
        try:
            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            ex_style |= win32con.WS_EX_TRANSPARENT | win32con.WS_EX_NOACTIVATE
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style)
            print("Click-through enabled - window is now transparent to mouse clicks")
        except Exception as e:
            print(f"Warning: Could not enable click-through: {e}")

    def start_timer(self, index: int, seconds: float):
        if not 0 <= index < len(self._remaining):
            return
        print(f"start_timer({index}) called with {seconds} seconds")
        # Stop any existing flash for this timer
        self._flash_timer[index].stop()
        self._flash_state[index] = False

        self._remaining[index] = float(seconds)
        # Never activate the overlay: starting a timer must leave focus in the game.
        if not self.isVisible():
            self.show()
            self.enable_click_through_after_show()
        self.raise_()
        # Clear "READY" text immediately and force update
        self.label.set_text(index, self._display_text(index, ""))
        QtWidgets.QApplication.processEvents()  # Force clear to happen
        print(f"Cleared READY, updating label with remaining={self._remaining[index]}")
        # Update label with timer value
        self._update_label(index)
        QtWidgets.QApplication.processEvents()  # Force timer display
        print(f"Starting timer, label texts={self.label.texts()}")
        if not self._qtimer.isActive():
            self._qtimer.start()
        print(f"Timer started successfully with {self._remaining[index]}s remaining")

    def stop(self, index: int):
        if not 0 <= index < len(self._remaining):
            return
        self._flash_timer[index].stop()
        self._flash_state[index] = False
        self._remaining[index] = 0.0
        self._set_label_text(index, self._display_text(index, "READY"), color=CAP_COLORS[index])
        if all(rem <= 0 for rem in self._remaining):
            self._qtimer.stop()

    def _tick(self):
        any_active = False
        for i, remaining in enumerate(self._remaining):
            if remaining <= 0:
                continue
            any_active = True
            self._remaining[i] -= 0.05
            if self._remaining[i] <= 0:
                self.stop(i)
                continue
            self._update_label(i)
        if not any_active:
            self._qtimer.stop()

    def _flash_tick(self, index: int):
        """Flash the label when <= 10 seconds"""
        if self._remaining[index] <= 10 and self._remaining[index] > 0:
            self._flash_state[index] = not self._flash_state[index]
            sec = int(self._remaining[index] + 0.999)
            text = self._display_text(index, f"{sec:02d}s")

            # Alternate between bright red and dimmed red
            color = "#FF0000" if self._flash_state[index] else "#CC0000"
            self._set_label_text(index, text, color=color)

    def _update_label(self, index: int):
        sec = int(self._remaining[index] + 0.999)  # ceil-ish display
        text = self._display_text(index, f"{sec:02d}s")

        # Determine color
        if self._remaining[index] <= 10:
            if not self._flash_timer[index].isActive():
                self._flash_timer[index].start()
            # Color handled by flash timer; update text only.
            self._set_label_text(index, text)
        else:
            if self._flash_timer[index].isActive():
                self._flash_timer[index].stop()
            self._flash_state[index] = False
            self._set_label_text(index, text, color=CAP_COLORS[index])


class ChatWindow(QtWidgets.QWidget):
    """Always-on-top companion chat, placed in the lower-left beside gameplay."""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.scope = "team"
        self.messages = {"global": [], "team": []}
        self.unread = {"global": False, "team": False}
        self._alert_active = False
        self._font_size = 12
        self._text_color = "#F8FBFF"
        self.setWindowTitle("ShazChat")
        self.setWindowFlags(
            QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
            | QtCore.Qt.WindowType.WindowTitleHint
            | QtCore.Qt.WindowType.WindowSystemMenuHint
            | QtCore.Qt.WindowType.WindowMinimizeButtonHint
            | QtCore.Qt.WindowType.WindowCloseButtonHint
        )
        # Keep the compact default, but let one-monitor players make the panel
        # as large or small as their setup needs.
        self.setMinimumSize(300, 230)
        self.resize(360, 300)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._alert_timer = QtCore.QTimer(self)
        self._alert_timer.setSingleShot(True)
        self._alert_timer.timeout.connect(self._clear_alert)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(7)
        tabs = QtWidgets.QHBoxLayout()
        self.global_btn = QtWidgets.QPushButton("Global")
        self.team_btn = QtWidgets.QPushButton("My Team")
        for button in (self.global_btn, self.team_btn):
            button.setCheckable(True)
            tabs.addWidget(button)
        self.global_btn.clicked.connect(lambda: self.set_scope("global"))
        self.team_btn.clicked.connect(lambda: self.set_scope("team"))
        layout.addLayout(tabs)

        self.title = QtWidgets.QLabel()
        self.title.setObjectName("chatTitle")
        layout.addWidget(self.title)
        self.feed = QtWidgets.QListWidget()
        self.feed.setWordWrap(True)
        self.feed.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        layout.addWidget(self.feed, 1)

        compose = QtWidgets.QHBoxLayout()
        self.input = QtWidgets.QLineEdit()
        self.input.setMaxLength(300)
        self.input.setPlaceholderText("Message your team…")
        self.input.returnPressed.connect(self._send)
        self.input.installEventFilter(self)
        self.send_btn = QtWidgets.QPushButton("Send")
        self.send_btn.clicked.connect(self._send)
        compose.addWidget(self.input, 1)
        compose.addWidget(self.send_btn)
        layout.addLayout(compose)
        self._apply_visual_state()
        self.set_scope("team")

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        panel_alpha = 210 if self._alert_active else 145
        painter.setBrush(QtGui.QColor(16, 21, 28, panel_alpha))
        painter.setPen(QtGui.QPen(QtGui.QColor(66, 210, 177, 220 if self._alert_active else 90), 1))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 10, 10)

    def _apply_visual_state(self):
        feed_alpha = 245 if self._alert_active else 180
        border = "#42D2B1" if self._alert_active else "#344253"
        self.setStyleSheet(
            "QWidget { background: transparent; color: #F7FAFC; }"
            "QPushButton { background: rgba(46,107,255,235); border: 0; border-radius: 6px; padding: 6px 10px; font-weight: 700; }"
            "QPushButton:checked { background: rgba(0,168,120,240); }"
            "QPushButton:hover { background: rgba(63,124,255,245); }"
            f"QLineEdit, QListWidget {{ background: rgba(27,35,46,{feed_alpha}); border: 1px solid {border}; border-radius: 6px; padding: 7px; color: #F7FAFC; }}"
            "QLabel#chatTitle { color: #D7E5F4; font-weight: 700; }"
        )
        self.update()

    def _highlight_message(self):
        self._alert_active = True
        self._apply_visual_state()
        self._alert_timer.start(6500)

    def _clear_alert(self):
        self._alert_active = False
        self._apply_visual_state()

    def set_scope(self, scope):
        self.scope = scope
        self.unread[scope] = False
        self.global_btn.setChecked(scope == "global")
        self.team_btn.setChecked(scope == "team")
        self.global_btn.setText("Global" + (" •" if self.unread["global"] else ""))
        self.team_btn.setText("My Team" + (" •" if self.unread["team"] else ""))
        self.title.setText("Global chat" if scope == "global" else f"Team {self.app.room} chat")
        self.input.setPlaceholderText("Message everyone…" if scope == "global" else "Message your team…")
        self._render()

    def set_room(self, room):
        if self.scope == "team":
            self.title.setText(f"Team {room} chat")

    def set_history(self, scope, messages):
        self.messages[scope] = list(messages)[-50:]
        if scope == self.scope:
            self._render()

    def add_message(self, scope, message):
        self.messages[scope].append(message)
        self.messages[scope] = self.messages[scope][-50:]
        self._highlight_message()
        if scope == self.scope:
            self._render()
        else:
            self.unread[scope] = True
            self.global_btn.setText("Global" + (" •" if self.unread["global"] else ""))
            self.team_btn.setText("My Team" + (" •" if self.unread["team"] else ""))

    def _render(self):
        self.feed.clear()
        for message in self.messages[self.scope]:
            name = str(message.get("name") or "Player")
            text = str(message.get("text") or "")
            timestamp = str(message.get("timestamp") or "")
            channel_color = "#42D2B1" if self.scope == "team" else "#61A8FF"
            label = QtWidgets.QLabel(
                f'<span style="color:{channel_color}; font-weight:700;">{html.escape(timestamp)}</span> '
                f'<span style="color:#FFFFFF; font-weight:700;">{html.escape(name)}:</span> '
                f'<span style="color:{self._text_color};">{html.escape(text)}</span>'
            )
            label.setTextFormat(QtCore.Qt.TextFormat.RichText)
            label.setWordWrap(True)
            label.setStyleSheet(f"background: transparent; padding: 3px 4px; font-size: {self._font_size}px;")
            item = QtWidgets.QListWidgetItem()
            item.setSizeHint(QtCore.QSize(1, max(30, label.sizeHint().height() + 8)))
            self.feed.addItem(item)
            self.feed.setItemWidget(item, label)
        self.feed.scrollToBottom()

    @staticmethod
    def _valid_color(value, fallback):
        color = QtGui.QColor(str(value or ""))
        return color.name().upper() if color.isValid() else fallback

    def set_appearance(self, font_size, text_color):
        self._font_size = max(10, min(24, int(font_size)))
        self._text_color = self._valid_color(text_color, "#F8FBFF")
        self._render()

    def _send(self):
        text = self.input.text().strip()
        if not text:
            return
        self.app.send_chat(self.scope, text)
        self.input.clear()
        self.app.finish_chat_entry()

    def eventFilter(self, watched, event):
        if watched is self.input and event.type() in (
            QtCore.QEvent.Type.FocusIn,
            QtCore.QEvent.Type.FocusOut,
        ):
            # QApplication.focusChanged can briefly report no focused widget
            # while a Tool window is activated. Synchronize after Qt completes
            # the transition so registered global keys never steal typed text.
            QtCore.QTimer.singleShot(0, self.app._sync_text_entry_focus)
        if (
            watched is self.input
            and event.type() == QtCore.QEvent.Type.KeyPress
            and event.key() == QtCore.Qt.Key.Key_Escape
        ):
            self.input.clear()
            self.app.finish_chat_entry()
            return True
        return super().eventFilter(watched, event)


class ChatOverlayWindow(QtWidgets.QWidget):
    """A click-through, gameplay-safe display of the most recent chat messages."""

    MAX_MESSAGES = 3
    MIN_CARD_HEIGHT = 48
    CARD_VERTICAL_PADDING = 14
    CARD_GAP = 6

    def __init__(self):
        super().__init__()
        self._messages = []
        self._alert_active = False
        self._font_size = 12
        self._text_color = "#F8FBFF"
        self.setWindowTitle("ShazChat Overlay")
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(520, 180)
        self._screen = None
        self._chat_height = 0
        self._fade_timer = QtCore.QTimer(self)
        self._fade_timer.setSingleShot(True)
        self._fade_timer.timeout.connect(self._fade_to_idle)
        QtCore.QTimer.singleShot(300, self._make_click_through)

    def _make_click_through(self):
        if not (sys.platform.startswith("win") and win32gui and win32con):
            return
        try:
            hwnd = int(self.winId())
            if not hwnd:
                return
            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            ex_style |= win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT | win32con.WS_EX_NOACTIVATE
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style)
        except Exception as e:
            print(f"Warning: Could not make chat overlay click-through: {e}")

    def set_position(self, screen, chat_height):
        self._screen = screen
        self._chat_height = int(chat_height)
        x = screen.x() + 18
        y = screen.y() + screen.height() - int(chat_height) - self.height() - 34
        self.setGeometry(x, y, self.width(), self.height())

    def _message_text(self, scope, name, text):
        channel = "TEAM" if scope == "team" else "GLOBAL"
        return f"{channel}  {name}: {text}"

    def _message_markup(self, scope, name, text):
        channel = "TEAM" if scope == "team" else "GLOBAL"
        channel_color = "#42D2B1" if scope == "team" else "#61A8FF"
        return (
            f'<span style="color:{channel_color}; font-weight:700;">{channel}</span>  '
            f'<span style="color:#FFFFFF; font-weight:700;">{html.escape(name)}:</span> '
            f'<span style="color:{self._text_color};">{html.escape(text)}</span>'
        )

    def _message_document(self, scope, name, text):
        document = QtGui.QTextDocument()
        document.setDefaultFont(QtGui.QFont("Segoe UI", self._font_size, QtGui.QFont.Weight.DemiBold))
        document.setDocumentMargin(0)
        document.setTextWidth(self.width() - 42)
        document.setHtml(self._message_markup(scope, name, text))
        return document

    def _message_heights(self):
        heights = []
        for scope, name, text in self._messages:
            document = self._message_document(scope, name, text)
            heights.append(max(self.MIN_CARD_HEIGHT, int(document.size().height()) + self.CARD_VERTICAL_PADDING))
        return heights

    def _resize_for_messages(self):
        heights = self._message_heights()
        target_height = 12 + sum(heights) + self.CARD_GAP * max(0, len(heights) - 1)
        if target_height != self.height():
            self.setFixedHeight(target_height)
        if self._screen is not None:
            self.set_position(self._screen, self._chat_height)

    def add_message(self, scope, message):
        name = str(message.get("name") or "Player").strip()[:32]
        text = str(message.get("text") or "").strip()[:220]
        if not text:
            return
        self._messages.append((scope, name, text))
        self._messages = self._messages[-self.MAX_MESSAGES:]
        self._resize_for_messages()
        self._alert_active = True
        self.show()
        self.raise_()
        self._fade_timer.start(8000)
        self.update()

    def _fade_to_idle(self):
        self._alert_active = False
        self.update()

    def set_appearance(self, font_size, text_color):
        self._font_size = max(10, min(24, int(font_size)))
        color = QtGui.QColor(str(text_color or ""))
        self._text_color = color.name().upper() if color.isValid() else "#F8FBFF"
        self._resize_for_messages()
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), QtCore.Qt.GlobalColor.transparent)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceOver)
        if not self._messages:
            painter.end()
            return

        panel_alpha = 180 if self._alert_active else 70
        message_heights = self._message_heights()
        top = 6
        for index, (scope, name, text) in enumerate(self._messages):
            card_height = message_heights[index]
            rect = QtCore.QRect(6, top, self.width() - 12, card_height)
            accent = QtGui.QColor("#42D2B1" if scope == "team" else "#61A8FF")
            painter.setBrush(QtGui.QColor(14, 20, 28, panel_alpha))
            painter.setPen(QtGui.QPen(QtGui.QColor(accent.red(), accent.green(), accent.blue(), 210 if self._alert_active else 100), 1))
            painter.drawRoundedRect(rect, 8, 8)
            painter.fillRect(QtCore.QRect(rect.x(), rect.y(), 5, rect.height()), accent)
            document = self._message_document(scope, name, text)
            painter.save()
            painter.translate(rect.x() + 14, rect.y() + 6)
            document.drawContents(painter)
            painter.restore()
            top += card_height + self.CARD_GAP
        painter.end()


class ConnectionBadgeWindow(QtWidgets.QWidget):
    """Small click-through gameplay indicator for sync availability."""

    STATES = {
        "connected": ("CONNECTED", "#42D2B1"),
        "connecting": ("CONNECTING", "#61A8FF"),
        "reconnecting": ("RECONNECTING", "#F5A524"),
        "offline": ("OFFLINE", "#EF5B67"),
    }

    def __init__(self):
        super().__init__()
        self._state = "offline"
        self.setWindowTitle("ShazChat Connection")
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(138, 25)
        self._positioned = False
        QtCore.QTimer.singleShot(250, self._make_click_through)

    def _make_click_through(self):
        if not (sys.platform.startswith("win") and win32gui and win32con):
            return
        try:
            hwnd = int(self.winId())
            if hwnd:
                style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                win32gui.SetWindowLong(
                    hwnd,
                    win32con.GWL_EXSTYLE,
                    style | win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT | win32con.WS_EX_NOACTIVATE,
                )
        except Exception:
            pass

    def set_state(self, state):
        self._state = state if state in self.STATES else "offline"
        self.update()

    def set_position(self, timer_x, timer_y, timer_width, timer_height):
        self.move(int(timer_x + (timer_width - self.width()) / 2), int(timer_y + timer_height + 5))
        self._positioned = True

    def paintEvent(self, event):
        text, color = self.STATES[self._state]
        accent = QtGui.QColor(color)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setBrush(QtGui.QColor(14, 20, 28, 185))
        painter.setPen(QtGui.QPen(QtGui.QColor(accent.red(), accent.green(), accent.blue(), 220), 1))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 8, 8)
        painter.setBrush(accent)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.drawEllipse(10, 9, 7, 7)
        painter.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
        painter.setPen(QtGui.QColor("#F7FAFC"))
        painter.drawText(self.rect().adjusted(25, 0, -6, 0), QtCore.Qt.AlignmentFlag.AlignVCenter, text)
        painter.end()


class SettingsWindow(QtWidgets.QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWindowTitle(f"ShazChat Settings · v{APP_VERSION}")
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowType.WindowStaysOnTopHint)
        self.setMinimumSize(620, 500)
        self.resize(660, 590)
        self._suspend_auto_apply = False
        self._auto_apply_timer = QtCore.QTimer(self)
        self._auto_apply_timer.setSingleShot(True)
        self._auto_apply_timer.setInterval(350)
        self._auto_apply_timer.timeout.connect(self._apply_current_settings)

        root_layout = QtWidgets.QVBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)
        self.tabs = QtWidgets.QTabWidget()

        player_form = QtWidgets.QFormLayout()
        player_form.setLabelAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        player_form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        player_form.setHorizontalSpacing(14)
        player_form.setVerticalSpacing(9)
        timer_form = QtWidgets.QFormLayout()
        timer_form.setLabelAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        timer_form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        timer_form.setHorizontalSpacing(14)
        timer_form.setVerticalSpacing(9)

        self.name_input = QtWidgets.QLineEdit()
        self.name_input.setPlaceholderText("Your display name")
        self.name_input.setMaxLength(32)
        player_form.addRow("Your name", self.name_input)

        self.times_input_1 = QtWidgets.QLineEdit()
        self.times_input_1.setPlaceholderText("Capper 1 times (e.g., 35,25,20)")
        self.hotkey_input_1 = HotkeyCaptureEdit()

        self.times_input_2 = QtWidgets.QLineEdit()
        self.times_input_2.setPlaceholderText("Capper 2 times (e.g., 35,25,20)")
        self.hotkey_input_2 = HotkeyCaptureEdit()
        self.chat_hotkey_input = HotkeyCaptureEdit()
        self.overlay_hotkey_input = HotkeyCaptureEdit()

        timer_form.addRow("Capper 1 times", self.times_input_1)
        timer_form.addRow("Capper 1 hotkey", self.hotkey_input_1)
        timer_form.addRow("Capper 2 times", self.times_input_2)
        timer_form.addRow("Capper 2 hotkey", self.hotkey_input_2)
        timer_form.addRow("Open chat hotkey", self.chat_hotkey_input)
        timer_form.addRow("Toggle gameplay controls hotkey", self.overlay_hotkey_input)

        self.monitor_select = QtWidgets.QComboBox()
        self._refresh_monitors()
        player_form.addRow("Display monitor", self.monitor_select)

        self.map_select = QtWidgets.QComboBox()
        for name in MAP_PRESETS:
            self.map_select.addItem(name)
        timer_form.addRow("Map preset", self.map_select)

        self.room_select = QtWidgets.QComboBox()
        for room in range(1, MAX_ROOMS + 1):
            self.room_select.addItem(f"○ Team {room} · Empty", room)

        self.compatibility_mode = QtWidgets.QCheckBox("Turn off gameplay overlays and global hotkeys (compatibility mode)")
        self.compatibility_mode.setToolTip("Unchecked is the normal setting: timer and chat overlays start with the app. Check this only if a game becomes unstable.")
        player_form.addRow("Game safety", self.compatibility_mode)

        chat_form = QtWidgets.QFormLayout()
        chat_form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        chat_form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        chat_form.setHorizontalSpacing(14)
        chat_form.setVerticalSpacing(10)
        self.chat_font_size = QtWidgets.QSpinBox()
        self.chat_font_size.setRange(10, 24)
        self.chat_font_size.setValue(12)
        self.chat_font_size.setSuffix(" px")
        self.chat_text_color = "#F8FBFF"
        self.chat_text_color_btn = QtWidgets.QPushButton()
        self.chat_text_color_btn.clicked.connect(lambda: self._choose_chat_color("text"))
        self._update_chat_color_buttons()
        chat_form.addRow("Chat font size", self.chat_font_size)
        chat_form.addRow("Message color", self.chat_text_color_btn)
        chat_hint = QtWidgets.QLabel("Player names stay white. Team channel labels stay green so team callouts remain easy to spot.")
        chat_hint.setWordWrap(True)
        chat_form.addRow("", chat_hint)

        update_row = QtWidgets.QHBoxLayout()
        self.update_status = QtWidgets.QLabel(f"Version {APP_VERSION}")
        self.update_button = QtWidgets.QPushButton("Check for updates")
        self.update_button.clicked.connect(lambda: self.app.check_for_updates(manual=True))
        update_row.addWidget(self.update_status, 1)
        update_row.addWidget(self.update_button)
        player_form.addRow("App updates", update_row)

        for control in (
            self.name_input,
            self.times_input_1,
            self.hotkey_input_1,
            self.times_input_2,
            self.hotkey_input_2,
            self.chat_hotkey_input,
            self.overlay_hotkey_input,
            self.monitor_select,
            self.map_select,
            self.room_select,
        ):
            control.setMinimumHeight(36)

        preset_row = QtWidgets.QHBoxLayout()
        self.load_preset_btn = QtWidgets.QPushButton("Load")
        self.save_preset_btn = QtWidgets.QPushButton("Save")
        self.load_preset_btn.clicked.connect(self._on_load_preset)
        self.save_preset_btn.clicked.connect(self._on_save_preset)
        self.load_preset_btn.setMinimumHeight(36)
        self.save_preset_btn.setMinimumHeight(36)
        preset_row.addWidget(self.load_preset_btn)
        preset_row.addWidget(self.save_preset_btn)
        timer_form.addRow("Preset actions", preset_row)

        manual_row = QtWidgets.QHBoxLayout()
        start_capper_1 = QtWidgets.QPushButton("Start Capper 1")
        start_capper_2 = QtWidgets.QPushButton("Start Capper 2")
        start_capper_1.clicked.connect(lambda: self.app.trigger_timer(0))
        start_capper_2.clicked.connect(lambda: self.app.trigger_timer(1))
        manual_row.addWidget(start_capper_1)
        manual_row.addWidget(start_capper_2)
        timer_form.addRow("Manual start", manual_row)

        player_group = QtWidgets.QGroupBox("Player & Match")
        player_group.setLayout(player_form)
        timer_group = QtWidgets.QGroupBox("Timer Controls")
        timer_group.setLayout(timer_form)
        chat_group = QtWidgets.QGroupBox("Chat appearance")
        chat_group.setLayout(chat_form)

        room_group = QtWidgets.QGroupBox("Room")
        room_form = QtWidgets.QFormLayout()
        room_form.setLabelAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        room_form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        room_form.addRow("Team / Room", self.room_select)
        room_group.setLayout(room_form)

        account_group = QtWidgets.QGroupBox("Account")
        account_layout = QtWidgets.QVBoxLayout()
        self.account_label = QtWidgets.QLabel("Sign in to manage your account.")
        self.account_label.setWordWrap(True)
        account_layout.addWidget(self.account_label)
        account_actions = QtWidgets.QHBoxLayout()
        self.change_password_btn = QtWidgets.QPushButton("Change password")
        self.delete_account_btn = QtWidgets.QPushButton("Delete account")
        self.change_password_btn.setMinimumHeight(36)
        self.delete_account_btn.setMinimumHeight(36)
        self.delete_account_btn.setStyleSheet("QPushButton { background-color: #A83242; }")
        self.change_password_btn.clicked.connect(self.app.prompt_change_password)
        self.delete_account_btn.clicked.connect(self.app.prompt_delete_account)
        account_actions.addWidget(self.change_password_btn)
        account_actions.addWidget(self.delete_account_btn)
        account_layout.addLayout(account_actions)
        account_group.setLayout(account_layout)
        self.set_account("", False)

        role_group = QtWidgets.QGroupBox("Role")
        role_layout = QtWidgets.QGridLayout()
        self.role_buttons = {}
        self.role_labels = {}
        for index, role in enumerate(TEAM_ROLES):
            btn = QtWidgets.QRadioButton(role)
            btn.setMinimumHeight(34)
            self.role_buttons[role] = btn
            self.role_labels[role] = role
            role_layout.addWidget(btn, index // 2, index % 2)
        self.role_buttons[DEFAULT_ROLE].setChecked(True)
        role_group.setLayout(role_layout)

        roster_group = QtWidgets.QGroupBox("Team players")
        roster_layout = QtWidgets.QVBoxLayout()
        self.roster_label = QtWidgets.QLabel("Join a team to see who is connected.")
        self.roster_label.setWordWrap(True)
        self.roster_label.setMinimumHeight(32)
        self.password_btn = QtWidgets.QPushButton("Set team password")
        self.password_btn.setMinimumHeight(38)
        self.password_btn.setVisible(False)
        self.password_btn.clicked.connect(self.app.prompt_room_password)
        self.roster_layout = QtWidgets.QListWidget()
        self.roster_layout.setMinimumHeight(150)
        roster_layout.addWidget(self.roster_label)
        roster_layout.addWidget(self.password_btn)
        roster_layout.addWidget(self.roster_layout)
        roster_group.setLayout(roster_layout)

        exit_btn = QtWidgets.QPushButton("Exit")
        exit_btn.setMinimumHeight(40)
        exit_btn.clicked.connect(QtWidgets.QApplication.quit)

        self.status_label = QtWidgets.QLabel("WebSocket: idle")

        def tab_page(*widgets):
            page = QtWidgets.QWidget()
            page_layout = QtWidgets.QVBoxLayout(page)
            page_layout.setContentsMargins(4, 10, 4, 4)
            page_layout.setSpacing(10)
            for widget in widgets:
                page_layout.addWidget(widget)
            page_layout.addStretch(1)
            return page

        self.tabs.addTab(tab_page(player_group), "Settings")
        self.tabs.addTab(tab_page(timer_group), "Timers")
        self.tabs.addTab(tab_page(chat_group), "Chat")
        self.tabs.addTab(tab_page(room_group, role_group, roster_group), "Roles / Team")
        self.tabs.addTab(tab_page(account_group), "Account")
        root_layout.addWidget(self.tabs, 1)

        footer = QtWidgets.QHBoxLayout()
        footer.addWidget(self.status_label, 1)
        footer.addWidget(exit_btn)
        root_layout.addLayout(footer)
        self.setStyleSheet(
            "QWidget {"
            "background-color: #0F1720;"
            "color: #E8F0F8;"
            "}"
            "QGroupBox {"
            "border: 1px solid #344255;"
            "border-radius: 12px;"
            "margin-top: 18px;"
            "padding: 12px 8px 8px 8px;"
            "background-color: #182331;"
            "}"
            "QGroupBox::title {"
            "subcontrol-origin: margin;"
            "left: 12px;"
            "padding: 0 8px;"
            "color: #B8D7F1;"
            "font-weight: 700;"
            "font-size: 13px;"
            "background-color: #182331;"
            "}"
            "QTabWidget::pane {"
            "border: 1px solid #344255;"
            "border-radius: 10px;"
            "background-color: #0F1720;"
            "top: -1px;"
            "}"
            "QTabBar::tab {"
            "background-color: #182331;"
            "border: 1px solid #344255;"
            "border-bottom: none;"
            "border-top-left-radius: 7px;"
            "border-top-right-radius: 7px;"
            "padding: 7px 16px;"
            "margin-right: 4px;"
            "color: #AFC1D3;"
            "font-weight: 700;"
            "}"
            "QTabBar::tab:selected {"
            "background-color: #214D68;"
            "border-color: #61A8FF;"
            "color: #F8FBFF;"
            "}"
            "QGroupBox QLabel, QGroupBox QCheckBox, QGroupBox QRadioButton {"
            "color: #E8F0F8;"
            "}"
            "QCheckBox, QRadioButton {"
            "color: #E8F0F8;"
            "font-weight: 600;"
            "}"
            "QGroupBox QRadioButton {"
            "background-color: #223144;"
            "border: 1px solid #40536A;"
            "border-radius: 6px;"
            "padding: 6px 10px;"
            "}"
            "QGroupBox QRadioButton:checked {"
            "background-color: #174C4A;"
            "border-color: #42D2B1;"
            "}"
            "QGroupBox QRadioButton::indicator {"
            "width: 12px;"
            "height: 12px;"
            "}"
            "QGroupBox QRadioButton::indicator:checked {"
            "background-color: #42D2B1;"
            "border: 1px solid #D8FFF4;"
            "}"
            "QGroupBox QRadioButton::indicator:unchecked {"
            "background-color: #0F1720;"
            "border: 1px solid #7D91A8;"
            "}"
            "QGroupBox QCheckBox {"
            "background-color: #223144;"
            "border: 1px solid #40536A;"
            "border-radius: 6px;"
            "padding: 4px 8px;"
            "}"
            "QGroupBox QCheckBox::indicator:checked {"
            "background-color: #42D2B1;"
            "border: 1px solid #D8FFF4;"
            "}"
            "QCheckBox::indicator:unchecked, QRadioButton::indicator:unchecked {"
            "background-color: #0F1720;"
            "border: 1px solid #7D91A8;"
            "}"
            "QLineEdit, QComboBox {"
            "background-color: #223144;"
            "border: 1px solid #40536A;"
            "border-radius: 8px;"
            "padding: 6px 10px;"
            "min-height: 22px;"
            "color: #F8FBFF;"
            "selection-background-color: #2E6BFF;"
            "}"
            "QLineEdit:focus, QComboBox:focus { border: 1px solid #61A8FF; }"
            "QComboBox QAbstractItemView { background-color: #223144; color: #F8FBFF; selection-background-color: #2E6BFF; }"
            "QComboBox::down-arrow {"
            "image: none;"
            "border-left: 6px solid transparent;"
            "border-right: 6px solid transparent;"
            "border-top: 7px solid #61A8FF;"
            "margin-right: 6px;"
            "}"
            "QPushButton {"
            "background-color: #2E6BFF;"
            "border: 1px solid #6C9CFF;"
            "border-radius: 8px;"
            "padding: 7px 10px;"
            "color: #FFFFFF;"
            "font-weight: 700;"
            "}"
            "QPushButton:hover {"
            "background-color: #3F7CFF;"
            "}"
            "QPushButton:pressed {"
            "background-color: #2457CC;"
            "}"
            "QLabel {"
            "color: #D9E7F3;"
            "}"
            "QCheckBox::indicator, QRadioButton::indicator {"
            "width: 14px;"
            "height: 14px;"
            "}"
            "QCheckBox::indicator:checked {"
            "background-color: #42D2B1;"
            "border: 1px solid #D8FFF4;"
            "}"
            "QRadioButton::indicator:checked {"
            "background-color: #42D2B1;"
            "border: 1px solid #D8FFF4;"
            "}"
            "QListWidget { background-color: #101B27; border: 1px solid #40536A; border-radius: 8px; padding: 5px; color: #E8F0F8; }"
            "QListWidget::item { padding: 6px 8px; border-radius: 5px; }"
            "QListWidget::item:selected { background-color: #214D68; }"
            "QScrollBar:vertical { background: #101B27; width: 10px; margin: 4px; }"
            "QScrollBar::handle:vertical { background: #536B84; min-height: 26px; border-radius: 5px; }"
        )

        for input_widget in (
            self.name_input,
            self.times_input_1,
            self.hotkey_input_1,
            self.times_input_2,
            self.hotkey_input_2,
            self.chat_hotkey_input,
            self.overlay_hotkey_input,
        ):
            input_widget.textChanged.connect(self._queue_auto_apply)
        for selector in (self.monitor_select, self.map_select, self.room_select):
            selector.currentIndexChanged.connect(self._queue_auto_apply)
        self.compatibility_mode.toggled.connect(self._queue_auto_apply)
        self.chat_font_size.valueChanged.connect(self._queue_auto_apply)
        for button in self.role_buttons.values():
            button.toggled.connect(self._queue_auto_apply)

    def _refresh_monitors(self):
        self.monitor_select.clear()
        screens = QtWidgets.QApplication.screens()
        for i, screen in enumerate(screens):
            name = screen.name() or f"Monitor {i + 1}"
            geom = screen.availableGeometry()
            label = f"{i + 1}: {name} ({geom.width()}x{geom.height()})"
            self.monitor_select.addItem(label, i)

    def set_update_status(self, text: str, checking: bool = False):
        self.update_status.setText(text)
        self.update_button.setEnabled(not checking)

    def _current_role(self):
        for role, btn in self.role_buttons.items():
            if btn.isChecked():
                return role
        return DEFAULT_ROLE

    def set_role(self, role):
        if role in self.role_buttons:
            self.role_buttons[role].setChecked(True)

    def set_room(self, room):
        for i in range(self.room_select.count()):
            if int(self.room_select.itemData(i)) == int(room):
                self.room_select.setCurrentIndex(i)
                break

    def set_player_name(self, name):
        self.name_input.setText(name or "")

    def set_account(self, name, active):
        enabled = bool(active)
        self.change_password_btn.setEnabled(enabled)
        self.delete_account_btn.setEnabled(enabled)
        self.account_label.setText(
            f"Signed in as {name}." if enabled else "Sign in to manage your account."
        )

    def update_roster(self, room, members, locked, can_set_password=False):
        self.roster_layout.clear()
        privacy = "Password protected" if locked else "Open team"
        self.roster_label.setText(f"Team {room} · {privacy} · timer overlay shows this team's cap times.")
        self.password_btn.setVisible(bool(can_set_password and not locked))
        for member in members:
            name = str(member.get("name") or "Player")
            role = str(member.get("role") or "Player")
            self.roster_layout.addItem(f"{name} — {role}")

    def set_room_privacy(self, can_set_password, locked):
        self.password_btn.setVisible(bool(can_set_password and not locked))

    def update_team_activity(self, teams):
        selected_room = self.room_select.currentData()
        team_by_room = {int(team.get("room", 0)): team for team in teams}
        for room in range(1, MAX_ROOMS + 1):
            team = team_by_room.get(room, {})
            count = int(team.get("count", 0) or 0)
            first_name = str(team.get("first_name") or "").strip()
            index = room - 1
            if count:
                detail = f"{count} playing" + (f" · {first_name}" if first_name else "")
                self.room_select.setItemText(index, f"● Team {room} · {detail}")
                self.room_select.setItemData(
                    index,
                    QtGui.QBrush(QtGui.QColor("#42D2B1")),
                    QtCore.Qt.ItemDataRole.ForegroundRole,
                )
            else:
                self.room_select.setItemText(index, f"○ Team {room} · Empty")
                self.room_select.setItemData(
                    index,
                    QtGui.QBrush(QtGui.QColor("#93A4B8")),
                    QtCore.Qt.ItemDataRole.ForegroundRole,
                )
        if selected_room is not None:
            self.set_room(selected_room)

    def update_role_availability(self, role_owners, my_id):
        for role in LOCKED_ROLES:
            owner = role_owners.get(role)
            btn = self.role_buttons.get(role)
            if not btn:
                continue
            available = owner is None or owner == my_id
            btn.setEnabled(available)
            if available:
                btn.setText(self.role_labels[role])
            else:
                btn.setText(f"{self.role_labels[role]} (taken)")

    def prompt_role(self, current_role):
        roles = TEAM_ROLES
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Select Role")
        dialog.setModal(True)
        layout = QtWidgets.QVBoxLayout(dialog)
        label = QtWidgets.QLabel("Choose your role:")
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)

        grid = QtWidgets.QGridLayout()
        layout.addLayout(grid)

        chosen = {"role": None}

        def pick(role):
            chosen["role"] = role
            dialog.accept()

        role_colors = {
            "Capper 1": "#7A3DF0",
            "Capper 2": "#7A3DF0",
            "Offense": "#2BBE4B",
            "Defense": "#E14444",
        }
        for i, role in enumerate(roles):
            btn = QtWidgets.QPushButton(role)
            btn.setMinimumHeight(36)
            color = role_colors.get(role, "#444444")
            btn.setStyleSheet(
                "QPushButton {"
                f"background-color: {color};"
                "color: white;"
                "border: 1px solid rgba(255,255,255,0.3);"
                "border-radius: 6px;"
                "padding: 6px;"
                "font-weight: 600;"
                "}"
                "QPushButton:hover {"
                "filter: brightness(1.1);"
                "}"
                "QPushButton:pressed {"
                "filter: brightness(0.9);"
                "}"
            )
            btn.clicked.connect(lambda _, r=role: pick(r))
            grid.addWidget(btn, i // 2, i % 2)

        dialog.setLayout(layout)
        dialog.exec()
        return chosen["role"]

    def prompt_room(self, current_room):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Select Team / Room")
        dialog.setModal(True)
        layout = QtWidgets.QVBoxLayout(dialog)
        label = QtWidgets.QLabel("Choose your team/room:")
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)

        selector = QtWidgets.QComboBox()
        for room in range(1, MAX_ROOMS + 1):
            selector.addItem(f"Team {room}", room)
        selector.setCurrentIndex(max(0, int(current_room) - 1))
        layout.addWidget(selector)

        join_btn = QtWidgets.QPushButton("Join Room")
        join_btn.clicked.connect(dialog.accept)
        layout.addWidget(join_btn)

        dialog.setLayout(layout)
        dialog.exec()
        return int(selector.currentData())

    def load_current(
        self,
        times_1,
        hotkey_1,
        times_2,
        hotkey_2,
        chat_hotkey,
        overlay_hotkey,
        monitor_index,
        room,
        map_name=None,
        role=None,
        player_name="",
        compatibility_mode=False,
    ):
        self._suspend_auto_apply = True
        try:
            self._refresh_monitors()
            self.times_input_1.setText(",".join(str(t) for t in times_1))
            self.hotkey_input_1.setText(hotkey_1)
            self.times_input_2.setText(",".join(str(t) for t in times_2))
            self.hotkey_input_2.setText(hotkey_2)
            self.chat_hotkey_input.setText(chat_hotkey)
            self.overlay_hotkey_input.setText(overlay_hotkey)
            if 0 <= monitor_index < self.monitor_select.count():
                self.monitor_select.setCurrentIndex(monitor_index)
            if map_name and map_name in MAP_PRESETS:
                self.map_select.setCurrentText(map_name)
            self.set_room(room)
            if role in self.role_buttons:
                self.role_buttons[role].setChecked(True)
            self.set_player_name(player_name)
            self.compatibility_mode.setChecked(bool(compatibility_mode))
        finally:
            self._suspend_auto_apply = False

    def set_status(self, text: str):
        self.status_label.setText(text)

    def _queue_auto_apply(self, *_):
        if not self._suspend_auto_apply:
            self._auto_apply_timer.start()

    def _update_chat_color_buttons(self):
        for button, color, label in (
            (self.chat_text_color_btn, self.chat_text_color, "Message color"),
        ):
            button.setText(f"{label}: {color}")
            button.setStyleSheet(
                f"QPushButton {{ background-color: {color}; color: {'#101820' if QtGui.QColor(color).lightness() > 150 else '#FFFFFF'}; }}"
            )

    def _choose_chat_color(self, target):
        current = self.chat_text_color
        color = QtWidgets.QColorDialog.getColor(QtGui.QColor(current), self, "Choose chat color")
        if not color.isValid():
            return
        self.chat_text_color = color.name().upper()
        self._update_chat_color_buttons()
        self._queue_auto_apply()

    def _apply_current_settings(self):
        times_text_1 = self.times_input_1.text().strip()
        player_name = self.name_input.text().strip()
        hotkey_text_1 = self.hotkey_input_1.text().strip().lower()
        times_text_2 = self.times_input_2.text().strip()
        hotkey_text_2 = self.hotkey_input_2.text().strip().lower()
        chat_hotkey_text = self.chat_hotkey_input.text().strip().lower()
        overlay_hotkey_text = self.overlay_hotkey_input.text().strip().lower()
        monitor_index = int(self.monitor_select.currentData())
        room = int(self.room_select.currentData())
        self.app.update_settings(
            times_text_1,
            hotkey_text_1,
            times_text_2,
            hotkey_text_2,
            chat_hotkey_text,
            overlay_hotkey_text,
            monitor_index,
            room,
            self.map_select.currentText(),
            self._current_role(),
            player_name,
            self.compatibility_mode.isChecked(),
            self.chat_font_size.value(),
            self.chat_text_color,
        )

    def _load_presets(self):
        try:
            if os.path.exists(PRESET_FILE):
                with open(PRESET_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            print(f"Failed to load presets: {e}")
        return {}

    def load_last_preset(self):
        presets = self._load_presets()
        last_map = presets.get("_last_map")
        last_room = presets.get("_last_room")
        self.chat_hotkey_input.setText(presets.get("_chat_hotkey", CHAT_HOTKEY))
        self.overlay_hotkey_input.setText(presets.get("_overlay_toggle_hotkey", OVERLAY_TOGGLE_HOTKEY))
        player_name = str(presets.get("_player_name") or "").strip()
        # Gameplay overlays are the default. The only persisted exception is an
        # explicit opt-out from the player through the compatibility checkbox.
        compatibility_mode = bool(presets.get("_compatibility_mode", False))
        self.chat_font_size.setValue(max(10, min(24, int(presets.get("_chat_font_size", 12)))))
        self.chat_text_color = str(presets.get("_chat_text_color") or "#F8FBFF")
        self._update_chat_color_buttons()
        blocker = QtCore.QSignalBlocker(self.compatibility_mode)
        self.compatibility_mode.setChecked(compatibility_mode)
        del blocker
        self.app.set_compatibility_mode(compatibility_mode)
        if player_name:
            self.app.player_name = player_name
            self.set_player_name(player_name)
        if last_room:
            self.set_room(last_room)
        if last_map in MAP_PRESETS:
            self.map_select.setCurrentText(last_map)
            preset = presets.get(last_map)
            if isinstance(preset, dict):
                self.times_input_1.setText(preset.get("times_1", ""))
                self.hotkey_input_1.setText(preset.get("hotkey_1", HOTKEY_1))
                self.times_input_2.setText(preset.get("times_2", ""))
                self.hotkey_input_2.setText(preset.get("hotkey_2", HOTKEY_2))
                self.chat_hotkey_input.setText(presets.get("_chat_hotkey", CHAT_HOTKEY))
                self.overlay_hotkey_input.setText(presets.get("_overlay_toggle_hotkey", OVERLAY_TOGGLE_HOTKEY))
                monitor_index = int(preset.get("monitor_index", 0))
                if 0 <= monitor_index < self.monitor_select.count():
                    self.monitor_select.setCurrentIndex(monitor_index)
            self._apply_current_settings()
        else:
            self._apply_current_settings()
        role = presets.get("_last_role")
        if role in self.role_buttons:
            self.role_buttons[role].setChecked(True)
            self._apply_current_settings()

    def _save_presets(self, data):
        try:
            os.makedirs(PRESET_DIR, exist_ok=True)
            with open(PRESET_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
        except Exception as e:
            print(f"Failed to save presets: {e}")

    def _save_last_role(self, role):
        presets = self._load_presets()
        presets["_last_role"] = role
        self._save_presets(presets)

    def _save_last_room(self, room):
        presets = self._load_presets()
        presets["_last_room"] = int(room)
        self._save_presets(presets)

    def _save_player_name(self, name):
        presets = self._load_presets()
        presets["_player_name"] = name
        self._save_presets(presets)

    def _save_compatibility_mode(self, enabled):
        presets = self._load_presets()
        presets["_compatibility_mode"] = bool(enabled)
        self._save_presets(presets)

    def _save_chat_hotkey(self, hotkey):
        presets = self._load_presets()
        presets["_chat_hotkey"] = str(hotkey or CHAT_HOTKEY).strip().lower()
        self._save_presets(presets)

    def _save_overlay_hotkey(self, hotkey):
        presets = self._load_presets()
        presets["_overlay_toggle_hotkey"] = str(hotkey or OVERLAY_TOGGLE_HOTKEY).strip().lower()
        self._save_presets(presets)

    def _save_chat_appearance(self, font_size, text_color):
        presets = self._load_presets()
        presets["_chat_font_size"] = int(font_size)
        presets["_chat_text_color"] = str(text_color)
        self._save_presets(presets)

    def _on_load_preset(self):
        map_name = self.map_select.currentText()
        presets = self._load_presets()
        preset = presets.get(map_name)
        if not isinstance(preset, dict):
            return
        self.times_input_1.setText(preset.get("times_1", ""))
        self.hotkey_input_1.setText(preset.get("hotkey_1", HOTKEY_1))
        self.times_input_2.setText(preset.get("times_2", ""))
        self.hotkey_input_2.setText(preset.get("hotkey_2", HOTKEY_2))
        self.chat_hotkey_input.setText(presets.get("_chat_hotkey", CHAT_HOTKEY))
        self.overlay_hotkey_input.setText(presets.get("_overlay_toggle_hotkey", OVERLAY_TOGGLE_HOTKEY))
        monitor_index = int(preset.get("monitor_index", 0))
        if 0 <= monitor_index < self.monitor_select.count():
            self.monitor_select.setCurrentIndex(monitor_index)
        if map_name in MAP_PRESETS:
            self.map_select.setCurrentText(map_name)
        self._apply_current_settings()

    def _on_save_preset(self):
        map_name = self.map_select.currentText()
        presets = self._load_presets()
        presets[map_name] = {
            "times_1": self.times_input_1.text().strip(),
            "hotkey_1": self.hotkey_input_1.text().strip().lower(),
            "times_2": self.times_input_2.text().strip(),
            "hotkey_2": self.hotkey_input_2.text().strip().lower(),
            "monitor_index": int(self.monitor_select.currentData()),
        }
        presets["_chat_hotkey"] = self.chat_hotkey_input.text().strip().lower()
        presets["_overlay_toggle_hotkey"] = self.overlay_hotkey_input.text().strip().lower()
        presets["_last_map"] = map_name
        presets["_last_role"] = self._current_role()
        self._save_presets(presets)


class CreateAccountDialog(QtWidgets.QDialog):
    """Dedicated first-time account flow, kept separate from sign-in."""

    def __init__(self, sign_in_dialog):
        super().__init__(sign_in_dialog)
        self.sign_in_dialog = sign_in_dialog
        self.setWindowTitle("Create ShazChat account")
        self.setModal(True)
        self.setMinimumWidth(380)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(10)
        title = QtWidgets.QLabel("Create your account")
        title.setStyleSheet("font-size: 17px; font-weight: 700;")
        layout.addWidget(title)
        info = QtWidgets.QLabel(
            "Choose the player name your teammates will see. Your password is stored on the server as a secure hash."
        )
        info.setWordWrap(True)
        layout.addWidget(info)
        form = QtWidgets.QFormLayout()
        self.username = QtWidgets.QLineEdit()
        self.username.setMaxLength(32)
        self.username.setPlaceholderText("3–32 letters, numbers, dots, dashes, or underscores")
        self.password = QtWidgets.QLineEdit()
        self.password.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("At least 8 characters")
        self.confirm = QtWidgets.QLineEdit()
        self.confirm.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.confirm.setPlaceholderText("Enter the same password again")
        self.recovery_pin = QtWidgets.QLineEdit()
        self.recovery_pin.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.recovery_pin.setInputMask("999999999999; ")
        self.recovery_pin.setPlaceholderText("4–12 digits; used only to reset your password")
        self.confirm_pin = QtWidgets.QLineEdit()
        self.confirm_pin.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.confirm_pin.setInputMask("999999999999; ")
        self.confirm_pin.setPlaceholderText("Enter the same PIN again")
        form.addRow("Player name", self.username)
        form.addRow("Password", self.password)
        form.addRow("Confirm password", self.confirm)
        form.addRow("Recovery PIN", self.recovery_pin)
        form.addRow("Confirm PIN", self.confirm_pin)
        layout.addLayout(form)
        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QtWidgets.QHBoxLayout()
        cancel = QtWidgets.QPushButton("Back to sign in")
        self.create = QtWidgets.QPushButton("Create account")
        cancel.clicked.connect(self.reject)
        self.create.clicked.connect(self._submit)
        self.confirm.returnPressed.connect(self._submit)
        buttons.addWidget(cancel)
        buttons.addWidget(self.create)
        layout.addLayout(buttons)

    def open_for_name(self, name):
        self.username.setText(name)
        self.password.clear()
        self.confirm.clear()
        self.recovery_pin.clear()
        self.confirm_pin.clear()
        self.status.setText("")
        self.create.setEnabled(True)
        self.show()
        self.raise_()
        self.activateWindow()

    def set_busy(self, busy):
        self.create.setEnabled(not busy)

    def _submit(self):
        username = self.username.text().strip()
        password = self.password.text()
        if password != self.confirm.text():
            self.status.setText("Passwords do not match.")
            return
        recovery_pin = self.recovery_pin.text().strip()
        if recovery_pin != self.confirm_pin.text().strip():
            self.status.setText("Recovery PINs do not match.")
            return
        self.status.setText("Creating account…")
        self.sign_in_dialog.submit_create(username, password, recovery_pin)

    def show_error(self, text):
        self.password.clear()
        self.confirm.clear()
        self.recovery_pin.clear()
        self.confirm_pin.clear()
        self.status.setText(text)
        self.create.setEnabled(True)


class PasswordRecoveryDialog(QtWidgets.QDialog):
    """Self-service password reset backed only by the player's recovery PIN."""

    def __init__(self, sign_in_dialog):
        super().__init__(sign_in_dialog)
        self.sign_in_dialog = sign_in_dialog
        self.setWindowTitle("Reset ShazChat password")
        self.setModal(True)
        self.setMinimumWidth(390)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(10)
        title = QtWidgets.QLabel("Reset your password")
        title.setStyleSheet("font-size: 17px; font-weight: 700;")
        layout.addWidget(title)
        info = QtWidgets.QLabel("Enter the recovery PIN you chose when creating this account, then choose a new password.")
        info.setWordWrap(True)
        layout.addWidget(info)
        form = QtWidgets.QFormLayout()
        self.username = QtWidgets.QLineEdit()
        self.username.setMaxLength(32)
        self.recovery_pin = QtWidgets.QLineEdit()
        self.recovery_pin.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.recovery_pin.setInputMask("999999999999; ")
        self.new_password = QtWidgets.QLineEdit()
        self.new_password.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.new_password.setPlaceholderText("At least 8 characters")
        self.confirm = QtWidgets.QLineEdit()
        self.confirm.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.confirm.setPlaceholderText("Enter the same password again")
        form.addRow("Player name", self.username)
        form.addRow("Recovery PIN", self.recovery_pin)
        form.addRow("New password", self.new_password)
        form.addRow("Confirm password", self.confirm)
        layout.addLayout(form)
        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QtWidgets.QHBoxLayout()
        cancel = QtWidgets.QPushButton("Back to sign in")
        self.reset = QtWidgets.QPushButton("Reset password")
        cancel.clicked.connect(self.reject)
        self.reset.clicked.connect(self._submit)
        self.confirm.returnPressed.connect(self._submit)
        buttons.addWidget(cancel)
        buttons.addWidget(self.reset)
        layout.addLayout(buttons)

    def open_for_name(self, name):
        self.username.setText(name)
        self.recovery_pin.clear()
        self.new_password.clear()
        self.confirm.clear()
        self.status.setText("")
        self.reset.setEnabled(True)
        self.show()
        self.raise_()
        self.activateWindow()

    def _submit(self):
        username = self.username.text().strip()
        pin = self.recovery_pin.text().strip()
        password = self.new_password.text()
        if not username or not pin or not password:
            self.status.setText("Complete every field to reset your password.")
            return
        if password != self.confirm.text():
            self.status.setText("Passwords do not match.")
            return
        self.reset.setEnabled(False)
        self.status.setText("Resetting password…")
        self.sign_in_dialog.submit_password_reset(username, pin, password)

    def show_error(self, text):
        self.recovery_pin.clear()
        self.new_password.clear()
        self.confirm.clear()
        self.status.setText(text)
        self.reset.setEnabled(True)


class AccountDialog(QtWidgets.QDialog):
    """Sign-in surface with a separate, explicit account-creation dialog."""

    def __init__(self, app, suggested_name=""):
        super().__init__(app.settings)
        self.app = app
        self.setWindowTitle("ShazChat account")
        self.setModal(False)
        self.setMinimumWidth(360)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(10)
        title = QtWidgets.QLabel("Sign in to ShazChat")
        title.setStyleSheet("font-size: 17px; font-weight: 700;")
        layout.addWidget(title)
        info = QtWidgets.QLabel("Use your existing player name and password to join your team.")
        info.setWordWrap(True)
        layout.addWidget(info)
        form = QtWidgets.QFormLayout()
        self.username = QtWidgets.QLineEdit(suggested_name)
        self.username.setMaxLength(32)
        self.username.setPlaceholderText("Player name")
        self.password = QtWidgets.QLineEdit()
        self.password.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Password")
        form.addRow("Player name", self.username)
        form.addRow("Password", self.password)
        layout.addLayout(form)
        self.status = QtWidgets.QLabel("Connecting to server…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QtWidgets.QHBoxLayout()
        self.sign_in = QtWidgets.QPushButton("Sign in")
        self.create = QtWidgets.QPushButton("Create a new account")
        self.reset_password = QtWidgets.QPushButton("Reset password")
        self.sign_in.clicked.connect(self._submit_sign_in)
        self.password.returnPressed.connect(self._submit_sign_in)
        self.create.clicked.connect(self._open_create_account)
        self.reset_password.clicked.connect(self._open_password_reset)
        buttons.addWidget(self.sign_in)
        buttons.addWidget(self.create)
        layout.addLayout(buttons)
        layout.addWidget(self.reset_password)
        self.create_dialog = CreateAccountDialog(self)
        self.password_recovery_dialog = PasswordRecoveryDialog(self)
        self.set_ready(False)

    def set_ready(self, ready):
        self.sign_in.setEnabled(bool(ready))
        self.create.setEnabled(bool(ready))
        self.reset_password.setEnabled(bool(ready))
        if ready:
            self.status.setText("Sign in, or create your account the first time you use ShazChat.")

    def _submit_sign_in(self):
        username = self.username.text().strip()
        password = self.password.text()
        if not username or not password:
            self.status.setText("Enter your player name and password.")
            return
        self.set_ready(False)
        self.status.setText("Signing in…")
        self.app.submit_account(username, password, False)

    def _open_create_account(self):
        self.create_dialog.open_for_name(self.username.text().strip())

    def _open_password_reset(self):
        self.password_recovery_dialog.open_for_name(self.username.text().strip())

    def submit_create(self, username, password, recovery_pin):
        if not username or not password:
            self.create_dialog.show_error("Enter a player name and password.")
            return
        self.set_ready(False)
        self.create_dialog.set_busy(True)
        self.app.submit_account(username, password, True, recovery_pin)

    def submit_password_reset(self, username, recovery_pin, new_password):
        self.set_ready(False)
        self.app.submit_password_reset(username, recovery_pin, new_password)

    def show_error(self, text):
        if self.create_dialog.isVisible():
            self.create_dialog.show_error(text)
            self.set_ready(True)
            return
        if self.password_recovery_dialog.isVisible():
            self.password_recovery_dialog.show_error(text)
            self.set_ready(True)
            return
        self.password.clear()
        self.status.setText(text)
        self.sign_in.setEnabled(True)
        self.create.setEnabled(True)
        self.reset_password.setEnabled(True)


class CapTimerApp:
    def __init__(self, network=True, server_url=None, room=DEFAULT_ROOM):
        self.network_enabled = network
        self.app = QtWidgets.QApplication(sys.argv)
        self.logger = configure_diagnostics()
        self.update_check_in_progress = False
        self.update_install_in_progress = False
        self.update_release = None
        self._update_prompted_version = None
        self.ui_dispatcher = UiDispatcher()
        self.room = int(room)
        self.window = OverlayWindow()
        self.settings = SettingsWindow(self)
        self.chat = ChatWindow(self)
        self.chat_overlay = ChatOverlayWindow()
        self.connection_badge = ConnectionBadgeWindow()
        self.connection_state = "offline"
        self.cycle_index = [-1, -1]
        self.lock = threading.Lock()
        self.compatibility_mode_enabled = False
        # This is intentionally session-only: overlays start visible on launch
        # unless the player explicitly enables compatibility mode.
        self.gameplay_overlays_hidden = False
        self._text_entry_focused = False
        # Linux listener callbacks arrive on a non-Qt thread; route both
        # platform backends through the dispatcher before touching the UI.
        self.hotkey_manager = NativeHotkeyManager(
            self.app,
            lambda hotkey_id: self.dispatch_ui(
                lambda hotkey_id=hotkey_id: self._on_registered_hotkey(hotkey_id)
            ),
        )
        self._hotkey_down = {1: False, 2: False, 3: False, 4: False}
        self._last_hotkey_fire = 0.0
        self._last_toggle_hotkey = 0.0
        self._game_window_handle = 0
        self._hotkey_poll_timer = QtCore.QTimer(self.app)
        self._hotkey_poll_timer.setInterval(12)
        self._hotkey_poll_timer.timeout.connect(self._poll_held_hotkeys)
        self.app.focusChanged.connect(self._handle_focus_change)
        self.monitor_index = 0
        self.selected_map = "Custom"
        self.player_name = ""
        self.chat_font_size = 12
        self.chat_text_color = "#F8FBFF"
        self.player_id = MY_ID
        self.authenticated = not bool(server_url)
        self.account_dialog = None
        self._session_started = False
        self.role = None
        self.pending_room = None
        self.role_owners = {role: None for role in LOCKED_ROLES}
        self.claimed_role = None
        self.pending_role = None

        # WebSocket support
        self.ws_client = None
        self.ws_loop = None
        if server_url and websockets:
            self.set_connection_state("connecting")
            self.update_status("WebSocket: connecting...")
            print(f"Connecting to WebSocket server: {server_url}")
            self.ws_loop = asyncio.new_event_loop()
            self.ws_thread = threading.Thread(target=self._run_ws_loop, daemon=True)
            self.ws_thread.start()
            self.ws_client = WebSocketClient(server_url, self)
            # Connect asynchronously
            asyncio.run_coroutine_threadsafe(self.ws_client.run(), self.ws_loop)
            # Store loop reference in client
            self.ws_client.loop = self.ws_loop
        elif server_url:
            print("WARNING: websockets library not available. Install with: pip install websockets")
            self.update_status("WebSocket: missing dependency")
            self.set_connection_state("offline")
            QtWidgets.QMessageBox.warning(
                None,
                "Missing Dependency",
                "The 'websockets' library is missing, so network sync is disabled.",
            )

        if not server_url:
            self.update_status("Local-only mode: shared team timers require the server")
            self.set_connection_state("offline")

        self.app.aboutToQuit.connect(self.shutdown)
        self._refresh_hotkeys()

    def dispatch_ui(self, callback):
        self.ui_dispatcher.callback.emit(callback)

    def check_for_updates(self, manual=False):
        """Check the signed stable-channel manifest without blocking the game UI."""
        if self.update_check_in_progress or self.update_install_in_progress:
            return
        self.update_check_in_progress = True
        self.settings.set_update_status("Checking for updates…", checking=True)

        def worker():
            try:
                release = fetch_update()
                available = is_newer(release)
                result = (release, available, None)
            except UpdateError as exc:
                result = (None, False, str(exc))
            except Exception as exc:
                self.logger.exception("Unexpected update-check failure")
                result = (None, False, f"Unable to check for updates: {exc}")
            self.dispatch_ui(lambda: self._finish_update_check(*result, manual=manual))

        threading.Thread(target=worker, name="capper-update-check", daemon=True).start()

    def _finish_update_check(self, release, available, error, manual=False):
        self.update_check_in_progress = False
        self.settings.set_update_status(f"Version {APP_VERSION}")
        if error:
            self.logger.warning("Update check failed: %s", error)
            if manual:
                QtWidgets.QMessageBox.warning(self.settings, "Updates unavailable", error)
            return
        if not available:
            self.logger.info("Update check complete: version %s is current", APP_VERSION)
            if manual:
                QtWidgets.QMessageBox.information(
                    self.settings,
                    "ShazChat is current",
                    f"You are already using the latest version ({APP_VERSION}).",
                )
            return
        self.update_release = release
        self.settings.set_update_status(f"Version {release.version} available")
        self.logger.info("Signed update available: %s", release.version)
        if self._update_prompted_version != release.version:
            self._update_prompted_version = release.version
            self._show_update_available(release)

    def _show_update_available(self, release):
        dialog = QtWidgets.QMessageBox(self.settings)
        dialog.setIcon(QtWidgets.QMessageBox.Icon.Information)
        dialog.setWindowTitle("ShazChat update available")
        dialog.setText(f"ShazChat {release.version} is ready to install.")
        dialog.setInformativeText(
            "The installer is downloaded from the official ShazChat release site and verified before it runs.\n\n"
            f"What's new:\n{release.notes or 'No release notes provided.'}"
        )
        install_button = dialog.addButton("Download and install", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        dialog.addButton("Later", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        dialog.exec()
        if dialog.clickedButton() is install_button:
            self.install_available_update()

    def install_available_update(self):
        release = self.update_release
        if not release or self.update_install_in_progress:
            return
        self.update_install_in_progress = True
        self.settings.set_update_status(f"Downloading {release.version}…", checking=True)

        def worker():
            try:
                installer = download_verified_installer(release, UPDATE_CACHE_DIR)
                result = (installer, None)
            except UpdateError as exc:
                result = (None, str(exc))
            except Exception as exc:
                self.logger.exception("Unexpected update-download failure")
                result = (None, f"Unable to download update: {exc}")
            self.dispatch_ui(lambda: self._finish_update_download(*result))

        threading.Thread(target=worker, name="capper-update-download", daemon=True).start()

    def _finish_update_download(self, installer, error):
        self.update_install_in_progress = False
        self.settings.set_update_status(f"Version {APP_VERSION}")
        if error:
            self.logger.warning("Update download failed: %s", error)
            QtWidgets.QMessageBox.warning(self.settings, "Update was not installed", error)
            return
        self.logger.info("Verified update installer downloaded: %s", installer)
        self.update_status("Update verified. Restarting to install…")
        # A tiny detached shell waits for this process to close before Inno Setup
        # replaces the executable. Inno launches the new version after install.
        command = (
            "ping 127.0.0.1 -n 3 > nul & "
            f'start "" "{installer}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS'
        )
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(["cmd.exe", "/d", "/s", "/c", command], creationflags=flags)
        except OSError as exc:
            self.logger.exception("Unable to launch verified installer")
            QtWidgets.QMessageBox.critical(self.settings, "Update could not start", str(exc))
            return
        QtCore.QTimer.singleShot(100, QtWidgets.QApplication.quit)

    def _run_ws_loop(self):
        """Run asyncio event loop in separate thread"""
        asyncio.set_event_loop(self.ws_loop)
        self.ws_loop.run_forever()

    def _on_registered_hotkey(self, hotkey_id):
        if hotkey_id in (1, 2):
            self._fire_hotkey(hotkey_id)
        elif hotkey_id == 3:
            self.open_chat_composer()
        elif hotkey_id == 4:
            self._toggle_gameplay_overlays_from_hotkey()

    def _fire_hotkey(self, hotkey_id):
        """Trigger once when either Windows hotkey path observes the key press."""
        now = time.monotonic()
        # RegisterHotKey and the physical-state fallback can see the same press.
        if now - self._last_hotkey_fire < 0.12:
            return
        self._last_hotkey_fire = now
        self.trigger_timer(hotkey_id - 1)

    def _toggle_gameplay_overlays_from_hotkey(self):
        """Ignore duplicate events from the native listener and polling fallback."""
        now = time.monotonic()
        if now - self._last_toggle_hotkey < 0.3:
            return
        self._last_toggle_hotkey = now
        self.toggle_gameplay_overlays()

    def _key_is_held(self, virtual_key):
        if not sys.platform.startswith("win"):
            return False
        try:
            return bool(ctypes.windll.user32.GetAsyncKeyState(int(virtual_key)) & 0x8000)
        except Exception:
            return False

    def _poll_held_hotkeys(self):
        """Fallback for games that suppress RegisterHotKey while other keys are held."""
        if self.compatibility_mode_enabled or self._text_entry_focused:
            self._hotkey_down = {1: False, 2: False, 3: False, 4: False}
            return
        for hotkey_id, key in self._active_hotkey_bindings():
            virtual_key = NativeHotkeyManager._virtual_key(key)
            down = bool(virtual_key is not None and self._key_is_held(virtual_key))
            if down and not self._hotkey_down.get(hotkey_id, False):
                if hotkey_id == 3:
                    self.open_chat_composer()
                elif hotkey_id == 4:
                    self._toggle_gameplay_overlays_from_hotkey()
                else:
                    self._fire_hotkey(hotkey_id)
            self._hotkey_down[hotkey_id] = down

    @staticmethod
    def _is_text_entry(widget):
        """True when a global letter hotkey would steal normal typing."""
        if isinstance(widget, (QtWidgets.QLineEdit, QtWidgets.QTextEdit, QtWidgets.QPlainTextEdit, QtWidgets.QAbstractSpinBox)):
            return True
        return isinstance(widget, QtWidgets.QComboBox) and widget.isEditable()

    def _handle_focus_change(self, _old, current):
        self._sync_text_entry_focus(current)

    def _sync_text_entry_focus(self, current=None):
        """Suspend global keys whenever any ShazChat text field is focused."""
        typing = self.chat.input.hasFocus() or self._is_text_entry(
            current if current is not None else self.app.focusWidget()
        )
        if typing == self._text_entry_focused:
            return
        self._text_entry_focused = typing
        self._refresh_hotkeys()
        self.logger.info("Global hotkeys %s while text input is focused", "suspended" if typing else "restored")

    def _refresh_hotkeys(self):
        self.hotkey_manager.unregister(1)
        self.hotkey_manager.unregister(2)
        self.hotkey_manager.unregister(3)
        self.hotkey_manager.unregister(4)
        self._hotkey_down = {1: False, 2: False, 3: False, 4: False}
        self._hotkey_poll_timer.stop()
        if self.compatibility_mode_enabled:
            self.logger.info("Compatibility mode enabled; global hotkeys are disabled")
            return
        if self._text_entry_focused:
            # RegisterHotKey consumes its key before QLineEdit can receive it.
            # Releasing the registrations while typing lets chat use every key.
            return
        # Games sometimes suppress Windows registered-hotkey messages while
        # movement or mouse buttons are held. Poll physical key state there as
        # a non-injecting fallback. Linux uses its listener backend directly.
        if sys.platform.startswith("win"):
            self._hotkey_poll_timer.start()
        failures = []
        for hotkey_id, key in self._active_hotkey_bindings():
            ok, reason = self.hotkey_manager.register(hotkey_id, key)
            if not ok:
                failures.append(reason)
        if failures:
            message = "Global hotkey unavailable. " + " ".join(failures)
            self.logger.warning(message)
            self.update_status(message)
        else:
            backend = "Windows registered" if sys.platform.startswith("win") else "Linux listener"
            self.logger.info("%s hotkeys enabled: %s", backend, ", ".join(key for _, key in self._active_hotkey_bindings()))

    def _active_hotkey_bindings(self):
        """Keep only the toggle binding while gameplay controls are paused."""
        if self.gameplay_overlays_hidden:
            return ((4, OVERLAY_TOGGLE_HOTKEY),)
        return (
            (1, HOTKEY_1),
            (2, HOTKEY_2),
            (3, CHAT_HOTKEY),
            (4, OVERLAY_TOGGLE_HOTKEY),
        )

    def open_chat_composer(self):
        """Focus chat only when the player explicitly uses the chat hotkey."""
        if self.compatibility_mode_enabled or self._text_entry_focused:
            return
        if sys.platform.startswith("win"):
            try:
                foreground = int(ctypes.windll.user32.GetForegroundWindow())
                our_windows = {int(widget.winId()) for widget in (self.chat, self.settings, self.window, self.chat_overlay)}
                if foreground and foreground not in our_windows:
                    self._game_window_handle = foreground
            except Exception:
                pass
        self.chat.show()
        self.chat.raise_()
        self.chat.activateWindow()
        self.chat.input.setFocus(QtCore.Qt.FocusReason.ShortcutFocusReason)
        self.chat.input.selectAll()

    def finish_chat_entry(self):
        """Return keyboard focus to the prior game window after Send or Escape."""
        self.chat.input.clearFocus()
        if not (sys.platform.startswith("win") and self._game_window_handle):
            return
        try:
            if ctypes.windll.user32.IsWindow(self._game_window_handle):
                ctypes.windll.user32.SetForegroundWindow(self._game_window_handle)
        except Exception:
            self.logger.debug("Could not return focus to the game window", exc_info=True)

    def set_compatibility_mode(self, enabled):
        enabled = bool(enabled)
        if self.compatibility_mode_enabled == enabled:
            return
        self.compatibility_mode_enabled = enabled
        self.settings._save_compatibility_mode(enabled)
        self._refresh_hotkeys()
        if enabled:
            self.window.hide()
            self.chat_overlay.hide()
            self.connection_badge.hide()
            self.update_status("Compatibility mode: gameplay overlays and global hotkeys are disabled")
        else:
            self._set_gameplay_overlays_visible(not self.gameplay_overlays_hidden)
            self.update_status("Compatibility mode off: gameplay controls restored" if not self.gameplay_overlays_hidden else "Compatibility mode off: controls remain paused")

    def _set_gameplay_overlays_visible(self, visible):
        if self.compatibility_mode_enabled:
            self.window.hide()
            self.chat_overlay.hide()
            self.connection_badge.hide()
            return
        if visible:
            self.position_window()
            self.chat_overlay.show()
            self.connection_badge.show()
        else:
            self.window.hide()
            self.chat_overlay.hide()
            self.connection_badge.hide()

    def toggle_gameplay_overlays(self):
        if self.compatibility_mode_enabled:
            self.update_status("Compatibility mode already has gameplay overlays off")
            return
        self.gameplay_overlays_hidden = not self.gameplay_overlays_hidden
        self._set_gameplay_overlays_visible(not self.gameplay_overlays_hidden)
        self._refresh_hotkeys()
        self.update_status(
            "Gameplay controls paused — press the toggle hotkey again to restore"
            if self.gameplay_overlays_hidden
            else "Gameplay overlays and hotkeys restored"
        )

    def shutdown(self):
        self.logger.info("ShazChat shutting down")
        self.set_connection_state("offline")
        self._hotkey_poll_timer.stop()
        self.hotkey_manager.close()
        if self.ws_client:
            self.ws_client.close()
        if self.ws_loop and self.ws_loop.is_running():
            self.ws_loop.call_soon_threadsafe(self.ws_loop.stop)

    def update_settings(
        self,
        times_text_1: str,
        hotkey_text_1: str,
        times_text_2: str,
        hotkey_text_2: str,
        chat_hotkey_text: str,
        overlay_hotkey_text: str,
        monitor_index: int,
        room: int,
        map_name: Optional[str] = None,
        role: Optional[str] = None,
        player_name: Optional[str] = None,
        compatibility_mode: Optional[bool] = None,
        chat_font_size: Optional[int] = None,
        chat_text_color: Optional[str] = None,
    ):
        global HOTKEY_1, HOTKEY_2, CHAT_HOTKEY, OVERLAY_TOGGLE_HOTKEY, TIMER_OPTIONS_1, TIMER_OPTIONS_2
        with self.lock:
            new_times_1 = []
            if times_text_1:
                for part in times_text_1.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    try:
                        new_times_1.append(int(part))
                    except ValueError:
                        continue
            if new_times_1:
                TIMER_OPTIONS_1 = new_times_1
                self.cycle_index[0] = -1

            new_times_2 = []
            if times_text_2:
                for part in times_text_2.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    try:
                        new_times_2.append(int(part))
                    except ValueError:
                        continue
            if new_times_2:
                TIMER_OPTIONS_2 = new_times_2
                self.cycle_index[1] = -1

            hotkeys_changed = False
            if hotkey_text_1 and hotkey_text_1 != HOTKEY_1:
                HOTKEY_1 = hotkey_text_1
                hotkeys_changed = True
            if hotkey_text_2 and hotkey_text_2 != HOTKEY_2:
                HOTKEY_2 = hotkey_text_2
                hotkeys_changed = True
            if chat_hotkey_text and chat_hotkey_text != CHAT_HOTKEY:
                CHAT_HOTKEY = chat_hotkey_text
                self.settings._save_chat_hotkey(CHAT_HOTKEY)
                hotkeys_changed = True
            if overlay_hotkey_text and overlay_hotkey_text != OVERLAY_TOGGLE_HOTKEY:
                OVERLAY_TOGGLE_HOTKEY = overlay_hotkey_text
                self.settings._save_overlay_hotkey(OVERLAY_TOGGLE_HOTKEY)
                hotkeys_changed = True
            if hotkeys_changed:
                self._refresh_hotkeys()

            if monitor_index != self.monitor_index:
                self.monitor_index = monitor_index
                self.position_window()
            if room and int(room) != int(self.room):
                self._request_room(int(room))
            if map_name:
                self.selected_map = map_name
            if player_name:
                self.player_name = player_name.strip()[:32]
                self.settings._save_player_name(self.player_name)
            if compatibility_mode is not None:
                self.set_compatibility_mode(compatibility_mode)
            if chat_font_size is not None and chat_text_color:
                self.chat_font_size = max(10, min(24, int(chat_font_size)))
                text_color = QtGui.QColor(chat_text_color)
                self.chat_text_color = text_color.name().upper() if text_color.isValid() else "#F8FBFF"
                self.chat.set_appearance(self.chat_font_size, self.chat_text_color)
                self.chat_overlay.set_appearance(self.chat_font_size, self.chat_text_color)
                self.settings._save_chat_appearance(self.chat_font_size, self.chat_text_color)
            if role:
                if role in LOCKED_ROLES:
                    if self.role_owners.get(role) == self.player_id or self.role_owners.get(role) is None:
                        self._request_role(role)
                    else:
                        self.update_status(f"Role '{role}' is already taken")
                else:
                    self._set_role(role)

    def update_status(self, text: str):
        self.settings.set_status(text)

    def set_connection_state(self, state: str):
        """Render the sync state without relying on the settings window being open."""
        self.connection_state = state if state in ConnectionBadgeWindow.STATES else "offline"
        self.connection_badge.set_state(self.connection_state)
        if self.connection_badge._positioned and not self.compatibility_mode_enabled and not self.gameplay_overlays_hidden:
            self.connection_badge.show()

    def on_ws_connected(self):
        if self.authenticated:
            self._request_room(self.room)
            if self.role in LOCKED_ROLES:
                QtCore.QTimer.singleShot(250, lambda: self._request_role(self.role))
        elif self.account_dialog:
            self.account_dialog.set_ready(True)

    def _show_account_dialog(self):
        if self.account_dialog is None:
            self.account_dialog = AccountDialog(self, self.player_name)
        self.account_dialog.show()
        self.account_dialog.raise_()
        self.account_dialog.activateWindow()
        if self.ws_client and self.ws_client.running:
            self.account_dialog.set_ready(True)

    def submit_account(self, username, password, create=False, recovery_pin=""):
        if not self.ws_client or not self.ws_client.running:
            if self.account_dialog:
                self.account_dialog.show_error("Still connecting to the ShazChat server.")
            return
        asyncio.run_coroutine_threadsafe(
            self.ws_client.send_auth(username, password, create, recovery_pin), self.ws_loop
        )

    def submit_password_reset(self, username, recovery_pin, new_password):
        if not self.ws_client or not self.ws_client.running:
            if self.account_dialog:
                self.account_dialog.password_recovery_dialog.show_error("Still connecting to the ShazChat server.")
            return
        asyncio.run_coroutine_threadsafe(
            self.ws_client.send_password_reset(username, recovery_pin, new_password), self.ws_loop
        )

    def handle_auth_result(self, data):
        if not data.get("ok"):
            message = str(data.get("message") or "Unable to sign in.")
            if data.get("reason") == "throttled":
                message = "Too many sign-in attempts. Please wait a minute and try again."
            if self.account_dialog:
                self.account_dialog.show_error(message)
            return
        self.authenticated = True
        self.player_id = str(data.get("id") or MY_ID)
        self.player_name = str(data.get("name") or "Player")[:32]
        self.settings._save_player_name(self.player_name)
        self.settings.set_player_name(self.player_name)
        self.settings.name_input.setReadOnly(True)
        self.settings.set_account(self.player_name, True)
        if self.account_dialog:
            self.account_dialog.create_dialog.close()
            self.account_dialog.password_recovery_dialog.close()
            self.account_dialog.close()
        self.update_status(f"Signed in as {self.player_name}")
        self._start_session()

    def handle_auth_required(self):
        self.authenticated = False
        self.settings.set_account("", False)
        self.update_status("Sign in to join a team")
        self._show_account_dialog()

    def handle_account_reset_result(self, data):
        if not self.account_dialog:
            return
        recovery_dialog = self.account_dialog.password_recovery_dialog
        if not data.get("ok"):
            message = str(data.get("message") or "Unable to reset password.")
            if data.get("reason") == "throttled":
                message = "Too many reset attempts. Please wait a minute and try again."
            recovery_dialog.show_error(message)
            self.account_dialog.set_ready(True)
            return
        recovery_dialog.close()
        self.account_dialog.password.setFocus()
        self.account_dialog.status.setText("Password reset. Sign in with your new password.")
        self.account_dialog.set_ready(True)

    def prompt_change_password(self):
        if not self.authenticated or not self.ws_client or not self.ws_client.running:
            QtWidgets.QMessageBox.warning(self.settings, "Account", "Sign in to change your password.")
            return
        current, accepted = QtWidgets.QInputDialog.getText(
            self.settings,
            "Change password",
            "Current password:",
            QtWidgets.QLineEdit.EchoMode.Password,
        )
        if not accepted:
            return
        new_password, accepted = QtWidgets.QInputDialog.getText(
            self.settings,
            "Change password",
            "New password (8–128 characters):",
            QtWidgets.QLineEdit.EchoMode.Password,
        )
        if not accepted:
            return
        confirm, accepted = QtWidgets.QInputDialog.getText(
            self.settings,
            "Change password",
            "Confirm new password:",
            QtWidgets.QLineEdit.EchoMode.Password,
        )
        if not accepted:
            return
        if new_password != confirm:
            QtWidgets.QMessageBox.warning(self.settings, "Change password", "The new passwords do not match.")
            return
        asyncio.run_coroutine_threadsafe(
            self.ws_client.send_change_password(current, new_password), self.ws_loop
        )
        self.update_status("Changing password…")

    def prompt_delete_account(self):
        if not self.authenticated or not self.ws_client or not self.ws_client.running:
            QtWidgets.QMessageBox.warning(self.settings, "Account", "Sign in to delete your account.")
            return
        choice = QtWidgets.QMessageBox.warning(
            self.settings,
            "Delete account?",
            "This permanently deletes your player account. Your current team role will be released.\n\nThis cannot be undone.",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if choice != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        password, accepted = QtWidgets.QInputDialog.getText(
            self.settings,
            "Confirm account deletion",
            "Enter your password to permanently delete this account:",
            QtWidgets.QLineEdit.EchoMode.Password,
        )
        if not accepted:
            return
        asyncio.run_coroutine_threadsafe(self.ws_client.send_delete_account(password), self.ws_loop)
        self.update_status("Deleting account…")

    def handle_account_password_result(self, data):
        if data.get("ok"):
            self.update_status("Password changed")
            QtWidgets.QMessageBox.information(self.settings, "Password changed", "Your password was changed successfully. Other sessions were signed out.")
            return
        message = str(data.get("message") or "Unable to change password.")
        if data.get("reason") == "throttled":
            message = "Too many password attempts. Please wait a minute and try again."
        self.update_status("Password change failed")
        QtWidgets.QMessageBox.warning(self.settings, "Change password", message)

    def handle_account_delete_result(self, data):
        if not data.get("ok"):
            message = str(data.get("message") or "Unable to delete account.")
            if data.get("reason") == "throttled":
                message = "Too many password attempts. Please wait a minute and try again."
            self.update_status("Account deletion failed")
            QtWidgets.QMessageBox.warning(self.settings, "Delete account", message)
            return
        QtWidgets.QMessageBox.information(self.settings, "Account deleted", "Your account was deleted and you have been signed out.")
        self._sign_out_to_account_dialog()

    def handle_account_session_ended(self, command):
        message = "Your password changed on another device. Please sign in again."
        if command == "account_deleted":
            message = "This account was deleted. Create a new account to keep playing."
        self._sign_out_to_account_dialog(message)

    def _sign_out_to_account_dialog(self, message=None):
        self.authenticated = False
        self.player_id = MY_ID
        self.player_name = ""
        self.role = None
        self.claimed_role = None
        self.pending_role = None
        self._session_started = False
        self.settings.set_account("", False)
        self.settings.set_player_name("")
        self.settings.name_input.setReadOnly(False)
        self.settings.set_role(DEFAULT_ROLE)
        self.chat.hide()
        self.chat_overlay.hide()
        self.update_status(message or "Sign in to join a team")
        self._show_account_dialog()

    def handle_role_status(self, roles):
        for role in LOCKED_ROLES:
            self.role_owners[role] = roles.get(role)
        self.settings.update_role_availability(self.role_owners, self.player_id)
        if self.role in LOCKED_ROLES and self.role_owners.get(self.role) not in (None, self.player_id):
            self.update_status(f"Role '{self.role}' is taken")

    def handle_role_result(self, role, ok):
        if role not in LOCKED_ROLES:
            return
        if ok:
            self.claimed_role = role
            self.pending_role = None
            self._set_role(role)
            self.update_status(f"Role '{role}' claimed")
        else:
            self.pending_role = None
            self.settings.set_role(self.role)
            self.update_status(f"Role '{role}' is already taken")

    def handle_join_result(self, room, ok, reason, count, capacity, can_set_password=False, locked=False):
        if not ok:
            if reason == "full":
                self.update_status(f"Room {room} is full ({capacity} max).")
            elif reason == "invalid":
                self.update_status(f"Room {room} is invalid.")
            elif reason == "password_required":
                password, accepted = QtWidgets.QInputDialog.getText(
                    self.settings,
                    f"Team {room} password",
                    "This team is private. Enter its password:",
                    QtWidgets.QLineEdit.EchoMode.Password,
                )
                if accepted:
                    self._request_room(int(room), password)
                    return
                self.update_status(f"Team {room} requires a password.")
            elif reason == "throttled":
                self.update_status(f"Too many password attempts. Try Team {room} again in a minute.")
            else:
                self.update_status(f"Room {room} join failed.")
            self.settings.set_room(self.room)
            self.pending_room = None
            return
        self.room = int(room)
        self.pending_room = None
        self.settings.set_room(self.room)
        self.chat.set_room(self.room)
        self.settings._save_last_room(self.room)
        if count is not None and capacity is not None:
            self.update_status(f"Room {self.room}: {count}/{capacity} connected")
        else:
            self.update_status(f"Room {self.room} joined")
        self.settings.set_room_privacy(can_set_password, locked)
        if self.role in LOCKED_ROLES:
            self._request_role(self.role)

    def handle_room_state(self, room, members, locked, owner_id=None):
        if int(room) == int(self.room):
            can_set_password = owner_id == self.player_id and not locked
            self.settings.update_roster(room, members, locked, can_set_password)
            capper_names = []
            for role in LOCKED_ROLES:
                member = next((item for item in members if item.get("role") == role), None)
                capper_names.append((member or {}).get("name") or role)
            self.window.set_capper_names(capper_names)

    def handle_chat_history(self, scope, messages):
        self.chat.set_history(scope, messages)

    def handle_chat_message(self, scope, message):
        self.chat.add_message(scope, message)
        if not self.compatibility_mode_enabled and not self.gameplay_overlays_hidden:
            self.chat_overlay.add_message(scope, message)

    def handle_team_directory(self, teams):
        self.settings.update_team_activity(teams)

    def handle_room_password_result(self, ok, locked):
        if ok:
            self.update_status("Team password set" if locked else "Team remains public")
            if locked:
                self.settings.set_room_privacy(False, True)
        else:
            self.update_status("Only the first player can set this team password")

    def prompt_room_password(self):
        password, accepted = QtWidgets.QInputDialog.getText(
            self.settings,
            f"Team {self.room} password",
            "Set an optional password for this team:",
            QtWidgets.QLineEdit.EchoMode.Password,
        )
        if not accepted:
            return
        if not password.strip():
            self.update_status("Password was not set; the team remains public")
            return
        self._set_room_password(password)

    def _set_role(self, role):
        if role == self.role:
            return
        if self.role in LOCKED_ROLES:
            self._release_role(self.role)
        self.role = role
        self.settings.set_role(role)
        self.settings._save_last_role(role)
        if role in ("Offense", "Defense") and self.ws_client and self.ws_client.running:
            asyncio.run_coroutine_threadsafe(
                self.ws_client.send_role_update(role), self.ws_loop
            )

    def _choose_role(self, role):
        if role in LOCKED_ROLES:
            self._request_role(role)
        elif role in ("Offense", "Defense"):
            self._set_role(role)

    def _request_role(self, role):
        if not self.ws_client or not self.ws_client.running:
            self._set_role(role)
            return
        self.pending_role = role
        asyncio.run_coroutine_threadsafe(
            self.ws_client.send_role_claim(role, self.player_id), self.ws_loop
        )

    def _request_room(self, room, password=""):
        self.pending_room = room
        if self.ws_client and self.ws_client.running:
            asyncio.run_coroutine_threadsafe(
                self.ws_client.send_join(int(room), self.player_id, self.player_name, self.role or "Player", password), self.ws_loop
            )
        else:
            self.update_status("WebSocket: waiting to join room...")

    def _set_room_password(self, password):
        if self.ws_client and self.ws_client.running:
            asyncio.run_coroutine_threadsafe(
                self.ws_client.send_room_password(password), self.ws_loop
            )

    def send_chat(self, scope, text):
        if not self.ws_client or not self.ws_client.running:
            self.update_status("Chat requires a connection to the ShazChat server")
            return
        asyncio.run_coroutine_threadsafe(
            self.ws_client.send_chat(scope, text), self.ws_loop
        )

    def _release_role(self, role):
        if self.ws_client and self.ws_client.running:
            asyncio.run_coroutine_threadsafe(
                self.ws_client.send_role_release(role, self.player_id), self.ws_loop
            )

    def trigger_timer(self, index: int):
        # cycle index => start chosen timer and broadcast if enabled
        if index == 0 and self.role != "Capper 1":
            return
        if index == 1 and self.role != "Capper 2":
            return
        print(f"Hotkey pressed for capper {index + 1}!")
        with self.lock:
            if index == 0:
                options = TIMER_OPTIONS_1
            else:
                options = TIMER_OPTIONS_2
            if not options:
                return
            self.cycle_index[index] = (self.cycle_index[index] + 1) % len(options)
            sec = options[self.cycle_index[index]]
            print(f"Emitting signal with {sec} seconds for capper {index + 1}")
            # Use Qt signal to safely call start() from background thread
            self.window.start_timer_signal.emit(index, float(sec))
            print(f"Signal emitted")

            # Send via WebSocket if connected
            if self.ws_client and self.ws_client.running:
                asyncio.run_coroutine_threadsafe(
                    self.ws_client.send_timer(sec, self.player_id, index + 1), self.ws_loop
                )
    def _start_session(self):
        if self._session_started:
            return
        self._session_started = True
        if not self.player_name:
            name, accepted = QtWidgets.QInputDialog.getText(
                self.settings,
                "Your player name",
                "Enter the name your team should see:",
            )
            if accepted and name.strip():
                self.player_name = name.strip()[:32]
                self.settings._save_player_name(self.player_name)
            else:
                self.player_name = "Player"
        self.settings.name_input.setReadOnly(bool(self.ws_client))
        selected = self.settings.prompt_role(self.role)
        if selected:
            self._choose_role(selected)
        if self.ws_client:
            selected_room = self.settings.prompt_room(self.room)
            if selected_room:
                self.room = int(selected_room)
                self._request_room(self.room)
        # Show settings window
        self.settings.load_current(
            TIMER_OPTIONS_1,
            HOTKEY_1,
            TIMER_OPTIONS_2,
            HOTKEY_2,
            CHAT_HOTKEY,
            OVERLAY_TOGGLE_HOTKEY,
            self.monitor_index,
            self.room,
            self.selected_map,
            self.role,
            self.player_name,
            self.compatibility_mode_enabled,
        )
        self.settings.show()
        self.chat.show()
        if not self.compatibility_mode_enabled and not self.gameplay_overlays_hidden:
            self.chat_overlay.show()

    def run(self):
        if not self.compatibility_mode_enabled:
            self.position_window()
        if self.ws_client:
            self._show_account_dialog()
        else:
            self._start_session()
        # The request contains no player/team information. Failures stay silent
        # at launch; a player can always use Settings > Check for updates.
        QtCore.QTimer.singleShot(1200, lambda: self.check_for_updates(manual=False))

        # Process events to ensure window is rendered
        self.app.processEvents()
        print(f"Window should be visible. Label texts: {self.window.label.texts()}")
        sys.exit(self.app.exec())

    def position_window(self):
        screens = QtWidgets.QApplication.screens()
        if not screens:
            return
        if not 0 <= self.monitor_index < len(screens):
            self.monitor_index = 0
        screen = screens[self.monitor_index].availableGeometry()
        w = WINDOW_WIDTH
        h = WINDOW_HEIGHT
        x = int(screen.x() + (screen.width() - w) / 2)
        y = int(screen.y() + screen.height() * 0.05)

        # Set the final monitor geometry before the first show/click-through.
        self.window.place_on_screen(x, y, w, h)
        self.connection_badge.set_position(x, y, w, h)

        print(f"Window positioned at ({x}, {y}) with size {w}x{h}")
        print(f"Screen size: {screen.width()}x{screen.height()}")

        if not self.gameplay_overlays_hidden and not self.compatibility_mode_enabled:
            self.window.show()
            self.window.enable_click_through_after_show()
            self.window.raise_()
            self.window.label.show()
            self.window.label.setVisible(True)
            self.window.label.resize(TIMER_WIDTH, WINDOW_HEIGHT)
            self.connection_badge.show()
        else:
            self.window.hide()
            self.connection_badge.hide()
        chat_w = self.chat.width()
        chat_h = self.chat.height()
        self.chat.setGeometry(screen.x() + 18, screen.y() + screen.height() - chat_h - 18, chat_w, chat_h)
        self.chat_overlay.set_position(screen, chat_h)


def parse_args():
    p = argparse.ArgumentParser(description="ShazChat team overlay")
    p.add_argument("--no-network", action="store_true", help="Run local-only without team sync")
    p.add_argument(
        "--server",
        default=DEFAULT_SERVER_URL,
        help="WebSocket server URL (for example, wss://capper.novatec.casa)",
    )
    p.add_argument("--room", type=int, default=DEFAULT_ROOM, help="Team/room number (1-10)")
    p.add_argument("--hotkey1", default=HOTKEY_1, help="Capper 1 hotkey (default: v)")
    p.add_argument("--hotkey2", default=HOTKEY_2, help="Capper 2 hotkey (default: b)")
    p.add_argument("--chat-hotkey", default=CHAT_HOTKEY, help="Open chat hotkey (default: enter)")
    p.add_argument("--overlay-hotkey", default=OVERLAY_TOGGLE_HOTKEY, help="Pause/resume gameplay controls (default: f10)")
    p.add_argument("--monitor", type=int, default=1, help="Monitor number (1 = primary)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    HOTKEY_1 = args.hotkey1.lower()
    HOTKEY_2 = args.hotkey2.lower()
    CHAT_HOTKEY = args.chat_hotkey.lower()
    OVERLAY_TOGGLE_HOTKEY = args.overlay_hotkey.lower()
    server_url = None if args.no_network else args.server
    app = CapTimerApp(
        network=(not args.no_network),
        server_url=server_url,
        room=args.room,
    )
    app.monitor_index = max(0, args.monitor - 1)
    app.settings.load_last_preset()
    app.run()
