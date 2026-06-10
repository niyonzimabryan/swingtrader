import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from database.token_store import (
    EncryptedFileTokenStorage,
    generate_key,
    is_configured,
    token_store_path,
)


def _run(coro):
    return asyncio.run(coro)


class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key = generate_key()
        self.settings = SimpleNamespace(
            token_encryption_key=self.key,
            database_url=f"sqlite:///{Path(self.tmp.name) / 'swing.db'}",
        )
        self.store = EncryptedFileTokenStorage(self.settings)

    def tearDown(self):
        self.tmp.cleanup()

    def _token(self, **kw):
        base = dict(access_token="acc-123", token_type="Bearer", expires_in=3600, scope="internal")
        base.update(kw)
        return OAuthToken(**base)

    # --- config gating ---
    def test_missing_key_is_unconfigured_and_raises(self):
        s = SimpleNamespace(token_encryption_key="", database_url="sqlite:///x.db")
        self.assertFalse(is_configured(s))
        with self.assertRaises(ValueError):
            EncryptedFileTokenStorage(s)

    def test_invalid_key_raises(self):
        s = SimpleNamespace(token_encryption_key="not-a-fernet-key", database_url="sqlite:///x.db")
        with self.assertRaises(ValueError):
            EncryptedFileTokenStorage(s)

    def test_path_colocated_with_db(self):
        p = token_store_path(self.settings)
        self.assertEqual(p, Path(self.tmp.name) / "robinhood_token.enc")

    # --- round trips ---
    def test_token_round_trip(self):
        _run(self.store.set_tokens(self._token(refresh_token="ref-xyz")))
        got = _run(self.store.get_tokens())
        self.assertIsNotNone(got)
        self.assertEqual(got.access_token, "acc-123")
        self.assertEqual(got.refresh_token, "ref-xyz")

    def test_client_info_round_trip(self):
        info = OAuthClientInformationFull(
            client_id="cid-1",
            redirect_uris=["http://localhost:8765/callback"],
            token_endpoint_auth_method="none",
        )
        _run(self.store.set_client_info(info))
        got = _run(self.store.get_client_info())
        self.assertEqual(got.client_id, "cid-1")

    def test_get_tokens_none_when_empty(self):
        self.assertIsNone(_run(self.store.get_tokens()))
        self.assertIsNone(_run(self.store.get_client_info()))

    # --- encryption at rest ---
    def test_file_is_encrypted_not_plaintext(self):
        _run(self.store.set_tokens(self._token(refresh_token="ref-secret")))
        raw = (Path(self.tmp.name) / "robinhood_token.enc").read_bytes()
        self.assertNotIn(b"ref-secret", raw)
        self.assertNotIn(b"acc-123", raw)

    def test_file_mode_is_owner_only(self):
        _run(self.store.set_tokens(self._token()))
        mode = os.stat(Path(self.tmp.name) / "robinhood_token.enc").st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_wrong_key_returns_none_not_crash(self):
        _run(self.store.set_tokens(self._token(refresh_token="ref-xyz")))
        other = SimpleNamespace(token_encryption_key=generate_key(), database_url=self.settings.database_url)
        other_store = EncryptedFileTokenStorage(other)
        self.assertIsNone(_run(other_store.get_tokens()))  # decrypt fails -> None

    # --- status / discovery signal ---
    def test_status_reports_refresh_token_present(self):
        _run(self.store.set_tokens(self._token(refresh_token="ref-xyz")))
        st = self.store.status()
        self.assertTrue(st["has_access_token"])
        self.assertTrue(st["has_refresh_token"])
        self.assertEqual(st["scope"], "internal")
        self.assertFalse(st["needs_reauth"])  # fresh + has refresh

    def test_status_flags_reauth_when_no_refresh_and_expired(self):
        # No refresh token, already expired -> needs_reauth True.
        _run(self.store.set_tokens(self._token(refresh_token=None, expires_in=1)))
        # Force an expired absolute timestamp by rewriting expires_at into the past.
        import json
        from cryptography.fernet import Fernet

        p = Path(self.tmp.name) / "robinhood_token.enc"
        f = Fernet(self.key.encode())
        blob = json.loads(f.decrypt(p.read_bytes()))
        blob["tokens_expires_at"] = "2000-01-01T00:00:00+00:00"
        p.write_bytes(f.encrypt(json.dumps(blob).encode()))

        st = self.store.status()
        self.assertTrue(st["has_access_token"])
        self.assertFalse(st["has_refresh_token"])
        self.assertTrue(st["needs_reauth"])


if __name__ == "__main__":
    unittest.main()
