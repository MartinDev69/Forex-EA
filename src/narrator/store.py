"""Persistent storage for LLM-generated trade narratives.

One row per closed trade. The bot writes after journal.record_close;
the API reads single rows by trade_id.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_narratives (
    trade_id      INTEGER PRIMARY KEY,
    narrative     TEXT NOT NULL,
    provider      TEXT NOT NULL,         -- 'anthropic' | 'openai' | 'stub'
    model         TEXT,
    prompt_tokens INTEGER,
    output_tokens INTEGER,
    created_at    TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class TradeNarrative:
    trade_id: int
    narrative: str
    provider: str
    created_at: str
    model: str | None = None
    prompt_tokens: int | None = None
    output_tokens: int | None = None

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "narrative": self.narrative,
            "provider": self.provider,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "output_tokens": self.output_tokens,
            "created_at": self.created_at,
        }


class NarrativeStore:
    def __init__(self, db_path: Path | str = "data/trades.db") -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def write(self, narrative: TradeNarrative) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO trade_narratives
                    (trade_id, narrative, provider, model,
                     prompt_tokens, output_tokens, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    narrative.trade_id,
                    narrative.narrative,
                    narrative.provider,
                    narrative.model,
                    narrative.prompt_tokens,
                    narrative.output_tokens,
                    narrative.created_at,
                ),
            )

    def get(self, trade_id: int) -> TradeNarrative | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM trade_narratives WHERE trade_id = ?",
                (trade_id,),
            ).fetchone()
        if row is None:
            return None
        return TradeNarrative(
            trade_id=row["trade_id"],
            narrative=row["narrative"],
            provider=row["provider"],
            model=row["model"],
            prompt_tokens=row["prompt_tokens"],
            output_tokens=row["output_tokens"],
            created_at=row["created_at"],
        )
