#!/usr/bin/env python3
"""Capper Vision Lab: opt-in, local-only experimental HUD event tracker."""

from __future__ import annotations

import sys
from collections import Counter, deque

import numpy as np
from PyQt6 import QtCore, QtGui, QtWidgets

from storage import MatchStore
from vision_engine import EVENT_TYPES, HudChangeDetector, PrototypeClassifier


CAPTURE_INTERVAL_MS = 200  # 5 FPS
PRE_EVENT_FRAMES = 5       # One second before the detected HUD change.
POST_EVENT_FRAMES = 5      # One second after the detected HUD change.
PROFILE = {"name": "TRIBES 3 1080p borderless", "width": 1920, "height": 1080}
# Relative coordinates keep the configuration explicit and make later profiles simple.
HUD_ZONES = {
    # Starts below the live FPS/GPU overlay and ends before the lower-right HUD.
    # Three feed rows fit here; ending above the lower weapon-selection HUD
    # prevents explosions, item cards, and other gameplay effects from being
    # mistaken for a second coloured player name.
    "kill_feed": (0.76, 0.040, 0.23, 0.12),
    "objective": (0.27, 0.00, 0.46, 0.20),
}


def image_to_array(image: QtGui.QImage) -> np.ndarray:
    converted = image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
    pointer = converted.bits()
    pointer.setsize(converted.sizeInBytes())
    return np.frombuffer(pointer, dtype=np.uint8).reshape(converted.height(), converted.width(), 4).copy()


def image_to_png_bytes(image: QtGui.QImage) -> bytes:
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


