"""Small, local event-candidate detector and label prototype classifier.

This is intentionally not OCR, game-memory inspection, packet inspection, or
process injection. It only receives already-captured HUD pixels.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Iterable

import numpy as np


EVENT_TYPES = (
    "kill",
    "death",
    "assist",
    "flag_grab",
    "flag_drop",
    "flag_return",
    "flag_capture",
    "score_change",
)

OBJECTIVE_EVENT_TYPES = ("flag_grab", "flag_drop", "flag_return", "flag_capture")

# Compact 8 x 40 neutral-message glyph templates from visually verified local
# 1080p feed rows. They contain no player names and are used only as a
# starting point; the reviewer can add more confirmed local examples.
OBJECTIVE_MESSAGE_SEEDS = {
    "flag_grab": (
        "AAAEAAABwAAeBGTAgYlE4BOAmgDAIAcAAgAAAwYGABGDDZQBPxwFAA==",
        "IlwQU+YiUxBf/jpfEDz2OkAA/nwAAAfgOAAAP4AAAAH8AAAAH+AAAA==",
        "AAAAAAADiB/QAAQCL4AQBRAIAEAAAAAAAAAAiIAAARCPEAAGCjxGAA==",
        "b+S+W8JvrL+YEmsqtAAMbtAAAABgAAAAAAAAAAAAAAAAAAAAAAAAAA==",
    ),
    "flag_drop": (
        "AAA1Ss9QHdVKzVz2l8vJfp73ysl//vfK+3v6lfoKe5bXwAZaveAAAA==",
        "AAAAABgMAIAAAAAAACQACgAQAAAAACAAAAAAQAgAAAGAAAAABgEGAA==",
        "A1sBOGUO00P/+w9/77/7APf3+GMBxPvJ0wAcwoYTCP4/kBM5gHIijQ==",
    ),
    "flag_capture": (
        "CAiU2AAAF0QAAAAJAAAAABcBBRAdVwUEHBCOAAAAAAAAAAAAAAAAAA==",
        "ABABUgBB/W99QFv/7X9AXu3vf0B6PP9/gH/7/36ra7vZf+J/4e//9A==",
        "EgQyPRASBTAdyBI6wCSAEgGAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==",
    ),
}


@dataclass(frozen=True)
class Candidate:
    zone: str
    change_score: float
    feature: list[float]
    suggested_type: str = "unknown"
    auto_confirm: bool = False
    objective_feature: list[float] | None = None
    auto_event_types: tuple[str, ...] = ()


def feature_from_rgb(image: np.ndarray) -> list[float]:
    """Create a compact, resolution-independent visual feature vector."""
    if image.ndim != 3 or image.shape[2] < 3:
        raise ValueError("Expected an RGB image")
    rgb = image[:, :, :3].astype(np.float32)
    gray = (0.2126 * rgb[:, :, 0]) + (0.7152 * rgb[:, :, 1]) + (0.0722 * rgb[:, :, 2])
    height, width = gray.shape
    rows = np.linspace(0, height, 9, dtype=int)
    columns = np.linspace(0, width, 9, dtype=int)
    values: list[float] = []
    for row in range(8):
        for column in range(8):
            block = gray[rows[row]:rows[row + 1], columns[column]:columns[column + 1]]
            values.append(float(block.mean() / 255.0) if block.size else 0.0)
    # Color proportions help distinguish the red/blue objective treatments.
    means = rgb.reshape(-1, 3).mean(axis=0) / 255.0
    return values + [float(value) for value in means]


class ObjectiveMessageClassifier:
    """Small template model for the fixed flag-message glyph shapes."""

    def __init__(self, confirmed_events: Iterable[dict]):
        self._templates: dict[str, list[np.ndarray]] = {
            event_type: [
                np.unpackbits(np.frombuffer(base64.b64decode(value), dtype=np.uint8)).astype(np.float32)
                for value in values
            ]
            for event_type, values in OBJECTIVE_MESSAGE_SEEDS.items()
        }
        self._manual_counts: dict[str, int] = {event_type: 0 for event_type in OBJECTIVE_EVENT_TYPES}
        for event in confirmed_events:
            labels = event.get("event_types")
            if not isinstance(labels, list) or len(labels) != 1 or labels[0] not in OBJECTIVE_EVENT_TYPES:
                continue
            feature = event.get("objective_feature")
            if not isinstance(feature, list):
                continue
            self._templates.setdefault(labels[0], []).append(np.asarray(feature, dtype=np.float32))
            self._manual_counts[labels[0]] += 1

    def predict(self, feature: list[float]) -> tuple[str, float]:
        value = np.asarray(feature, dtype=np.float32)
        distances = {
            event_type: min(float(np.mean(np.abs(value - template))) for template in templates if template.shape == value.shape)
            for event_type, templates in self._templates.items()
            if any(template.shape == value.shape for template in templates)
        }
        if not distances:
            return "unknown", 0.0
        event_type, distance = min(distances.items(), key=lambda item: item[1])
        return event_type, 1.0 - distance

    def can_auto_confirm(self, event_type: str, confidence: float) -> bool:
        """Only promote a flag type after enough local ground-truth labels."""
        ground_truth_examples = self._manual_counts.get(event_type, 0) + len(OBJECTIVE_MESSAGE_SEEDS.get(event_type, ()))
        return event_type in OBJECTIVE_EVENT_TYPES and ground_truth_examples >= 3 and confidence >= 0.93


class HudChangeDetector:
    """Find meaningful HUD changes while avoiding a candidate every frame."""

    def __init__(self, threshold: float = 10.0, cooldown_frames: int = 8):
        self.threshold = threshold
        self.cooldown_frames = cooldown_frames
        self._previous: dict[str, np.ndarray] = {}
        self._previous_feed_rgb: np.ndarray | None = None
        self._cooldowns: dict[str, int] = {}
        self._kill_frame_index = 0
        self._recent_kill_rows: list[tuple[int, np.ndarray]] = []
        self._objective_classifier = ObjectiveMessageClassifier([])

    def set_objective_examples(self, confirmed_events: Iterable[dict]) -> None:
        """Learn flag-message shapes from manually confirmed, one-label clips."""
        self._objective_classifier = ObjectiveMessageClassifier(confirmed_events)

    @staticmethod
    def _kill_feed_text_mask(image: np.ndarray) -> np.ndarray:
        """Keep likely colored player-name pixels; discard moving game scenery.

        The feed is transparent, so raw frame differencing sees the world behind
        it. Team-name colours are much more stable than the scenery. The FPS
        overlay is excluded by dropping the top of the configured crop.
        """
        rgb = image[:, :, :3]
        height = rgb.shape[0]
        rgb = rgb[int(height * 0.12):int(height * 0.78)]
        red, green, blue = (rgb[:, :, index].astype(np.int16) for index in range(3))
        red_name = (red > 150) & (red > green * 1.35) & (red > blue * 1.35)
        blue_name = (blue > 150) & (blue > green * 1.35) & (blue > red * 1.55)
        gold_name = (red > 155) & (green > 110) & (blue < 125) & (red > blue * 1.5)
        return red_name | blue_name | gold_name

    @staticmethod
    def _kill_row_signature(previous: np.ndarray, current: np.ndarray, rgb: np.ndarray) -> np.ndarray | None:
        """Recognise a changed ``name + weapon + name`` feed row.

        This deliberately does not read text. A flag notice normally has one
        coloured name followed by neutral message text; a weapon elimination
        has two separated coloured name blocks with a neutral weapon glyph in
        the gap. Requiring all three shapes keeps ordinary HUD movement and
        one-name objective notices out of the automatic kill total.
        """
        changed = np.logical_xor(previous, current).mean(axis=1) > 0.02
        row_groups = np.split(np.flatnonzero(changed), np.where(np.diff(np.flatnonzero(changed)) > 1)[0] + 1)
        for rows in row_groups:
            if not len(rows):
                continue
            center = int(round(rows.mean()))
            start, end = max(0, center - 8), min(current.shape[0], center + 9)
            band = current[start:end]
            columns = band.mean(axis=0) > 0.03
            # Character gaps are smaller than the space between player names.
            padded = np.pad(columns.astype(np.int8), (4, 4))
            joined = np.convolve(padded, np.ones(9, dtype=np.int8), mode="valid") > 0
            edges = np.diff(np.r_[False, joined, False].astype(np.int8))
            starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
            left_margin = int(current.shape[1] * 0.18)
            right_limit = int(current.shape[1] * 0.93)
            name_runs = [
                (left, right)
                for left, right in zip(starts, ends)
                if right - left >= 30 and left >= left_margin and right <= right_limit
            ]
            if len(name_runs) != 2:
                continue
            left_name, right_name = name_runs
            gap_start, gap_end = left_name[1], right_name[0]
            # Feed names are close enough to share a weapon glyph. A large
            # gap would be a coloured HUD/world artefact at an edge instead.
            if not 6 <= gap_end - gap_start <= int(current.shape[1] * 0.25):
                continue
            # A weapon glyph is bright/neutral, rather than another team-colour.
            # This rejects a pair of stacked/adjacent coloured UI elements.
            gap = rgb[start:end, gap_start:gap_end, :3].astype(np.int16)
            brightness = gap.mean(axis=2)
            saturation = gap.max(axis=2) - gap.min(axis=2)
            neutral = ((brightness > 140) & (saturation < 75)).mean() > 0.04
            if neutral:
                # This is a compact visual signature of the two name blocks.
                # It remains stable when the same feed row fades or moves.
                return band.copy()
        return None

    @classmethod
    def _is_kill_row(cls, previous: np.ndarray, current: np.ndarray, rgb: np.ndarray) -> bool:
        return cls._kill_row_signature(previous, current, rgb) is not None

    @staticmethod
    def _same_kill_row(left: np.ndarray, right: np.ndarray) -> bool:
        """Treat a persisting/moving kill-feed row as the same event."""
        height = min(left.shape[0], right.shape[0])
        width = min(left.shape[1], right.shape[1])
        left, right = left[:height, :width], right[:height, :width]
        union = np.logical_or(left, right).sum()
        if not union:
            return False
        return float(np.logical_and(left, right).sum() / union) >= 0.55

    @staticmethod
    def _objective_message_feature(
        previous: np.ndarray,
        current: np.ndarray,
        rgb: np.ndarray,
        previous_rgb: np.ndarray | None = None,
    ) -> list[float] | None:
        """Encode the fixed neutral message after a changed one-name feed row.

        It is a compact glyph-shape feature, not OCR: player names are removed
        first, then the high-contrast neutral message area is pooled into a
        fixed grid. The learned templates distinguish the four fixed flag
        messages while remaining independent of the player name.
        """
        changed = np.logical_xor(previous, current).mean(axis=1) > 0.02
        row_groups = np.split(np.flatnonzero(changed), np.where(np.diff(np.flatnonzero(changed)) > 1)[0] + 1)
        for rows in row_groups:
            if not len(rows):
                continue
            center = int(round(rows.mean()))
            start, end = max(0, center - 8), min(current.shape[0], center + 9)
            band = current[start:end]
            columns = band.mean(axis=0) > 0.03
            joined = np.convolve(np.pad(columns.astype(np.int8), (4, 4)), np.ones(9, dtype=np.int8), mode="valid") > 0
            edges = np.diff(np.r_[False, joined, False].astype(np.int8))
            starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
            left_margin = int(current.shape[1] * 0.18)
            right_limit = int(current.shape[1] * 0.93)
            names = [
                (left, right)
                for left, right in zip(starts, ends)
                if right - left >= 30 and left >= left_margin and right <= right_limit
            ]
            if len(names) != 1:
                continue
            message_start = names[0][1] + 30  # player name, flag icon, then fixed message
            message_end = min(current.shape[1] - 12, message_start + int(current.shape[1] * 0.42))
            if message_end - message_start < 32:
                continue
            patch = rgb[start:end, message_start:message_end, :3].astype(np.float32)
            gray = patch.mean(axis=2)
            saturation = patch.max(axis=2) - patch.min(axis=2)
            vertical = np.abs(np.diff(gray, axis=0, prepend=gray[:1]))
            horizontal = np.abs(np.diff(gray, axis=1, prepend=gray[:, :1]))
            changed_rgb = np.ones_like(gray, dtype=bool)
            if previous_rgb is not None and previous_rgb.shape == rgb.shape:
                before = previous_rgb[start:end, message_start:message_end, :3].astype(np.float32)
                changed_rgb = np.abs(patch - before).max(axis=2) > 18
            ink = (gray > 110) & (saturation < 100) & ((vertical + horizontal) > 35) & changed_rgb
            if ink.mean() < 0.015:
                continue
            # Isolate the longest message-shaped run, then normalise its
            # horizontal position before pooling. Player-name length therefore
            # cannot influence the classification.
            active_columns = ink.mean(axis=0) > 0.04
            joined_columns = np.convolve(
                np.pad(active_columns.astype(np.int8), (4, 4)),
                np.ones(9, dtype=np.int8),
                mode="valid",
            ) > 0
            edges = np.diff(np.r_[False, joined_columns, False].astype(np.int8))
            starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
            message_runs = [(left, right) for left, right in zip(starts, ends) if right - left >= 24]
            if not message_runs:
                continue
            left, right = max(message_runs, key=lambda item: item[1] - item[0])
            ink = ink[:, left:right]
            if ink.shape[1] < 40:
                ink = np.pad(ink, ((0, 0), (0, 40 - ink.shape[1])))
            # Pool into 8 x 40 boolean glyph cells. Max-pooling preserves thin UI text.
            row_edges = np.linspace(0, ink.shape[0], 9, dtype=int)
            column_edges = np.linspace(0, ink.shape[1], 41, dtype=int)
            return [
                float(ink[row_edges[row]:row_edges[row + 1], column_edges[column]:column_edges[column + 1]].mean() > 0.08)
                for row in range(8)
                for column in range(40)
            ]
        return None

    def inspect(self, zone: str, image: np.ndarray) -> Candidate | None:
        current = image[:, :, :3].astype(np.float32)
        if zone == "kill_feed":
            self._kill_frame_index += 1
            self._recent_kill_rows = [
                item for item in self._recent_kill_rows
                if self._kill_frame_index - item[0] <= 75
            ]
            current_mask = self._kill_feed_text_mask(image)
            height = image.shape[0]
            text_rgb = image[int(height * 0.12):int(height * 0.78), :, :3]
            previous_text_rgb = self._previous_feed_rgb
            self._previous_feed_rgb = text_rgb.copy()
            previous_mask = self._previous.get(zone)
            self._previous[zone] = current_mask
            cooldown = max(0, self._cooldowns.get(zone, 0) - 1)
            self._cooldowns[zone] = cooldown
            if previous_mask is None or previous_mask.shape != current_mask.shape or cooldown:
                return None
            # A new coloured-name row changes this sparse mask. Background and
            # the performance overlay no longer participate in this score.
            score = float(np.logical_xor(current_mask, previous_mask).mean() * 255.0)
            if score < 0.65:
                return None
            self._cooldowns[zone] = max(self.cooldown_frames, 15)
            signature = self._kill_row_signature(previous_mask, current_mask, text_rgb)
            auto_confirm = signature is not None
            if auto_confirm and any(self._same_kill_row(signature, row) for _, row in self._recent_kill_rows):
                # It is the same visible row resurfacing/fading, not a new kill.
                return None
            if auto_confirm:
                self._recent_kill_rows.append((self._kill_frame_index, signature))
            objective_feature = self._objective_message_feature(
                previous_mask,
                current_mask,
                text_rgb,
                previous_text_rgb,
            )
            objective_type = "unknown"
            objective_confidence = 0.0
            if objective_feature:
                objective_type, objective_confidence = self._objective_classifier.predict(objective_feature)
            objective_auto = self._objective_classifier.can_auto_confirm(objective_type, objective_confidence)
            auto_event_types = tuple(
                event_type
                for event_type, enabled in (("kill", auto_confirm), (objective_type, objective_auto))
                if enabled
            )
            auto_confirm = bool(auto_event_types)
            suggested = auto_event_types[0] if auto_event_types else (
                objective_type if objective_type in OBJECTIVE_EVENT_TYPES and objective_confidence >= 0.65 else "unknown"
            )
            return Candidate(
                zone=zone,
                change_score=score,
                feature=feature_from_rgb(image),
                suggested_type=suggested,
                auto_confirm=auto_confirm,
                objective_feature=objective_feature,
                auto_event_types=auto_event_types,
            )
        previous = self._previous.get(zone)
        self._previous[zone] = current
        cooldown = max(0, self._cooldowns.get(zone, 0) - 1)
        self._cooldowns[zone] = cooldown
        if previous is None or previous.shape != current.shape or cooldown:
            return None
        score = float(np.abs(current - previous).mean())
        if score < self.threshold:
            return None
        self._cooldowns[zone] = self.cooldown_frames
        return Candidate(zone=zone, change_score=score, feature=feature_from_rgb(current))


class PrototypeClassifier:
    """A transparent local baseline that learns only from confirmed labels.

    The first run has no trained labels and returns ``unknown``. As the player
    confirms examples, it compares new HUD crops against per-event prototypes.
    A future ONNX/TensorRT model can replace this class without changing the
    capture, storage, or dashboard contracts.
    """

    def __init__(self, confirmed_events: Iterable[dict]):
        grouped: dict[str, list[np.ndarray]] = {}
        for event in confirmed_events:
            # A single feed clip can legitimately contain several events. Keep
            # those review examples for recap totals, but do not make them a
            # training prototype with an ambiguous single label.
            event_types = event.get("event_types")
            if isinstance(event_types, list) and len(event_types) != 1:
                continue
            event_type = event.get("event_type")
            feature = event.get("feature")
            if event_type not in EVENT_TYPES or not isinstance(feature, list):
                continue
            grouped.setdefault(event_type, []).append(np.asarray(feature, dtype=np.float32))
        self._prototypes = {
            event_type: np.stack(features).mean(axis=0)
            for event_type, features in grouped.items()
            if features
        }

    def predict(self, feature: list[float]) -> tuple[str, float]:
        if not self._prototypes:
            return "unknown", 0.0
        value = np.asarray(feature, dtype=np.float32)
        distances = {
            event_type: float(np.mean(np.abs(value - prototype)))
            for event_type, prototype in self._prototypes.items()
            if prototype.shape == value.shape
        }
        if not distances:
            return "unknown", 0.0
        event_type, distance = min(distances.items(), key=lambda item: item[1])
        return event_type, max(0.0, min(0.99, 1.0 - (distance * 3.0)))
