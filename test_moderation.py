"""Focused tests for ShazChat server-side moderation."""

import tempfile
import unittest
from pathlib import Path

from moderation import contains_blocked_term, load_blocked_terms


class ModerationTests(unittest.TestCase):
    def test_blocks_direct_and_basic_evasive_forms(self):
        self.assertTrue(contains_blocked_term("n1gg3r"))
        self.assertTrue(contains_blocked_term("s . p . i . c"))
        self.assertTrue(contains_blocked_term("goooook"))

    def test_leaves_normal_words_and_banter_alone(self):
        self.assertFalse(contains_blocked_term("spicy route is clear"))
        self.assertFalse(contains_blocked_term("good game, nice return"))

    def test_loads_optional_local_additions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blocked-words.txt"
            path.write_text("# local rules\nrouteword\n", encoding="utf-8")
            terms = load_blocked_terms(path)
        self.assertTrue(contains_blocked_term("routeword", terms))
        self.assertFalse(contains_blocked_term("routewording", terms))


if __name__ == "__main__":
    unittest.main()
