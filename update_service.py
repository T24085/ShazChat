"""Signed update-manifest verification and installer download helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from updater_config import APP_VERSION, UPDATE_DOWNLOAD_HOST, UPDATE_MANIFEST_URL, UPDATE_PUBLIC_KEY_B64

_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_INSTALLER_BYTES = 300 * 1024 * 1024


class UpdateError(RuntimeError):
    """An update endpoint or artifact did not pass verification."""


@dataclass(frozen=True)
class UpdateRelease:
    version: str
    installer_url: str
    sha256: str
    size: int
    notes: str
    published_at: str


def version_key(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch(str(value or ""))
    if not match:
        raise UpdateError("Update version is invalid.")
    return tuple(int(part) for part in match.groups())


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in manifest.items() if key != "signature"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != UPDATE_DOWNLOAD_HOST or not parsed.path:
        raise UpdateError("Update download URL is not an approved HTTPS release URL.")


def verify_manifest(manifest: dict[str, Any]) -> UpdateRelease:
    if not UPDATE_PUBLIC_KEY_B64:
        raise UpdateError("This build has no configured update signing key.")
    if not isinstance(manifest, dict):
        raise UpdateError("Update manifest is not an object.")
    required = {"version", "installer_url", "sha256", "size", "notes", "published_at", "signature"}
    if set(manifest) != required:
        raise UpdateError("Update manifest has unexpected fields.")
    version = str(manifest["version"])
    version_key(version)
    installer_url = str(manifest["installer_url"])
    _validate_url(installer_url)
    sha256 = str(manifest["sha256"]).lower()
    if not _SHA256_RE.fullmatch(sha256):
        raise UpdateError("Update manifest checksum is invalid.")
    size = manifest["size"]
    if not isinstance(size, int) or not 0 < size <= _MAX_INSTALLER_BYTES:
        raise UpdateError("Update manifest size is invalid.")
    notes = str(manifest["notes"])
    published_at = str(manifest["published_at"])
    if len(notes) > 8_000 or len(published_at) > 128:
        raise UpdateError("Update manifest text is too large.")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(UPDATE_PUBLIC_KEY_B64, validate=True))
        signature = base64.b64decode(str(manifest["signature"]), validate=True)
        public_key.verify(signature, canonical_manifest_bytes(manifest))
    except (ValueError, InvalidSignature) as exc:
        raise UpdateError("Update manifest signature is invalid.") from exc
    return UpdateRelease(version, installer_url, sha256, size, notes, published_at)


def fetch_update(timeout: float = 6.0) -> UpdateRelease:
    # Intentionally no player, team, device, or installed-version metadata.
    # Cloudflare R2 rejects Python's anonymous default user agent. This static
    # product identifier contains no player, team, device, or version data.
    request = Request(
        UPDATE_MANIFEST_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": "ShazChat Update Checker",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read(_MAX_MANIFEST_BYTES + 1)
    except Exception as exc:
        raise UpdateError(f"Unable to check for updates: {exc}") from exc
    if len(payload) > _MAX_MANIFEST_BYTES:
        raise UpdateError("Update manifest is too large.")
    try:
        manifest = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("Update manifest is not valid JSON.") from exc
    return verify_manifest(manifest)


def is_newer(release: UpdateRelease, current_version: str = APP_VERSION) -> bool:
    return version_key(release.version) > version_key(current_version)


def download_verified_installer(release: UpdateRelease, cache_dir: str | os.PathLike[str], timeout: float = 30.0) -> Path:
    _validate_url(release.installer_url)
    destination_dir = Path(cache_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"ShazChat-Setup-{release.version}.exe"
    request = Request(release.installer_url)
    hasher = hashlib.sha256()
    bytes_written = 0
    fd, temporary_name = tempfile.mkstemp(prefix="capper-times-", suffix=".download", dir=destination_dir)
    try:
        with os.fdopen(fd, "wb") as output, urlopen(request, timeout=timeout) as response:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > release.size or bytes_written > _MAX_INSTALLER_BYTES:
                    raise UpdateError("Update download exceeds the published size.")
                hasher.update(chunk)
                output.write(chunk)
        if bytes_written != release.size:
            raise UpdateError("Update download size does not match the signed manifest.")
        if hasher.hexdigest().lower() != release.sha256:
            raise UpdateError("Update download checksum does not match the signed manifest.")
        os.replace(temporary_name, destination)
        return destination
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
