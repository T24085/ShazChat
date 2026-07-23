# Capper Vision Lab

An opt-in, local-only experiment for turning visible TRIBES 3 HUD changes into reviewable match events. It is not part of ShazChat production, does not communicate with the production server, and is not included in the installer.

## What it does today

- Requires a 1920×1080 primary display and borderless gameplay.
- Captures only two small HUD zones at 5 FPS: the top three kill-feed rows and objective score.
- Detects visual HUD changes and saves a 10-frame, two-second sequence for each candidate: one second before the change and one second after it.
- Automatically counts only validated high-confidence feed events: weapon eliminations use two coloured player-name blocks with a neutral weapon glyph between them; the verified `Took`, `Dropped`, and `Captured the Flag` message shapes use one name plus a fixed neutral message. The detector deduplicates a persisting kill row so it cannot count the same elimination again as it fades or shifts. `Returned the Flag` stays reviewable until five local examples have been confirmed, then it follows the same conservative auto-count gate. Every auto-confirmed event remains visible in the review list and can be rejected.
- Lets the player add one or more confirmed labels to each clip: kill, death, assist, flag grab, flag drop, flag return, flag capture, or score change. This matters because one kill-feed clip can show multiple real events. Reject is reserved for non-events.
- Builds a transparent local prototype classifier from confirmed examples. It is intentionally a baseline until a trained model is available.

## Safety and privacy

- No OCR, process attachment, game-memory access, injection, packet inspection, network upload, or team synchronization.
- All candidate crops, per-event frame sequences, and match JSONL files stay under `%LOCALAPPDATA%\CapperVisionLab\matches`.
- Use only in private/non-competitive test matches until the project has been independently validated against the game's policies.

## Run

From the repository root, double-click `start-capper-vision-lab.bat`, or run:

```powershell
python experiments/capper_vision_lab/vision_lab.py
```

This experiment needs only the existing `PyQt6` and `numpy` packages.
