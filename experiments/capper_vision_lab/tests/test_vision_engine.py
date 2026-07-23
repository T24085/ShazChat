import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from vision_engine import HudChangeDetector, PrototypeClassifier, feature_from_rgb
from storage import MatchStore


class VisionEngineTests(unittest.TestCase):
    def test_feature_has_stable_shape(self):
        feature = feature_from_rgb(np.zeros((40, 80, 4), dtype=np.uint8))
        self.assertEqual(67, len(feature))

    def test_detector_debounces_a_changed_hud(self):
        detector = HudChangeDetector(threshold=2, cooldown_frames=2)
        blank = np.zeros((20, 20, 4), dtype=np.uint8)
        changed = np.full((20, 20, 4), 30, dtype=np.uint8)
        self.assertIsNone(detector.inspect("objective", blank))
        self.assertIsNotNone(detector.inspect("objective", changed))
        self.assertIsNone(detector.inspect("objective", blank))

    def test_kill_feed_ignores_blue_sky_but_detects_colored_name(self):
        detector = HudChangeDetector()
        sky = np.full((120, 200, 4), [95, 190, 255, 255], dtype=np.uint8)
        self.assertIsNone(detector.inspect("kill_feed", sky))
        name = sky.copy()
        name[40:52, 35:150, :3] = [0, 120, 255]
        self.assertIsNotNone(detector.inspect("kill_feed", name))

    def test_kill_feed_suggests_kill_for_two_colored_names(self):
        detector = HudChangeDetector()
        blank = np.zeros((120, 220, 4), dtype=np.uint8)
        self.assertIsNone(detector.inspect("kill_feed", blank))
        kill = blank.copy()
        kill[45:55, 45:90, :3] = [255, 0, 0]
        kill[45:55, 130:180, :3] = [0, 100, 255]
        kill[46:54, 86:104, :3] = [220, 220, 220]
        candidate = detector.inspect("kill_feed", kill)
        self.assertEqual("kill", candidate.suggested_type)
        self.assertTrue(candidate.auto_confirm)

    def test_kill_feed_does_not_auto_confirm_one_name_flag_notice(self):
        detector = HudChangeDetector()
        blank = np.zeros((120, 220, 4), dtype=np.uint8)
        self.assertIsNone(detector.inspect("kill_feed", blank))
        flag_notice = blank.copy()
        flag_notice[45:55, 20:70, :3] = [255, 0, 0]
        flag_notice[46:54, 90:112, :3] = [220, 220, 220]
        candidate = detector.inspect("kill_feed", flag_notice)
        self.assertEqual("unknown", candidate.suggested_type)
        self.assertFalse(candidate.auto_confirm)

    def test_kill_feed_rejects_a_colored_effect_at_the_right_edge(self):
        detector = HudChangeDetector()
        blank = np.zeros((120, 220, 4), dtype=np.uint8)
        self.assertIsNone(detector.inspect("kill_feed", blank))
        false_row = blank.copy()
        false_row[45:55, 50:95, :3] = [255, 0, 0]
        false_row[45:55, 205:220, :3] = [0, 100, 255]
        false_row[46:54, 120:145, :3] = [220, 220, 220]
        candidate = detector.inspect("kill_feed", false_row)
        self.assertEqual("unknown", candidate.suggested_type)
        self.assertFalse(candidate.auto_confirm)

    def test_kill_row_deduplication_matches_the_same_name_shapes(self):
        first = np.zeros((17, 220), dtype=bool)
        first[:, 50:90] = True
        first[:, 130:180] = True
        second = first.copy()
        second[:, 55:95] = second[:, 50:90]
        second[:, 50:55] = False
        self.assertTrue(HudChangeDetector._same_kill_row(first, second))

    def test_objective_message_feature_uses_one_name_and_neutral_text(self):
        previous = np.zeros((86, 220), dtype=bool)
        current = previous.copy()
        current[35:45, 45:90] = True
        rgb = np.zeros((86, 220, 3), dtype=np.uint8)
        rgb[35:45, 45:90] = [255, 0, 0]
        rgb[36:44, 130:180] = [220, 220, 220]
        feature = HudChangeDetector._objective_message_feature(previous, current, rgb)
        self.assertIsNotNone(feature)
        self.assertEqual(320, len(feature))

    def test_objective_templates_learn_only_confirmed_single_labels(self):
        detector = HudChangeDetector()
        feature = [0.0] * 320
        feature[20] = 1.0
        detector.set_objective_examples([{
            "event_type": "flag_grab",
            "event_types": ["flag_grab"],
            "objective_feature": feature,
            "review_state": "confirmed",
        }])
        event_type, confidence = detector._objective_classifier.predict(feature)
        self.assertEqual("flag_grab", event_type)
        self.assertGreater(confidence, 0.9)

    def test_objective_auto_confirm_requires_five_manual_examples(self):
        detector = HudChangeDetector()
        feature = [0.0] * 320
        feature[20] = 1.0
        examples = [{
            "event_type": "flag_return",
            "event_types": ["flag_return"],
            "objective_feature": feature,
            "review_state": "confirmed",
        }] * 5
        detector.set_objective_examples(examples)
        self.assertTrue(detector._objective_classifier.can_auto_confirm("flag_return", 0.95))

    def test_kill_feed_does_not_count_the_same_row_twice(self):
        detector = HudChangeDetector()
        blank = np.zeros((120, 220, 4), dtype=np.uint8)
        kill = blank.copy()
        kill[45:55, 45:90, :3] = [255, 0, 0]
        kill[45:55, 130:180, :3] = [0, 100, 255]
        kill[46:54, 96:118, :3] = [220, 220, 220]
        self.assertIsNone(detector.inspect("kill_feed", blank))
        self.assertTrue(detector.inspect("kill_feed", kill).auto_confirm)
        for _ in range(16):
            detector.inspect("kill_feed", kill)
        detector.inspect("kill_feed", blank)
        for _ in range(16):
            detector.inspect("kill_feed", blank)
        self.assertIsNone(detector.inspect("kill_feed", kill))

    def test_classifier_uses_confirmed_examples_only(self):
        feature = [0.5] * 67
        classifier = PrototypeClassifier([{"event_type": "flag_grab", "feature": feature, "review_state": "confirmed"}])
        event_type, confidence = classifier.predict(feature)
        self.assertEqual("flag_grab", event_type)
        self.assertGreater(confidence, 0.9)

    def test_classifier_supports_confirmed_flag_drops(self):
        feature = [0.25] * 67
        classifier = PrototypeClassifier([{"event_type": "flag_drop", "feature": feature, "review_state": "confirmed"}])
        event_type, confidence = classifier.predict(feature)
        self.assertEqual("flag_drop", event_type)
        self.assertGreater(confidence, 0.9)

    def test_classifier_skips_multi_label_clips(self):
        feature = [0.25] * 67
        classifier = PrototypeClassifier([{
            "event_type": "flag_drop",
            "event_types": ["flag_drop", "flag_grab"],
            "feature": feature,
            "review_state": "confirmed",
        }])
        self.assertEqual(("unknown", 0.0), classifier.predict(feature))

    def test_store_persists_a_multi_frame_clip(self):
        with TemporaryDirectory() as temp_dir:
            store = MatchStore(Path(temp_dir))
            match_dir = store.start_match({"name": "test"})
            event = store.add_event(
                {"event_type": "flag_grab", "review_state": "provisional"},
                b"thumbnail",
                [b"frame-0", b"frame-1", b"frame-2"],
            )
            self.assertEqual(3, event["clip_frame_count"])
            self.assertTrue((match_dir / event["clip"] / "frame-02.png").exists())

    def test_store_counts_auto_confirmed_kills_but_not_for_training(self):
        with TemporaryDirectory() as temp_dir:
            store = MatchStore(Path(temp_dir))
            store.start_match({"name": "test"})
            store.add_event(
                {"event_type": "kill", "event_types": ["kill"], "review_state": "auto_confirmed"},
                b"thumbnail",
            )
            self.assertEqual(1, len(store.counted_events()))
            self.assertEqual([], store.confirmed_events())


if __name__ == "__main__":
    unittest.main()
