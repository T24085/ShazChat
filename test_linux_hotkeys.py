"""Simulated tests for the Linux global-hotkey adapter.

These tests do not require an X11 display. They exercise the listener lifecycle
and key matching with a small pynput-compatible fake backend.
"""

import unittest

import main


class _FakeKeyCode:
    def __init__(self, char):
        self.char = char


class _FakeListener:
    def __init__(self, on_press=None, on_release=None, on_click=None):
        self.on_press = on_press
        self.on_release = on_release
        self.on_click = on_click
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def wait(self):
        return None

    def stop(self):
        self.stopped = True


class _FakeKeyboard:
    KeyCode = _FakeKeyCode
    Listener = _FakeListener


class _FakeButton:
    def __init__(self, name):
        self.name = name


class _FakeMouse:
    Listener = _FakeListener


class LinuxHotkeyTests(unittest.TestCase):
    def test_listener_dispatches_once_per_press_and_releases_cleanly(self):
        original_platform = main.sys.platform
        original_backend = main.pynput_keyboard
        original_mouse_backend = main.pynput_mouse
        calls = []
        try:
            main.sys.platform = "linux"
            main.pynput_keyboard = _FakeKeyboard
            main.pynput_mouse = _FakeMouse
            manager = main.NativeHotkeyManager(object(), calls.append)
            ok, error = manager.register(1, "v")
            self.assertTrue(ok, error)
            listener = manager._linux_listener
            listener.on_press(_FakeKeyCode("v"))
            listener.on_press(_FakeKeyCode("v"))
            self.assertEqual(calls, [1])
            listener.on_release(_FakeKeyCode("v"))
            listener.on_press(_FakeKeyCode("v"))
            self.assertEqual(calls, [1, 1])
            ok, error = manager.register(2, "mouse4")
            self.assertTrue(ok, error)
            mouse_listener = manager._linux_mouse_listener
            mouse_listener.on_click(0, 0, _FakeButton("x1"), True)
            mouse_listener.on_click(0, 0, _FakeButton("x1"), True)
            self.assertEqual(calls, [1, 1, 2])
            mouse_listener.on_click(0, 0, _FakeButton("x1"), False)
            mouse_listener.on_click(0, 0, _FakeButton("x1"), True)
            self.assertEqual(calls, [1, 1, 2, 2])
            manager.close()
            self.assertTrue(listener.stopped)
        finally:
            main.sys.platform = original_platform
            main.pynput_keyboard = original_backend
            main.pynput_mouse = original_mouse_backend


if __name__ == "__main__":
    unittest.main()
