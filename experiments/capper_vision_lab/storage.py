"""Local-only match storage for Capper Vision Lab."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class MatchStore:
    def __init__(self, root: Path | None = None):
        base = root or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CapperVisionLab" / "matches"
        self.root = Path(base)
        self.root.mkdir(parents=True, exist_ok=True)
        self.match_dir: Path | None = None
        self.events_file: Path | None = None

    def start_match(self, profile: dict) -> Path:
        identifier = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.match_dir = self.root / identifier
        (self.match_dir / "crops").mkdir(parents=True, exist_ok=False)
        (self.match_dir / "clips").mkdir(parents=True, exist_ok=False)
        (self.match_dir / "match.json").write_text(
            json.dumps({"started_at": _utc_now(), "profile": profile}, indent=2), encoding="utf-8"
        )
        self.events_file = self.match_dir / "events.jsonl"
        return self.match_dir

    def add_event(self, event: dict, png_bytes: bytes, clip_frames: list[bytes] | None = None) -> dict:
        if not self.match_dir or not self.events_file:
            raise RuntimeError("Start a match before adding events")
        event = dict(event)
        event["id"] = event.get("id") or uuid.uuid4().hex
        event["created_at"] = event.get("created_at") or _utc_now()
        crop_file = self.match_dir / "crops" / f"{event['id']}.png"
        crop_file.write_bytes(png_bytes)
        event["crop"] = str(crop_file.relative_to(self.match_dir))
        if clip_frames:
            clip_dir = self.match_dir / "clips" / event["id"]
            clip_dir.mkdir(parents=True, exist_ok=False)
            for index, frame in enumerate(clip_frames):
                (clip_dir / f"frame-{index:02d}.png").write_bytes(frame)
            event["clip"] = str(clip_dir.relative_to(self.match_dir))
            event["clip_frame_count"] = len(clip_frames)
        with self.events_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")
        return event

    def events(self) -> list[dict]:
        if not self.events_file or not self.events_file.exists():
            return []
        return [json.loads(line) for line in self.events_file.read_text(encoding="utf-8").splitlines() if line.strip()]

    def replace_event(self, updated: dict) -> None:
        if not self.events_file:
            raise RuntimeError("No active match")
        events = [updated if item.get("id") == updated.get("id") else item for item in self.events()]
        self.events_file.write_text("".join(json.dumps(item, separators=(",", ":")) + "\n" for item in events), encoding="utf-8")

    def confirmed_events(self) -> list[dict]:
        return [item for item in self.events() if item.get("review_state") == "confirmed"]

    def counted_events(self) -> list[dict]:
        """Events that belong in live totals.

        Manual confirmations are retained as the only training examples. A
        kill can also enter totals automatically when the detector has matched
        the stricter two-name-plus-weapon shape; those records remain visibly
        marked ``auto_confirmed`` and can still be rejected by the reviewer.
        """
        return [
            item for item in self.events()
            if item.get("review_state") in {"confirmed", "auto_confirmed"}
        ]

    def discard_active_match(self) -> None:
        if self.match_dir and self.match_dir.exists():
            shutil.rmtree(self.match_dir)
        self.match_dir = None
        self.events_file = None
