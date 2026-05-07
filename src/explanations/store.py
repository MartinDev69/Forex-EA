"""Per-trade decision context — captured at signal-open time.

The bot already has the regime, allocator weight, signal levels, and
account state in memory when it decides to open a trade. We persist a
snapshot here so the operator can later answer "why did the bot take
that?" without having to reason from sparse logs.

Cost: one small SQLite INSERT per trade open. Reads are single-row
lookups by `trade_id`, so the API doesn't need caching.

Capture-at-write (vs. reconstruct-at-read) was chosen deliberately —
ML scores and allocator weights at decision time aren't reconstructible
from the ambient stores after the fact, so a write path is the only one
that gives a complete picture.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_explanations (
    trade_id            INTEGER PRIMARY KEY,
    strategy            TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    side                TEXT NOT NULL,
    signal_price        REAL NOT NULL,
    signal_stop         REAL NOT NULL,
    signal_target       REAL NOT NULL,
    risk_reward         REAL NOT NULL,
    stop_distance_pips  REAL NOT NULL,
    lot_size            REAL NOT NULL,
    account_balance     REAL NOT NULL,
    regime_trend        TEXT,
    regime_volatility   TEXT,
    regime_label        TEXT,
    regime_adx          REAL,
    regime_atr_pct      REAL,
    allocator_role      TEXT,
    allocator_weight    REAL,
    ml_filter_passed    INTEGER,
    notes               TEXT,
    opened_at           TEXT NOT NULL,
    indicators_json     TEXT,
    bars_json           TEXT,
    overlays_json       TEXT
);
"""


@dataclass(frozen=True)
class TradeExplanation:
    trade_id: int
    strategy: str
    symbol: str
    side: str               # 'BUY' | 'SELL'
    signal_price: float
    signal_stop: float
    signal_target: float
    risk_reward: float      # |TP-entry| / |entry-SL|
    stop_distance_pips: float
    lot_size: float
    account_balance: float
    opened_at: str          # ISO-8601 UTC
    regime_trend: str | None = None
    regime_volatility: str | None = None
    regime_label: str | None = None
    regime_adx: float | None = None
    regime_atr_pct: float | None = None
    allocator_role: str | None = None
    allocator_weight: float | None = None
    # Tri-state: True = filter passed, False = filter rejected (shouldn't
    # normally land here, but recorded for debugging), None = no filter wired.
    ml_filter_passed: bool | None = None
    notes: str = ""
    # Indicator snapshot — what the strategy "saw" when it decided.
    # Free-form per strategy: e.g. {"rsi": 28.5, "ema_fast": 1.0852,
    # "atr": 0.00074}. Empty when the strategy doesn't expose any.
    indicators: dict = field(default_factory=dict)
    # OHLC snapshot — last N bars before the signal fired. Each entry is
    # {t: ISO-8601, o, h, l, c}. Stored so the dashboard can draw the
    # exact candles the strategy was looking at.
    bars: list = field(default_factory=list)
    # Indicator line series aligned with `bars`. Each entry:
    # {name: 'ema_fast', kind: 'line'|'band', color: str, values: [...]}.
    # Used by the chart to draw indicator overlays on top of the candles.
    overlays: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "strategy": self.strategy,
            "symbol": self.symbol,
            "side": self.side,
            "signal_price": self.signal_price,
            "signal_stop": self.signal_stop,
            "signal_target": self.signal_target,
            "risk_reward": self.risk_reward,
            "stop_distance_pips": self.stop_distance_pips,
            "lot_size": self.lot_size,
            "account_balance": self.account_balance,
            "regime_trend": self.regime_trend,
            "regime_volatility": self.regime_volatility,
            "regime_label": self.regime_label,
            "regime_adx": self.regime_adx,
            "regime_atr_pct": self.regime_atr_pct,
            "allocator_role": self.allocator_role,
            "allocator_weight": self.allocator_weight,
            "ml_filter_passed": self.ml_filter_passed,
            "notes": self.notes,
            "opened_at": self.opened_at,
            "indicators": dict(self.indicators or {}),
            "bars": list(self.bars or []),
            "overlays": list(self.overlays or []),
        }


