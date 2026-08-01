"""Focused regression tests for ShazChat timer usability controls."""

import tempfile
import unittest
from pathlib import Path

import main


class _Screen:
    def __init__(self, x, y, width, height):
        self._x, self._y, self._width, self._height = x, y, width, height

    def x(self):
        return self._x

    def y(self):
        return self._y

    def width(self):
        return self._width

    def height(self):
        return self._height


class TimerControlTests(unittest.TestCase):
    def test_preset_restarts_after_repeat_window_and_cycles_inside_it(self):
        options = [35, 25, 20]
        self.assertEqual(main.choose_timer_preset(options, -1, 0.0, 10.0, 1.5), 0)
        self.assertEqual(main.choose_timer_preset(options, 0, 10.0, 11.4, 1.5), 1)
        self.assertEqual(main.choose_timer_preset(options, 1, 10.0, 12.0, 1.5), 0)

    def test_timer_anchors_are_relative_to_the_selected_monitor(self):
        screen = _Screen(1920, 100, 1600, 900)
        self.assertEqual(main.timer_anchor_position(screen, 520, 160, "top-center"), (2460, 145))
        self.assertEqual(main.timer_anchor_position(screen, 520, 160, "bottom-center"), (2460, 822))
        self.assertEqual(main.timer_anchor_position(screen, 520, 160, "bottom-right"), (2982, 822))

    def test_update_launcher_keeps_installer_path_quoted(self):
        with tempfile.TemporaryDirectory() as directory:
            installer = r"C:\Users\Player Name\AppData\Roaming\ShazChat\updates\ShazChat-Setup.exe"
            launcher = Path(main.write_update_launcher(installer, directory))
            text = launcher.read_text(encoding="utf-8")
        self.assertIn('start "" /wait "C:\\Users\\Player Name\\AppData\\Roaming\\ShazChat\\updates\\ShazChat-Setup.exe"', text)
        self.assertIn('del "%~f0"', text)


if __name__ == "__main__":
    unittest.main()
