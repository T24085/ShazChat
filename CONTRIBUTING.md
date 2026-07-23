# Contributing to ShazChat

Thanks for helping improve ShazChat.

## Before you start

- Open an issue first for substantial features or behavior changes.
- Keep a pull request focused on one change.
- Do not commit account data, release keys, Cloudflare tokens, installers, or
  generated build folders.

## Development checks

Run the focused checks that cover your change before opening a pull request:

```powershell
python -m unittest test_account_store.py test_update_service.py
python -m py_compile main.py server.py account_store.py update_service.py
```

For UI changes, also test the Windows app manually with a game or a second
monitor when relevant. Do not use public-player traffic for automated testing.

## Pull requests

Explain what changed, why it changed, and how you tested it. Screenshots are
helpful for overlay, chat, and settings-window changes.
