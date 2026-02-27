"""
SecretStore — Multi-MCP

Handles encrypted storage and retrieval of secrets (API keys, SSH credentials).

Design principles (from AGENTS.md):
  - Secrets are NEVER stored in plain text on disk.
  - Secrets are NEVER returned to clients or written to logs.
  - The GUI may display a masked preview (e.g. "sk-****...abc") but never the full value.
  - Encryption uses Fernet (symmetric AES-128-CBC + HMAC) from the `cryptography` package.
  - The master key is derived from a passphrase (or a randomly generated key stored in a
    separate, gitignored file: .secrets/master.key).

Storage layout:
  .secrets/
    master.key          — randomly generated Fernet key (gitignored)
    store.json          — encrypted secret blobs, keyed by alias reference

IMPORTANT: .secrets/ is listed in .gitignore. Never commit this directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet


_DEFAULT_SECRETS_DIR = Path(".secrets")
_MASTER_KEY_FILE = _DEFAULT_SECRETS_DIR / "master.key"
_STORE_FILE = _DEFAULT_SECRETS_DIR / "store.json"


class SecretStore:
    """
    Encrypted key-value store for secrets.

    Usage::

        store = SecretStore()
        store.set("search:tavily_default", "tvly-xxxxxxxxxxxx")
        value = store.get("search:tavily_default")   # returns plain text in memory only
        preview = store.masked_preview("search:tavily_default")  # "tvly-****...xxx"
    """

    def __init__(self, secrets_dir: Path | str = _DEFAULT_SECRETS_DIR) -> None:
        self._dir = Path(secrets_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._key_file = self._dir / "master.key"
        self._store_file = self._dir / "store.json"
        self._fernet = Fernet(self._load_or_create_key())

    # ------------------------------------------------------------------
    # Key management
    # ------------------------------------------------------------------

    def _load_or_create_key(self) -> bytes:
        if self._key_file.exists():
            return self._key_file.read_bytes().strip()
        key = Fernet.generate_key()
        self._key_file.write_bytes(key)
        # Restrict permissions to owner only
        os.chmod(self._key_file, 0o600)
        return key

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def _load_store(self) -> dict[str, str]:
        if not self._store_file.exists():
            return {}
        return json.loads(self._store_file.read_text(encoding="utf-8"))

    def _save_store(self, data: dict[str, str]) -> None:
        self._store_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.chmod(self._store_file, 0o600)

    def set(self, ref: str, plain_value: str) -> None:
        """Encrypt and store a secret under *ref*."""
        store = self._load_store()
        encrypted = self._fernet.encrypt(plain_value.encode()).decode()
        store[ref] = encrypted
        self._save_store(store)

    def get(self, ref: str) -> str | None:
        """Decrypt and return the secret for *ref*, or None if not found."""
        store = self._load_store()
        encrypted = store.get(ref)
        if encrypted is None:
            return None
        return self._fernet.decrypt(encrypted.encode()).decode()

    def delete(self, ref: str) -> bool:
        """Remove a secret. Returns True if it existed."""
        store = self._load_store()
        if ref not in store:
            return False
        del store[ref]
        self._save_store(store)
        return True

    def list_refs(self) -> list[str]:
        """Return all stored secret references (not values)."""
        return list(self._load_store().keys())

    def exists(self, ref: str) -> bool:
        return ref in self._load_store()

    # ------------------------------------------------------------------
    # Masking
    # ------------------------------------------------------------------

    @staticmethod
    def masked_preview(plain_value: str, visible_prefix: int = 4, visible_suffix: int = 3) -> str:
        """
        Return a masked preview of a secret value.
        Example: "tvly-abcdefghij" -> "tvly-****...hij"
        """
        if len(plain_value) <= visible_prefix + visible_suffix + 4:
            return "*" * len(plain_value)
        prefix = plain_value[:visible_prefix]
        suffix = plain_value[-visible_suffix:]
        return f"{prefix}****...{suffix}"

    def get_masked_preview(self, ref: str) -> str | None:
        """Return a masked preview for the stored secret, or None if not found."""
        value = self.get(ref)
        if value is None:
            return None
        return self.masked_preview(value)

    # ------------------------------------------------------------------
    # Rotation
    # ------------------------------------------------------------------

    def rotate(self, ref: str, new_value: str) -> None:
        """Replace an existing secret with a new value."""
        self.set(ref, new_value)

    def disable(self, ref: str) -> None:
        """Mark a secret as disabled by prefixing its value with 'DISABLED:'."""
        value = self.get(ref)
        if value and not value.startswith("DISABLED:"):
            self.set(ref, f"DISABLED:{value}")
