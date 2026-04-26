"""Broker credentials — encrypted at rest, keyed per operator.

Each AD-ID has its own private broker config row: `Admi8X` sees only the
admin's saved MT5 credentials, every assigned operator sees only theirs.
Nobody sees anyone else's — the API filters by the authenticated user's
username before touching this store.

Design choices:
- Primary key is `username` (AD-ID). Switching brokers for an operator =
  overwrite their row.
- Password encrypted with Fernet (AES-128-CBC + HMAC-SHA256), key derived
  from `AUTH_SECRET` via PBKDF2-HMAC-SHA256 so we don't have to manage a
  second key. Losing `AUTH_SECRET` means saved broker passwords are
  unrecoverable — the operator would need to re-enter them. Rotating
  `AUTH_SECRET` has the same effect.
- `get_decrypted(username)` is the only path that returns the plaintext
  password. The API never sends plaintext back to the browser —
  /broker/config returns a masked view.
"""
from __future__ import annotations

import base64
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .ad_id import ADMIN_AD_ID

# Stable salt — this is fine because our entropy lives in AUTH_SECRET (48+ bytes
# of url-safe random). Using a per-DB salt would mean losing the DB kills the
# key too; rotating AUTH_SECRET already kills the key and we accept that.
_SALT = b"forex-ea:broker-config:v1"
_KDF_ITERATIONS = 200_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_config (
    username          TEXT PRIMARY KEY,
    broker            TEXT NOT NULL,
    login             INTEGER NOT NULL,
    password_enc      TEXT NOT NULL,
    server            TEXT NOT NULL,
    mt5_path          TEXT NOT NULL DEFAULT '',
    updated_at        TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class BrokerConfig:
    broker: str
    login: int
    password: str
    server: str
    mt5_path: str = ""
    updated_at: datetime | None = None


class BrokerConfigStore:
    def __init__(self, db_path: Path | str, secret: str) -> None:
        if not secret or len(secret) < 32:
            raise ValueError("secret must be 32+ chars (pass AUTH_SECRET)")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(self._derive_key(secret))
        with self._conn() as c:
            self._migrate(c)
            c.execute(_SCHEMA)

    def _migrate(self, c: sqlite3.Connection) -> None:
        """Lift pre-per-user single-row configs onto the AD-ID-keyed schema.

        The old layout had `id INTEGER PRIMARY KEY CHECK (id = 1)` with exactly
        one row. We promote that row to belong to the singleton admin so their
        existing broker setup survives the migration.
        """
        cols = {r[1] for r in c.execute("PRAGMA table_info(broker_config)").fetchall()}
        if not cols:
            return  # fresh DB — the CREATE IF NOT EXISTS below builds the new schema.
        if "username" in cols:
            return  # already migrated.
        row = c.execute("SELECT * FROM broker_config WHERE id = 1").fetchone()
        c.executescript("""
            ALTER TABLE broker_config RENAME TO broker_config_legacy;
            CREATE TABLE broker_config (
                username     TEXT PRIMARY KEY,
                broker       TEXT NOT NULL,
                login        INTEGER NOT NULL,
                password_enc TEXT NOT NULL,
                server       TEXT NOT NULL,
                mt5_path     TEXT NOT NULL DEFAULT '',
                updated_at   TEXT NOT NULL
            );
        """)
        if row is not None:
            c.execute(
                """INSERT INTO broker_config
                   (username, broker, login, password_enc, server, mt5_path, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (ADMIN_AD_ID, row["broker"], row["login"], row["password_enc"],
                 row["server"], row["mt5_path"], row["updated_at"]),
            )
        c.execute("DROP TABLE broker_config_legacy")

    @staticmethod
    def _derive_key(secret: str) -> bytes:
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=_SALT,
            iterations=_KDF_ITERATIONS,
        )
        return base64.urlsafe_b64encode(kdf.derive(secret.encode("utf-8")))

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, username: str, cfg: BrokerConfig) -> None:
        enc = self._fernet.encrypt(cfg.password.encode("utf-8")).decode("utf-8")
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO broker_config (username, broker, login, password_enc, server, mt5_path, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    broker       = excluded.broker,
                    login        = excluded.login,
                    password_enc = excluded.password_enc,
                    server       = excluded.server,
                    mt5_path     = excluded.mt5_path,
                    updated_at   = excluded.updated_at
                """,
                (username, cfg.broker, cfg.login, enc, cfg.server, cfg.mt5_path, now),
            )

    def get_decrypted(self, username: str) -> BrokerConfig | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM broker_config WHERE username = ?", (username,)
            ).fetchone()
        if row is None:
            return None
        try:
            pw = self._fernet.decrypt(row["password_enc"].encode("utf-8")).decode("utf-8")
        except InvalidToken:
            # AUTH_SECRET was rotated — stored password is unrecoverable.
            raise RuntimeError(
                "broker password could not be decrypted — AUTH_SECRET may have been rotated. "
                "Clear and re-enter via the dashboard."
            ) from None
        return BrokerConfig(
            broker=row["broker"],
            login=row["login"],
            password=pw,
            server=row["server"],
            mt5_path=row["mt5_path"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def get_masked(self, username: str) -> dict | None:
        """Browser-safe view — no plaintext password."""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM broker_config WHERE username = ?", (username,)
            ).fetchone()
        if row is None:
            return None
        # Fingerprint lets the UI show "password set" without revealing length.
        fp = hashlib.sha256(row["password_enc"].encode("utf-8")).hexdigest()[:8]
        return {
            "broker": row["broker"],
            "login": row["login"],
            "server": row["server"],
            "mt5_path": row["mt5_path"],
            "password_set": True,
            "password_fingerprint": fp,
            "updated_at": row["updated_at"],
        }

    def clear(self, username: str) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM broker_config WHERE username = ?", (username,))
        return cur.rowcount > 0

    def exists(self, username: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM broker_config WHERE username = ?", (username,)
            ).fetchone()
        return row is not None