class VisionLab(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Capper Vision Lab — Experimental / Local Only")
        self.resize(1060, 720)
        self.store = MatchStore()
        self.detector = HudChangeDetector()
        self.classifier = PrototypeClassifier([])
        self.events: list[dict] = []
        self.frame_buffers: dict[str, deque[QtGui.QImage]] = {}
        self.pending_clips: dict[str, dict] = {}
        self.active = False
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(CAPTURE_INTERVAL_MS)
        self.timer.timeout.connect(self.capture_once)
        self._build_ui()

    def _build_ui(self):
        root = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(root)
        self.setCentralWidget(root)

        left = QtWidgets.QVBoxLayout()
        warning = QtWidgets.QLabel(
            "EXPERIMENTAL — captures only configured HUD crops locally. No game-memory access, injection, OCR, "
            "packet inspection, uploads, or team sync. Use private/non-competitive testing only."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("background:#3a2511;color:#ffe0a3;padding:10px;border-radius:6px;")
        left.addWidget(warning)
        self.status = QtWidgets.QLabel("Ready. Start a local match when TRIBES is 1920×1080 borderless.")
        self.status.setWordWrap(True)
        left.addWidget(self.status)
        controls = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton("Start local match")
        self.start_button.clicked.connect(self.start_match)
        self.stop_button = QtWidgets.QPushButton("Stop capture")
        self.stop_button.clicked.connect(self.stop_match)
        self.stop_button.setEnabled(False)
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        left.addLayout(controls)

        self.preview = QtWidgets.QLabel("Candidate HUD crop will appear here")
        self.preview.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(620, 340)
        self.preview.setStyleSheet("background:#111;color:#aab;border:1px solid #445;")
        left.addWidget(self.preview, 1)
        self.candidate_label = QtWidgets.QLabel("No candidate selected")
        left.addWidget(self.candidate_label)
        review = QtWidgets.QHBoxLayout()
        self.event_type = QtWidgets.QComboBox()
        self.event_type.addItems(EVENT_TYPES)
        self.player_name = QtWidgets.QLineEdit()
        self.player_name.setPlaceholderText("Player name (optional)")
        self.review_hint = QtWidgets.QLabel("Choose the event type, then save a confirmed label. Use Reject only for non-events.")
        self.review_hint.setWordWrap(True)
        left.addWidget(self.review_hint)
        confirm = QtWidgets.QPushButton("Add confirmed label")
        confirm.clicked.connect(self.confirm_selected)
        reject = QtWidgets.QPushButton("Reject selected")
        reject.clicked.connect(self.reject_selected)
        review.addWidget(self.event_type)
        review.addWidget(self.player_name)
        review.addWidget(confirm)
        review.addWidget(reject)
        left.addLayout(review)
        layout.addLayout(left, 3)

        right = QtWidgets.QVBoxLayout()
        right.addWidget(QtWidgets.QLabel("Live match dashboard"))
        self.live_activity = QtWidgets.QTableWidget(2, 2)
        self.live_activity.setHorizontalHeaderLabels(["Live activity", "Candidates"])
        self.live_activity.verticalHeader().setVisible(False)
        self.live_activity.horizontalHeader().setStretchLastSection(True)
        for row, zone in enumerate(("kill_feed", "objective")):
            self.live_activity.setItem(row, 0, QtWidgets.QTableWidgetItem(zone.replace("_", " ").title()))
            self.live_activity.setItem(row, 1, QtWidgets.QTableWidgetItem("0"))
        right.addWidget(self.live_activity)
        self.chart = QtWidgets.QLabel("Match event chart appears as events are reviewed or high-confidence feed events are detected.")
        self.chart.setWordWrap(True)
        self.chart.setStyleSheet("background:#101923;color:#a9d7ff;padding:8px;border:1px solid #35506e;")
        right.addWidget(self.chart)
        right.addWidget(QtWidgets.QLabel("Live event timeline"))
        self.timeline = QtWidgets.QListWidget()
        self.timeline.setMaximumHeight(120)
        right.addWidget(self.timeline)
        right.addWidget(QtWidgets.QLabel("Candidate events"))
        self.event_list = QtWidgets.QListWidget()
        self.event_list.currentRowChanged.connect(self.select_event)
        right.addWidget(self.event_list, 3)
        right.addWidget(QtWidgets.QLabel("Match totals (reviewed + high-confidence feed events)"))
        self.totals = QtWidgets.QTableWidget(len(EVENT_TYPES), 2)
        self.totals.setHorizontalHeaderLabels(["Event", "Confirmed"])
        self.totals.verticalHeader().setVisible(False)
        self.totals.horizontalHeader().setStretchLastSection(True)
        for row, event_type in enumerate(EVENT_TYPES):
            self.totals.setItem(row, 0, QtWidgets.QTableWidgetItem(event_type.replace("_", " ").title()))
            self.totals.setItem(row, 1, QtWidgets.QTableWidgetItem("0"))
        right.addWidget(self.totals, 2)
        right.addWidget(QtWidgets.QLabel("Confirmed player stats"))
        self.player_totals = QtWidgets.QTableWidget(0, 4)
        self.player_totals.setHorizontalHeaderLabels(["Player", "Kills", "Deaths", "Objectives"])
        self.player_totals.verticalHeader().setVisible(False)
        self.player_totals.horizontalHeader().setStretchLastSection(True)
        self.player_totals.setMaximumHeight(150)
        right.addWidget(self.player_totals)
        self.path_label = QtWidgets.QLabel("Local data path appears after starting.")
        self.path_label.setWordWrap(True)
        right.addWidget(self.path_label)
        layout.addLayout(right, 2)

    def start_match(self):
        screen = QtGui.QGuiApplication.primaryScreen()
        geometry = screen.geometry()
        if geometry.width() != PROFILE["width"] or geometry.height() != PROFILE["height"]:
            QtWidgets.QMessageBox.warning(
                self,
                "1080p profile required",
                "Vision Lab v1 is calibrated for a 1920×1080 primary display. Switch to 1080p borderless before starting.",
            )
            return
        match_dir = self.store.start_match({**PROFILE, "zones": HUD_ZONES, "sample_interval_ms": CAPTURE_INTERVAL_MS})
        self.events = []
        self.event_list.clear()
        self.timeline.clear()
        self.detector = HudChangeDetector()
        self.classifier = PrototypeClassifier([])
        self.active = True
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.frame_buffers = {zone: deque(maxlen=PRE_EVENT_FRAMES) for zone in HUD_ZONES}
        self.pending_clips = {}
        self.refresh_dashboard()
        self.path_label.setText(f"HUD-only local clips: {match_dir}")
        self.status.setText("Capturing two-second HUD clips at 5 FPS. Only validated high-confidence feed events enter totals automatically.")
        self.timer.start()

    def stop_match(self):
        self.timer.stop()
        self.active = False
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        unfinished = len(self.pending_clips)
        self.pending_clips = {}
        suffix = f" {unfinished} incomplete end-of-match clip(s) were discarded." if unfinished else ""
        self.status.setText(f"Capture stopped. Confirmed recap saved locally in {self.store.match_dir}.{suffix}")

    def capture_once(self):
        if not self.active:
            return
        screen = QtGui.QGuiApplication.primaryScreen()
        geometry = screen.geometry()
        for zone, (x, y, width, height) in HUD_ZONES.items():
            crop = screen.grabWindow(
                0,
                int(geometry.x() + geometry.width() * x),
                int(geometry.y() + geometry.height() * y),
                int(geometry.width() * width),
                int(geometry.height() * height),
            ).toImage()
            if crop.isNull():
                continue
            # Copy the Qt-owned pixels so the pre/post event sequence stays stable.
            self.frame_buffers.setdefault(zone, deque(maxlen=PRE_EVENT_FRAMES)).append(crop.copy())
            self.append_post_event_frame(zone, crop)
            candidate = self.detector.inspect(zone, image_to_array(crop))
            if candidate and zone not in self.pending_clips:
                self.begin_candidate_clip(
                    candidate.zone,
                    candidate.change_score,
                    candidate.feature,
                    candidate.suggested_type,
                    candidate.auto_confirm,
                    candidate.objective_feature,
                    candidate.auto_event_types,
                )

    def begin_candidate_clip(
        self,
        zone,
        change_score,
        feature,
        suggested_type="unknown",
        auto_confirm=False,
        objective_feature=None,
        auto_event_types=(),
    ):
        frames = list(self.frame_buffers.get(zone, ()))
        if len(frames) < PRE_EVENT_FRAMES:
            return
        self.pending_clips[zone] = {
            "frames": frames,
            "remaining_post_frames": POST_EVENT_FRAMES,
            "change_score": change_score,
            "feature": feature,
            "suggested_type": suggested_type,
            "auto_confirm": auto_confirm,
            "objective_feature": objective_feature,
            "auto_event_types": list(auto_event_types),
        }
        self.status.setText(f"HUD change detected in {zone}; recording one second of post-event context.")

    def append_post_event_frame(self, zone, crop):
        pending = self.pending_clips.get(zone)
        if not pending:
            return
        pending["frames"].append(crop.copy())
        pending["remaining_post_frames"] -= 1
        if pending["remaining_post_frames"] <= 0:
            self.pending_clips.pop(zone, None)
            self.add_candidate_clip(zone, pending)

    def add_candidate_clip(self, zone, pending):
        frames = pending["frames"]
        if len(frames) != PRE_EVENT_FRAMES + POST_EVENT_FRAMES:
            return
        change_score = pending["change_score"]
        feature = pending["feature"]
        predicted, confidence = self.classifier.predict(feature)
        auto_event_types = list(pending.get("auto_event_types", ()))
        if auto_event_types:
            predicted, confidence = auto_event_types[0], 0.98
        auto_confirm = bool(pending.get("auto_confirm") and auto_event_types)
        event = self.store.add_event(
            {
                "zone": zone,
                "event_type": predicted,
                "event_types": auto_event_types if auto_confirm else [],
                "confidence": round(confidence, 3),
                "change_score": round(change_score, 3),
                "feature": feature,
                "objective_feature": pending.get("objective_feature"),
                "review_state": "auto_confirmed" if auto_confirm else "provisional",
            },
            image_to_png_bytes(frames[PRE_EVENT_FRAMES - 1]),
            [image_to_png_bytes(frame) for frame in frames],
        )
        self.events.append(event)
        item = QtWidgets.QListWidgetItem(self.event_text(event))
        item.setData(QtCore.Qt.ItemDataRole.UserRole, event["id"])
        self.event_list.addItem(item)
        self.event_list.setCurrentItem(item)
        state = (
            f"auto-confirmed {', '.join(item.replace('_', ' ') for item in auto_event_types)}"
            if auto_confirm else f"provisional {zone.replace('_', ' ')} change"
        )
        self.timeline.insertItem(0, f"{event['created_at'][11:19]} · {state}")
        while self.timeline.count() > 12:
            self.timeline.takeItem(self.timeline.count() - 1)
        self.refresh_dashboard()
        if auto_confirm:
            self.status.setText("High-confidence feed event confirmed automatically. Reject it if the saved clip shows otherwise.")
        else:
            self.status.setText(f"Two-second {zone} clip saved. Label it as a gameplay event or reject it before it affects totals.")

    @staticmethod
    def event_text(event):
        labels = event.get("event_types") or [event.get("event_type", "unknown")]
        label = ", ".join(str(item).replace("_", " ") for item in labels)
        return f"{event.get('review_state', 'provisional').upper()} · {label} · {event.get('zone')} · {event.get('confidence', 0):.0%}"

    def selected_event(self):
        row = self.event_list.currentRow()
        return self.events[row] if 0 <= row < len(self.events) else None

    def select_event(self, row):
        event = self.selected_event()
        if not event or not self.store.match_dir:
            return
        image = QtGui.QImage(str(self.store.match_dir / event["crop"]))
        self.preview.setPixmap(QtGui.QPixmap.fromImage(image).scaled(
            self.preview.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation
        ))
        self.candidate_label.setText(
            f"{event['zone']} clip ({event.get('clip_frame_count', 1)} frames) · change {event['change_score']:.1f} · "
            f"predicted {event['event_type']} at {event['confidence']:.0%} confidence · "
            f"confirmed labels: {', '.join(event.get('event_types', [])) or 'none'}"
        )
        labels = event.get("event_types") or [event.get("event_type")]
        if labels[-1] in EVENT_TYPES:
            self.event_type.setCurrentText(labels[-1])

    def confirm_selected(self):
        self.review_selected("confirmed")

    def reject_selected(self):
        self.review_selected("rejected")

    def review_selected(self, state):
        event = self.selected_event()
        row = self.event_list.currentRow()
        if not event or row < 0:
            return
        if state == "confirmed":
            event_types = list(event.get("event_types", []))
            selected_type = self.event_type.currentText()
            if selected_type not in event_types:
                event_types.append(selected_type)
            event["event_types"] = event_types
            # Retained for compatibility with existing local JSONL records.
            event["event_type"] = selected_type
            event["player"] = self.player_name.text().strip()
        else:
            event["event_types"] = []
        event["review_state"] = state
        self.store.replace_event(event)
        self.event_list.item(row).setText(self.event_text(event))
        self.classifier = PrototypeClassifier(self.store.confirmed_events())
        self.detector.set_objective_examples(self.store.confirmed_events())
        self.refresh_totals()
        self.refresh_dashboard()

    def refresh_totals(self):
        totals = Counter(
            event_type
            for event in self.store.counted_events()
            for event_type in (event.get("event_types") or [event["event_type"]])
        )
        for row, event_type in enumerate(EVENT_TYPES):
            self.totals.item(row, 1).setText(str(totals[event_type]))

    def refresh_dashboard(self):
        activity = Counter(event.get("zone") for event in self.events)
        for row, zone in enumerate(("kill_feed", "objective")):
            self.live_activity.item(row, 1).setText(str(activity[zone]))
        confirmed = self.store.counted_events() if self.store.match_dir else []
        totals = Counter(
            event_type
            for event in confirmed
            for event_type in (event.get("event_types") or [event.get("event_type")])
        )
        chart_types = ("kill", "death", "assist", "flag_grab", "flag_drop", "flag_capture", "flag_return")
        chart_values = [totals[event_type] for event_type in chart_types]
        maximum = max(chart_values, default=0, ) or 1
        bars = " | ".join(f"{event_type.replace('_', ' ')} {'█' * round((totals[event_type] / maximum) * 10)} {totals[event_type]}" for event_type in chart_types)
        self.chart.setText(
            f"Match event chart (reviewed + high-confidence feed events)\n{bars}\n"
            "Flag return learns from five confirmed local examples before it can auto-count."
        )
        players: dict[str, Counter] = {}
        for event in confirmed:
            player = event.get("player") or "Unassigned"
            bucket = players.setdefault(player, Counter())
            for event_type in (event.get("event_types") or [event.get("event_type")]):
                bucket[event_type] += 1
        self.player_totals.setRowCount(len(players))
        for row, (player, counts) in enumerate(sorted(players.items())):
            self.player_totals.setItem(row, 0, QtWidgets.QTableWidgetItem(player))
            self.player_totals.setItem(row, 1, QtWidgets.QTableWidgetItem(str(counts["kill"])))
            self.player_totals.setItem(row, 2, QtWidgets.QTableWidgetItem(str(counts["death"])))
            objectives = sum(counts[event_type] for event_type in ("flag_grab", "flag_drop", "flag_return", "flag_capture", "score_change"))
            self.player_totals.setItem(row, 3, QtWidgets.QTableWidgetItem(str(objectives)))


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Capper Vision Lab")
    window = VisionLab()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
