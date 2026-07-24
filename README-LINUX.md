# ShazChat on Linux

ShazChat's server, chat, timers, and global hotkeys run on Linux from source.
There is not a Windows `.exe` installer for Linux.

## Quick start

```bash
git clone https://github.com/T24085/ShazChat.git
cd ShazChat
chmod +x ShazChat.sh
./ShazChat.sh
```

The launcher creates a project-local `.venv`, installs the Linux client
dependencies there (including `pynput` for global hotkeys), then starts the
app. It does not modify your distro-managed Python installation.

## Hotkey support

Global timer and chat hotkeys are supported on **X11** and generally work in
games launched through **XWayland**. A pure Wayland desktop may block global key
listeners by design. When that happens, ShazChat shows a clear status message;
switch to an X11/XWayland session or use the timer buttons in the app.

The Windows click-through treatment is Windows-specific. Linux desktop
compositors still show the timer and chat overlays, but their precise focus and
click-through behavior can vary by desktop environment.
