"""Focused regression tests for ShazChat timer usability controls."""

import unittest

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


if __name__ == "__main__":
    unittest.main()
