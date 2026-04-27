"""Voice kill switch — halt the bot via a spoken phrase.

Speech-to-text happens client-side (the mobile app uses platform STT);
this module receives the transcribed string and decides whether it's a
kill command. Two reasons to keep STT off-server:

  * Audio uploads are large, slow on cellular, and a privacy hazard.
  * Mobile STT is good enough and runs offline.

What this module owns:

  * Phrase matching tolerant to STT noise — fuzzy ratio + substring,
    normalized for case/punctuation/whitespace. STT often returns
    "stop reading" instead of "stop trading"; the threshold is tuned to
    catch the obvious near-misses without over-matching unrelated speech.

  * `KillSwitchFlag` — a single-row SQLite cell the bot polls each tick.
    The API and the bot run as separate processes, so a process-local
    flag wouldn't propagate; SQLite gives us a durable cross-process
    signal that survives a bot restart (bot still won't trade until an
    operator clears it from the dashboard).

  * `VoiceLogStore` — audit log of every transcript received, matched
    or not. Useful for "did the kill switch fire?" forensics and for
    catching false positives in the wild.

Re-arming after a trip is a deliberate-friction operation: clearing
the flag is gated behind require_2fa at the API layer, because the
whole point of a voice kill is that you can act without unlocking your
phone — so coming back online should be the slower path, not the fast
one.
"""
from __future__ import annotations

import os
import re
import sqlite3
import string
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

_DEFAULT_PHRASES = (
    "stop trading",
    "halt the bot",
    "panic stop",
    "emergency stop",
)

_DEFAULT_FUZZY_THRESHOLD = 0.80


_PUNCT_RE = re.compile(rf"[{re.escape(string.punctuation)}]+")
_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace.

    Leaves the string usable for both substring and ratio matching.
    Empty input → empty output (callers treat that as 'no match').
    """
    if not text:
        return ""
    s = text.lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


@dataclass(frozen=True)
class VoiceKillConfig:
    phrases: tuple[str, ...] = _DEFAULT_PHRASES
    fuzzy_threshold: float = _DEFAULT_FUZZY_THRESHOLD

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> VoiceKillConfig:
        e = env if env is not None else os.environ
        raw = e.get("VOICE_KILL_PHRASES", "").strip()
        # Pipe-separated to avoid clashing with commas inside phrases (rare,
        # but cheap to guard against). Empty value → built-in defaults.
        if raw:
            phrases = tuple(p.strip() for p in raw.split("|") if p.strip())
        else:
            phrases = _DEFAULT_PHRASES
        try:
            threshold = float(e.get("VOICE_FUZZY_THRESHOLD", _DEFAULT_FUZZY_THRESHOLD))
        except ValueError:
            threshold = _DEFAULT_FUZZY_THRESHOLD
        # Clamp to a sane range — a threshold below 0.5 starts matching
        # everything; above 1.0 is impossible.
        threshold = max(0.5, min(threshold, 1.0))
        return cls(phrases=phrases, fuzzy_threshold=threshold)


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    phrase: str | None
    score: float
    normalized_transcript: str


def match_phrase(transcript: str, config: VoiceKillConfig) -> MatchResult:
    """Decide whether `transcript` triggers a kill.

    Two ways to match:
      1. The normalized phrase appears as a substring of the normalized
         transcript ("please stop trading now" → matches "stop trading").
      2. The best-effort fuzzy ratio over the whole transcript meets
         the threshold — catches STT errors that warp the substring.

    Returns the highest-scoring match across all configured phrases.
    """
    norm_t = normalize(transcript)
    if not norm_t:
        return MatchResult(False, None, 0.0, "")
    best_phrase: str | None = None
    best_score = 0.0
    matched = False
    for phrase in config.phrases:
        norm_p = normalize(phrase)
        if not norm_p:
            continue
        if norm_p in norm_t:
            return MatchResult(True, phrase, 1.0, norm_t)
        ratio = SequenceMatcher(a=norm_p, b=norm_t).ratio()
        if ratio > best_score:
            best_score = ratio
            best_phrase = phrase
        if ratio >= config.fuzzy_threshold:
            matched = True
    return MatchResult(
        matched=matched,
        phrase=best_phrase if matched else None,
        score=best_score,
        normalized_transcript=norm_t,
    )


# ---------------------------------------------------------------- DB layer

_FLAG_SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_kill_flag (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    active       INTEGER NOT NULL,
    triggered_at TEXT,
    triggered_by TEXT,
    phrase       TEXT,
    cleared_at   TEXT,
    cleared_by   TEXT
);
"""

