"""Create the private Ed25519 release key outside the repository.

Run once on the release computer. It never prints the private key.
Copy the printed public-key value into updater_config.py before building the
first update-enabled installer.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


key_path = Path(os.environ.get("CAPPERTIMER_RELEASE_KEY", Path(os.environ["APPDATA"]) / "CapperTimer" / "release-signing-key.pem"))
key_path.parent.mkdir(parents=True, exist_ok=True)
if key_path.exists():
    raise SystemExit(f"Refusing to overwrite existing release key: {key_path}")

private_key = Ed25519PrivateKey.generate()
key_path.write_bytes(
    private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
)
public_key = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
print("Private key created outside the repository:", key_path)
print("Set UPDATE_PUBLIC_KEY_B64 in updater_config.py to:")
print(base64.b64encode(public_key).decode("ascii"))
