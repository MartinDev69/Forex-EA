"""Live-vs-backtest drift monitor.

Pulls the most recent N closed trades per (strategy, symbol) from the
journal, computes win rate / avg-R / trade frequency, and compares to the
stored baseline. Emits a per-pair `DriftReport` with a status (ok / warn /
danger / unknown) so the UI can flag strategies that are degrading before
they bleed real money.

Cost profile (relevant for VPS deploys):
- One SQLite query per (strategy, symbol) we have a baseline for, with an
  index-friendly LIMIT N. For the typical 3 strategies × 3 symbols = 9
  small queries per refresh.
- No background thread — the API endpoint memoizes the result, so the
  load profile scales with API calls, not bot ticks.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .baseline import Baseline, BaselineStore


@dataclass(frozen=True)
class DriftConfig:
    # How many recent closed trades define the "live" window for each pair.
    live_trades_window: int = 50
    # Below this many live trades, we don't trust the comparison and the
    # status stays 'unknown'. Avoids a 1-loss sample tripping a danger flag.
    min_live_trades: int = 10
    # Win-rate / avg-R drift below this is considered noise (status=ok).
    warn_delta: float = 0.10
    # Above this, status flips to 'danger' (the strategy is materially off).
    danger_delta: float = 0.20

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "DriftConfig":
        import os
        e = env if env is not None else os.environ
        return cls(
            live_trades_window=int(e.get("DRIFT_LIVE_TRADES_WINDOW", "50")),
            min_live_trades=int(e.get("DRIFT_MIN_LIVE_TRADES", "10")),
            warn_delta=float(e.get("DRIFT_WARN_DELTA", "0.10")),
            danger_delta=float(e.get("DRIFT_DANGER_DELTA", "0.20")),
        )


@dataclass(frozen=True)
class MetricDelta:
    name: str         # 'win_rate' | 'avg_r' | 'trades_per_day'
    baseline: float
    live: float
    delta: float      # live - baseline
    delta_pct: float  # (live - baseline) / abs(baseline) when baseline != 0


@dataclass(frozen=True)
class DriftReport:
    strategy: str
    symbol: str
    status: str  # 'ok' | 'warn' | 'danger' | 'unknown'
    live_trade_count: int
    baseline: Baseline | None
    metrics: tuple[MetricDelta, ...]
    note: str

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "status": self.status,
            "live_trade_count": self.live_trade_count,
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "metrics": [
                {
                    "name": m.name,
                    "baseline": m.baseline,
                    "live": m.live,
                    "delta": m.delta,
                    "delta_pct": m.delta_pct,
                }
                for m in self.metrics
            ],
            "note": self.note,
        }


class DriftMonitor:
    def __init__(
        self,
        db_path: Path | str,
        baseline_store: BaselineStore,
        config: DriftConfig | None = None,
    ) -> None:
        self.path = Path(db_path)
        self.baseline_store = baseline_store
        self.config = config or DriftConfig()

    def report(self) -> list[DriftReport]:
        """Return one DriftReport per known baseline.

        If you want a report for a (strategy, symbol) without a baseline,
        seed one first with `BaselineStore.upsert` — we don't fabricate
        comparisons.
        """
        out: list[DriftReport] = []
        for baseline in self.baseline_store.all():
            out.append(self._compare(baseline))
        return out

    def _compare(self, baseline: Baseline) -> DriftReport:
        cfg = self.config
        live = self._live_metrics(baseline.strategy, baseline.symbol, cfg.live_trades_window)
        live_count = live["count"]

        if live_count < cfg.min_live_trades:
            return DriftReport(
                strategy=baseline.strategy,
                symbol=baseline.symbol,
                status="unknown",
                live_trade_count=live_count,
                baseline=baseline,
                metrics=(),
                note=(
                    f"only {live_count}/{cfg.min_live_trades} live trades — "
                    "monitoring will activate once enough samples are in"
                ),
            )

        deltas = (
            _delta("win_rate", baseline.win_rate, live["win_rate"]),
            _delta("avg_r", baseline.avg_r, live["avg_r"]),
            _delta("trades_per_day", baseline.avg_trades_per_day, live["trades_per_day"]),
        )

        # Win rate and avg-R degradation matter more than trade-rate drift —
        # rate can swing on legitimate regime changes. Keep the gate on the
        # PnL drivers.
        worst = max(abs(d.delta) for d in deltas[:2])
        if worst >= cfg.danger_delta:
            status = "danger"
        elif worst >= cfg.warn_delta:
            status = "warn"
        else:
            status = "ok"

        return DriftReport(
            strategy=baseline.strategy,
            symbol=baseline.symbol,
            status=status,
            live_trade_count=live_count,
            baseline=baseline,
            metrics=deltas,
            note=_note(status, deltas),
        )

    def _live_metrics(self, strategy: str, symbol: str, window: int) -> dict:
        """Pull last `window` closed trades for this (strategy, symbol)."""
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT entry_price, stop_loss, exit_price, pnl, side,
                       opened_at, closed_at
                FROM trades
                WHERE strategy = ? AND symbol = ? AND status = 'CLOSED'
                ORDER BY closed_at DESC
                LIMIT ?
                """,
                (strategy, symbol, window),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return {"count": 0, "win_rate": 0.0, "avg_r": 0.0, "trades_per_day": 0.0}

        wins = 0
        r_values: list[float] = []
        for r in rows:
            if (r["pnl"] or 0.0) > 0:
                wins += 1
            risk_per_unit = abs(r["entry_price"] - r["stop_loss"])
            if risk_per_unit > 0 and r["exit_price"] is not None:
                # Realized R — what the trade actually returned in stop-multiples.
                # Sign by side so stops register as -1R and targets as +1R+.
                move = (r["exit_price"] - r["entry_price"])
                if r["side"] == "SELL":
                    move = -move
                r_values.append(move / risk_per_unit)

        avg_r = sum(r_values) / len(r_values) if r_values else 0.0
        win_rate = wins / len(rows)

        first = _parse_dt(rows[-1]["closed_at"]) if rows[-1]["closed_at"] else None
        last = _parse_dt(rows[0]["closed_at"]) if rows[0]["closed_at"] else None
        if first and last:
            span_days = max(1.0, (last - first).total_seconds() / 86400.0)
        else:
            span_days = 1.0
        trades_per_day = len(rows) / span_days

        return {
            "count": len(rows),
            "win_rate": win_rate,
            "avg_r": avg_r,
            "trades_per_day": trades_per_day,
        }


def _delta(name: str, baseline_v: float, live_v: float) -> MetricDelta:
    delta = live_v - baseline_v
    pct = delta / abs(baseline_v) if baseline_v != 0 else 0.0
    return MetricDelta(name=name, baseline=baseline_v, live=live_v, delta=delta, delta_pct=pct)


def _note(status: str, deltas: tuple[MetricDelta, ...]) -> str:
    if status == "ok":
        return "live performance is tracking the baseline"
    by_name = {d.name: d for d in deltas}
    wr = by_name["win_rate"]
    r = by_name["avg_r"]
    bits: list[str] = []
    if abs(wr.delta) >= 0.05:
        bits.append(f"win-rate {_signed(wr.delta * 100)} pp vs baseline")
    if abs(r.delta) >= 0.05:
        bits.append(f"avg-R {_signed(r.delta)} vs baseline")
    return "; ".join(bits) if bits else "trade rate drifting"


def _signed(v: float) -> str:
    return f"{v:+.2f}"


def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