_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_kill_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    username    TEXT NOT NULL,
    transcript  TEXT NOT NULL,
    matched     INTEGER NOT NULL,
    phrase      TEXT,
    score       REAL NOT NULL
);
"""


@dataclass(frozen=True)
class KillSwitchState:
    active: bool
    triggered_at: str | None
    triggered_by: str | None
    phrase: str | None
    cleared_at: str | None
    cleared_by: str | None


class KillSwitchFlag:
    """Single-row latch surfaced via SQLite so API + bot processes share state."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(_FLAG_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def is_active(self) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT active FROM voice_kill_flag WHERE id = 1"
            ).fetchone()
        return bool(row["active"]) if row else False

    def activate(self, *, username: str, phrase: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO voice_kill_flag
                    (id, active, triggered_at, triggered_by, phrase, cleared_at, cleared_by)
                VALUES (1, 1, ?, ?, ?, NULL, NULL)
                ON CONFLICT(id) DO UPDATE SET
                    active = 1,
                    triggered_at = excluded.triggered_at,
                    triggered_by = excluded.triggered_by,
                    phrase = excluded.phrase,
                    cleared_at = NULL,
                    cleared_by = NULL
                """,
                (now, username, phrase),
            )

    def clear(self, *, username: str) -> bool:
        """Re-arm the bot. Returns True if a clear actually happened, False
        when the flag was already inactive (idempotent)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            cur = c.execute(
                """
                UPDATE voice_kill_flag
                SET active = 0, cleared_at = ?, cleared_by = ?
                WHERE id = 1 AND active = 1
                """,
                (now, username),
            )
            return cur.rowcount > 0

    def state(self) -> KillSwitchState:
        with self._conn() as c:
            row = c.execute(
                """
                SELECT active, triggered_at, triggered_by, phrase, cleared_at, cleared_by
                FROM voice_kill_flag WHERE id = 1
                """
            ).fetchone()
        if row is None:
            return KillSwitchState(False, None, None, None, None, None)
        return KillSwitchState(
            active=bool(row["active"]),
            triggered_at=row["triggered_at"],
            triggered_by=row["triggered_by"],
            phrase=row["phrase"],
            cleared_at=row["cleared_at"],
            cleared_by=row["cleared_by"],
        )


@dataclass(frozen=True)
class VoiceLogEntry:
    id: int
    received_at: str
    username: str
    transcript: str
    matched: bool
    phrase: str | None
    score: float


class VoiceLogStore:
    """Append-only audit of every voice command attempted, matched or not."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(_LOG_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def record(self, *, username: str, transcript: str, result: MatchResult) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO voice_kill_log
                    (received_at, username, transcript, matched, phrase, score)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    username,
                    transcript,
                    1 if result.matched else 0,
                    result.phrase,
                    result.score,
                ),
            )

    def recent(self, limit: int = 50) -> list[VoiceLogEntry]:
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT id, received_at, username, transcript, matched, phrase, score
                FROM voice_kill_log
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [
            VoiceLogEntry(
                id=r["id"],
                received_at=r["received_at"],
                username=r["username"],
                transcript=r["transcript"],
                matched=bool(r["matched"]),
                phrase=r["phrase"],
                score=r["score"],
            )
            for r in rows
        ]
