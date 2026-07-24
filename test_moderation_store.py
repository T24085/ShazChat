import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import account_store
import moderation_store


class ModerationStoreTests(unittest.TestCase):
    def test_ban_mute_and_compact_log(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with (
                patch.object(account_store, "DATA_DIR", base),
                patch.object(account_store, "ACCOUNTS_FILE", base / "accounts.json"),
                patch.object(moderation_store, "DATA_DIR", base),
                patch.object(moderation_store, "MODERATION_FILE", base / "moderation.json"),
                patch.object(moderation_store, "CHAT_LOG_FILE", base / "chat-log.json"),
            ):
                banned = moderation_store.ban_player("Panda_Test", "spam")
                self.assertEqual(banned["reason"], "spam")
                self.assertEqual(moderation_store.ban_status("panda_test")["name"], "Panda_Test")
                self.assertTrue(moderation_store.unban_player("Panda_Test"))
                self.assertIsNone(moderation_store.ban_status("Panda_Test"))

                muted = moderation_store.mute_player("Panda_Test", 1, "cooldown")
                self.assertIsNotNone(muted["expires_at"])
                self.assertEqual(moderation_store.mute_status("Panda_Test")["reason"], "cooldown")
                self.assertTrue(moderation_store.unmute_player("Panda_Test"))

                for number in range(55):
                    moderation_store.append_chat_log({"scope": "global", "name": "Panda_Test", "text": str(number), "timestamp": "10:00"})
                logs = moderation_store.chat_logs()
                self.assertEqual(len(logs), 50)
                self.assertEqual(logs[0]["text"], "5")

    def test_expired_mute_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with (
                patch.object(moderation_store, "DATA_DIR", base),
                patch.object(moderation_store, "MODERATION_FILE", base / "moderation.json"),
                patch.object(moderation_store, "CHAT_LOG_FILE", base / "chat-log.json"),
            ):
                moderation_store.mute_player("Panda_Test", 0)
                state = moderation_store._state()
                state["mutes"]["panda_test"]["expires_at"] = int(time.time()) - 1
                moderation_store._save(moderation_store.MODERATION_FILE, state)
                self.assertIsNone(moderation_store.mute_status("Panda_Test"))


if __name__ == "__main__":
    unittest.main()
