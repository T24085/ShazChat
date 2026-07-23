"""Build a signed stable.json manifest for an already-built installer.

This deliberately does not upload to Cloudflare: the release operator uploads
the immutable installer first, then this manifest last through the R2 dashboard
or an account-scoped deployment tool.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from update_service import canonical_manifest_bytes, version_key
from updater_config import UPDATE_DOWNLOAD_HOST


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a signed ShazChat update manifest")
    parser.add_argument("--version", required=True)
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--notes", required=True, help="Plain-text release notes")
    parser.add_argument("--out", type=Path, default=Path("release/stable.json"))
    parser.add_argument("--key", type=Path, default=Path(os.environ.get("CAPPERTIMER_RELEASE_KEY", Path(os.environ["APPDATA"]) / "CapperTimer" / "release-signing-key.pem")))
    args = parser.parse_args()

    version_key(args.version)
    if not args.installer.is_file():
        raise SystemExit(f"Installer not found: {args.installer}")
    if not args.key.is_file():
        raise SystemExit(f"Release signing key not found: {args.key}. Run tools/create_update_signing_key.py once.")

    artifact = args.installer.read_bytes()
    manifest = {
        "version": args.version,
        "installer_url": f"https://{UPDATE_DOWNLOAD_HOST}/capper-times/releases/{args.version}/ShazChat-Setup.exe",
        "sha256": hashlib.sha256(artifact).hexdigest(),
        "size": len(artifact),
        "notes": args.notes,
        "published_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    private_key = serialization.load_pem_private_key(args.key.read_bytes(), password=None)
    manifest["signature"] = base64.b64encode(private_key.sign(canonical_manifest_bytes(manifest))).decode("ascii")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Signed manifest written: {args.out}")
    print("Upload the installer first, verify its R2 URL, then upload this file as capper-times/stable.json.")


if __name__ == "__main__":
    main()
