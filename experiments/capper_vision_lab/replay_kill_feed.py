#!/usr/bin/env python3
"""Replay saved PNG kill-feed frames through the local detector.

This diagnostic is for validating opt-in saved clips. It accepts temporary
download URLs (for example, the raw URLs returned by Google Drive) and never
contacts the game or reads game memory.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from urllib.request import urlopen

import numpy as np
from PIL import Image

from vision_engine import HudChangeDetector, OBJECTIVE_EVENT_TYPES


def load_rgb(source: str) -> np.ndarray:
    if source.startswith(("https://", "http://")):
        with urlopen(source, timeout=30) as response:
            data = response.read()
    else:
        data = Path(source).read_bytes()
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGBA"))


def replay_frames(
    sources: list[str],
    vertical_window: tuple[float, float] = (0.0, 1.0),
    detector: HudChangeDetector | None = None,
) -> list:
    """Return every candidate detected while replaying one ordered clip."""
    detector = detector or HudChangeDetector()
    candidates = []
    for index, source in enumerate(sources):
        image = load_rgb(source)
        top = int(image.shape[0] * vertical_window[0])
        bottom = int(image.shape[0] * vertical_window[1])
        candidate = detector.inspect("kill_feed", image[top:bottom])
        if candidate:
            candidates.append((index, candidate))
    return candidates


def replay_match(
    match_dir: Path,
    offset: int = 0,
    limit: int | None = None,
    vertical_window: tuple[float, float] = (0.0, 1.0),
) -> int:
    events_file = match_dir / "events.jsonl"
    events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    reviewed = 0
    automatic = []
    objectives = []
    objective_features = []
    detector = HudChangeDetector()
    kill_events = [event for event in events if event.get("zone") == "kill_feed" and event.get("clip")]
    selected = kill_events[offset:offset + limit if limit else None]
    for event in selected:
        frames = sorted((match_dir / event["clip"]).glob("frame-*.png"))
        if len(frames) < 2:
            continue
        reviewed += 1
        for frame_index, candidate in replay_frames([str(frame) for frame in frames], vertical_window, detector):
            if "kill" in candidate.auto_event_types:
                automatic.append((event["id"], frame_index, candidate.change_score))
            for event_type in candidate.auto_event_types:
                if event_type in OBJECTIVE_EVENT_TYPES:
                    objectives.append((event["id"], frame_index, event_type, candidate.change_score))
            if candidate.objective_feature and not any(item in OBJECTIVE_EVENT_TYPES for item in candidate.auto_event_types):
                event_type, confidence = detector._objective_classifier.predict(candidate.objective_feature)
                objective_features.append((event["id"], frame_index, event_type, confidence, candidate.change_score))
    for event_id, frame_index, score in automatic:
        print(f"event={event_id} frame={frame_index:02d} score={score:.3f} auto_confirm=True")
    for event_id, frame_index, event_type, score in objectives:
        print(f"event={event_id} frame={frame_index:02d} score={score:.3f} suggested={event_type}")
    for event_id, frame_index, event_type, confidence, score in objective_features:
        print(f"event={event_id} frame={frame_index:02d} score={score:.3f} raw={event_type}:{confidence:.3f}")
    print(
        f"kill-feed clips={reviewed}/{len(kill_events)} auto-confirmed kills={len(automatic)} "
        f"objective suggestions={len(objectives)} raw-objective-rows={len(objective_features)}"
    )
    return 0


def main(urls: list[str]) -> int:
    if len(urls) < 2:
        raise SystemExit("Pass at least two raw PNG URLs in frame order.")
    candidates = replay_frames(urls)
    for index, candidate in candidates:
        if candidate:
            print(
                f"frame={index:02d} score={candidate.change_score:.3f} "
                f"type={candidate.suggested_type} auto_confirm={candidate.auto_confirm}"
            )
    print(f"kill-feed candidates={len(candidates)}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("sources", nargs="*")
    parser.add_argument("--match", type=Path)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--vertical-window",
        type=float,
        nargs=2,
        metavar=("TOP", "BOTTOM"),
        default=(0.0, 1.0),
        help="Keep this fraction of each legacy frame before replaying it.",
    )
    args = parser.parse_args()
    window = tuple(args.vertical_window)
    raise SystemExit(replay_match(args.match, args.offset, args.limit, window) if args.match else main(args.sources))
