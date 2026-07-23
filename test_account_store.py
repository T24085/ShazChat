import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import account_store
from account_store import (
    AccountError,
    authenticate_account,
    change_password,
    create_account,
    delete_account,
    recover_password,
    reset_password,
)


class AccountStoreTests(unittest.TestCase):
    def test_create_authenticate_and_reset(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(account_store, "DATA_DIR", Path(directory)), patch.object(account_store, "ACCOUNTS_FILE", Path(directory) / "accounts.json"):
            created = create_account("Panda_Test", "password123", "1234")
            self.assertEqual(created["name"], "Panda_Test")
            self.assertEqual(authenticate_account("panda_test", "wrongpassword"), None)
            self.assertEqual(authenticate_account("PANDA_TEST", "password123")["id"], created["id"])
            with self.assertRaises(AccountError):
                create_account("panda_test", "anotherpass", "5678")
            reset_password("Panda_Test", "newpassword123")
            self.assertIsNone(authenticate_account("Panda_Test", "password123"))
            self.assertEqual(authenticate_account("Panda_Test", "newpassword123")["name"], "Panda_Test")

    def test_rejects_invalid_names_and_short_passwords(self):
        with self.assertRaises(AccountError):
            create_account("bad name", "password123", "1234")
        with self.assertRaises(AccountError):
            create_account("Panda", "short", "1234")
        with self.assertRaises(AccountError):
            create_account("Panda", "password123", "not-a-pin")

    def test_change_and_delete_account(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(account_store, "DATA_DIR", Path(directory)), patch.object(account_store, "ACCOUNTS_FILE", Path(directory) / "accounts.json"):
            create_account("Panda", "password123", "1234")
            with self.assertRaises(AccountError):
                change_password("Panda", "incorrect", "newpassword123")
            changed = change_password("Panda", "password123", "newpassword123")
            self.assertEqual(changed["name"], "Panda")
            self.assertIsNone(authenticate_account("Panda", "password123"))
            self.assertIsNotNone(authenticate_account("Panda", "newpassword123"))
            with self.assertRaises(AccountError):
                delete_account("Panda", "incorrect")
            deleted = delete_account("Panda", "newpassword123")
            self.assertEqual(deleted["id"], "account:panda")
            self.assertIsNone(authenticate_account("Panda", "newpassword123"))

    def test_recover_password_with_pin(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(account_store, "DATA_DIR", Path(directory)), patch.object(account_store, "ACCOUNTS_FILE", Path(directory) / "accounts.json"):
            create_account("Panda", "password123", "2468")
            with self.assertRaises(AccountError):
                recover_password("Panda", "1111", "newpassword123")
            recovered = recover_password("Panda", "2468", "newpassword123")
            self.assertEqual(recovered["name"], "Panda")
            self.assertIsNone(authenticate_account("Panda", "password123"))
            self.assertIsNotNone(authenticate_account("Panda", "newpassword123"))


if __name__ == "__main__":
    unittest.main()