class TradeExplanationStore:
    def __init__(self, db_path: Path | str = "data/trades.db") -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(_SCHEMA)
            # Backwards-compat: pre-existing tables won't have the
            # indicators_json column. ALTER TABLE adds it once.
            cols = {r["name"] for r in c.execute("PRAGMA table_info(trade_explanations)")}
            if "indicators_json" not in cols:
                c.execute("ALTER TABLE trade_explanations ADD COLUMN indicators_json TEXT")
            if "bars_json" not in cols:
                c.execute("ALTER TABLE trade_explanations ADD COLUMN bars_json TEXT")
            if "overlays_json" not in cols:
                c.execute("ALTER TABLE trade_explanations ADD COLUMN overlays_json TEXT")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def record(self, exp: TradeExplanation) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO trade_explanations (
                    trade_id, strategy, symbol, side,
                    signal_price, signal_stop, signal_target,
                    risk_reward, stop_distance_pips, lot_size, account_balance,
                    regime_trend, regime_volatility, regime_label,
                    regime_adx, regime_atr_pct,
                    allocator_role, allocator_weight, ml_filter_passed,
                    notes, opened_at, indicators_json, bars_json, overlays_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    exp.trade_id, exp.strategy, exp.symbol, exp.side,
                    exp.signal_price, exp.signal_stop, exp.signal_target,
                    exp.risk_reward, exp.stop_distance_pips, exp.lot_size,
                    exp.account_balance,
                    exp.regime_trend, exp.regime_volatility, exp.regime_label,
                    exp.regime_adx, exp.regime_atr_pct,
                    exp.allocator_role, exp.allocator_weight,
                    None if exp.ml_filter_passed is None else int(exp.ml_filter_passed),
                    exp.notes, exp.opened_at,
                    json.dumps(exp.indicators) if exp.indicators else None,
                    json.dumps(exp.bars) if exp.bars else None,
                    json.dumps(exp.overlays) if exp.overlays else None,
                ),
            )

    def get(self, trade_id: int) -> TradeExplanation | None:
        with self._conn() as c:
            row = c.execute(
                """
                SELECT trade_id, strategy, symbol, side,
                       signal_price, signal_stop, signal_target,
                       risk_reward, stop_distance_pips, lot_size, account_balance,
                       regime_trend, regime_volatility, regime_label,
                       regime_adx, regime_atr_pct,
                       allocator_role, allocator_weight, ml_filter_passed,
                       notes, opened_at, indicators_json,
                       bars_json, overlays_json
                FROM trade_explanations
                WHERE trade_id = ?
                """,
                (trade_id,),
            ).fetchone()
        if row is None:
            return None
        return TradeExplanation(
            trade_id=row["trade_id"],
            strategy=row["strategy"],
            symbol=row["symbol"],
            side=row["side"],
            signal_price=row["signal_price"],
            signal_stop=row["signal_stop"],
            signal_target=row["signal_target"],
            risk_reward=row["risk_reward"],
            stop_distance_pips=row["stop_distance_pips"],
            lot_size=row["lot_size"],
            account_balance=row["account_balance"],
            regime_trend=row["regime_trend"],
            regime_volatility=row["regime_volatility"],
            regime_label=row["regime_label"],
            regime_adx=row["regime_adx"],
            regime_atr_pct=row["regime_atr_pct"],
            allocator_role=row["allocator_role"],
            allocator_weight=row["allocator_weight"],
            ml_filter_passed=(
                None if row["ml_filter_passed"] is None
                else bool(row["ml_filter_passed"])
            ),
            notes=row["notes"] or "",
            opened_at=row["opened_at"],
            indicators=(
                json.loads(row["indicators_json"]) if row["indicators_json"] else {}
            ),
            bars=(
                json.loads(row["bars_json"]) if row["bars_json"] else []
            ),
            overlays=(
                json.loads(row["overlays_json"]) if row["overlays_json"] else []
            ),
        )
