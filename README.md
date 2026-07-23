# ShazChat

Team communication, role coordination, chat, and capper timer overlays for Tribes.

ShazChat is an unofficial community project and is not affiliated with or
endorsed by Hi-Rez Studios or Prophecy Games.

## Features

- **Global Hotkey**: Press `V` (configurable) to cycle through timer presets (35s → 25s → 20s)
- **Private team sync**: Connect through `wss://capper.novatec.casa`; rooms, roles, rosters, and timers are server-scoped
- **Team presence**: Player names, role assignments, and the active team roster are shared only within the selected team room
- **Optional team privacy**: The first player in an empty team can set an optional password; it is cleared automatically when everyone leaves
- **Local-only option**: Run without a server when you only need timers on one computer
- **Always-on-top Overlay**: Transparent, click-through window that won't block gameplay
- **Large Display**: Easy-to-read countdown timer

## Installation

### Client Installation

1. Install Python 3.7+ if you don't have it
2. Install client dependencies:
   ```bash
   pip install -r requirements-client.txt
   ```

### Server Installation (for local testing)

The server only needs `websockets`:
```bash
pip install websockets
```

Note: `requirements.txt` contains only the server dependencies (no Windows-specific packages).

## Usage

### Client (Overlay Timer)

Connect to the ShazChat server:
```bash
python main.py --server wss://capper.novatec.casa
```

Run local-only (no shared team data):
```bash
python main.py --no-network
```

Custom hotkey:
```bash
python main.py --server wss://capper.novatec.casa --hotkey1 f
```

### Server (novatec.casa tunnel)

1. In Cloudflare, add `capper.novatec.casa` as a public hostname on the existing tunnel and point it to `http://localhost:8765`.
2. Run `start-server-nova-casa.bat` on the host computer.
3. Players run `ShazChat.bat` or `start-timer.bat`; both use `wss://capper.novatec.casa` by default.

4. **Test locally:**
   ```bash
   python server.py
   # Then connect clients with: python main.py --server ws://localhost:8765
   ```

## How it Works

- The overlay is a frameless, translucent PyQt6 window with a large QLabel that displays the remaining seconds
- Global hotkey handling is done by the `keyboard` package; when pressed it starts/restarts the timer
- **WebSocket Mode**: The server authorizes capper timer sends, scopes timers to a team room, and broadcasts only to that room
- **Local-only Mode**: No team data is broadcast when a server is not configured
- Windows-specific click-through functionality uses `pywin32` to make the overlay non-interactive

## Requirements

- **Client**: Windows (for click-through functionality), Python 3.7+
- **Server**: Any platform, Python 3.7+
- See `requirements-client.txt` for client dependencies
- See `requirements.txt` for server dependencies

## Contributing

Issues and pull requests are welcome. Please open an issue before making a
large change, keep pull requests focused, and run the relevant tests before
submitting. See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow and
[SECURITY.md](SECURITY.md) for responsible vulnerability reporting.

## License

ShazChat is released under the [MIT License](LICENSE).

## Team Setup

### For Your Team Members:

1. **Install Python** (if not already installed)
   - Download from https://www.python.org/downloads/
   - Make sure to check "Add Python to PATH" during installation

2. **Get the Files:**
   - Download/clone this repository
   - Or just get these files:
     - `main.py`
     - `start-timer.bat`
     - `requirements-client.txt`

3. **Install Dependencies:**
   ```bash
   pip install -r requirements-client.txt
   ```

4. **Run the Timer:**
    - Double-click `ShazChat.bat` (the packaged app) or `start-timer.bat` (Python)
   - The overlay will appear with "READY" text
   - Press `V` to start timers (35s → 25s → 20s)

5. **Server URL:**
   - Default: `wss://capper.novatec.casa`

## Customization

### Change Timer Presets

Edit `main.py` and find:
```python
TIMER_OPTIONS = [35, 25, 20]  # Change these values
```

### Change Hotkey

Edit `start-timer.bat` and change:
```bash
python main.py --server %CAPTIMER_SERVER% --hotkey1 f
```
(Change `f` to any key you want)

### Change Colors

In `main.py`, find the `setStyleSheet` calls:
- Green color (normal): `color: #00FF00;`
- Red color (warning): `color: #FF0000;`
- Background: `background-color: rgba(0, 0, 0, 200);`

### Change Position

In `main.py`, find `run()` method and change:
```python
self.window.move(int((screen.width() - w) / 2), int(screen.height() * 0.05))
```
- First number: horizontal position (0 = left, screen.width = right)
- Second number: vertical position (0 = top, screen.height = bottom)

### Change Size

In `main.py`, find:
```python
self.resize(600, 200)  # width, height in pixels
self.label.setMinimumSize(600, 200)
```

### Change Font Size

In `main.py`, find:
```python
font = QtGui.QFont("Segoe UI", 72, QtGui.QFont.Bold)  # Change 72 to desired size
```
