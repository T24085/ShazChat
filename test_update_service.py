import base64
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization

import update_service
from update_service import UpdateError, UpdateRelease, canonical_manifest_bytes, download_verified_installer, fetch_update, is_newer, verify_manifest
from updater_config import UPDATE_PUBLIC_KEY_B64


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.position = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        if size < 0:
            size = len(self.payload) - self.position
        chunk = self.payload[self.position:self.position + size]
        self.position += len(chunk)
        return chunk


class UpdateServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        key_path = Path(os.environ["APPDATA"]) / "CapperTimer" / "release-signing-key.pem"
        cls.private_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        public_raw = cls.private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        if base64.b64encode(public_raw).decode("ascii") != UPDATE_PUBLIC_KEY_B64:
            raise RuntimeError("Configured update public key does not match the local release key.")

    def signed_manifest(self):
        manifest = {
            "version": "1.1.1",
            "installer_url": "https://downloads.novatec.casa/capper-times/releases/1.1.1/ShazChat-Setup.exe",
            "sha256": "a" * 64,
            "size": 123,
            "notes": "Test release",
            "published_at": "2026-07-23T00:00:00Z",
        }
        manifest["signature"] = base64.b64encode(self.private_key.sign(canonical_manifest_bytes(manifest))).decode("ascii")
        return manifest

    def test_signed_manifest_and_version_comparison(self):
        release = verify_manifest(self.signed_manifest())
        self.assertEqual(release.version, "1.1.1")
        self.assertTrue(is_newer(release, "1.1.0"))
        self.assertFalse(is_newer(release, "1.1.1"))

    def test_rejects_tampered_manifest_and_bad_url(self):
        manifest = self.signed_manifest()
        manifest["notes"] = "Tampered"
        with self.assertRaises(UpdateError):
            verify_manifest(manifest)
        manifest = self.signed_manifest()
        manifest["installer_url"] = "https://example.com/ShazChat-Setup.exe"
        with self.assertRaises(UpdateError):
            verify_manifest(manifest)

    def test_rejects_malformed_manifest(self):
        with self.assertRaises(UpdateError):
            verify_manifest({"version": "1.1.1"})
        with self.assertRaises(UpdateError):
            update_service.version_key("latest")

    def test_manifest_request_identifies_shazchat_without_player_metadata(self):
        payload = json.dumps(self.signed_manifest()).encode("utf-8")
        with patch.object(update_service, "urlopen", return_value=FakeResponse(payload)) as mocked_open:
            fetch_update()
        request = mocked_open.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), "ShazChat Update Checker")
        self.assertEqual(request.get_header("Accept"), "application/json")

    def test_verified_download_and_checksum_rejection(self):
        payload = b"verified-installer-content"
        release = UpdateRelease(
            "1.1.1",
            "https://downloads.novatec.casa/capper-times/releases/1.1.1/ShazChat-Setup.exe",
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            "Test release",
            "2026-07-23T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as cache, patch.object(update_service, "urlopen", return_value=FakeResponse(payload)):
            installer = download_verified_installer(release, cache)
            self.assertEqual(installer.read_bytes(), payload)
        bad_release = UpdateRelease(release.version, release.installer_url, "0" * 64, release.size, release.notes, release.published_at)
        with tempfile.TemporaryDirectory() as cache, patch.object(update_service, "urlopen", return_value=FakeResponse(payload)):
            with self.assertRaises(UpdateError):
                download_verified_installer(bad_release, cache)
            self.assertEqual(list(Path(cache).glob("*.download")), [])


if __name__ == "__main__":
    unittest.main()
