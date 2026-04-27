"""ReplayEngine — re-walks a trade's recorded bars with tweaked SL/TP.

Given a trade row and the bars it traversed, simulates the exit under
new stop/target levels. The decision rule mirrors the live bot's
`_should_close_order`: stop touch → fill at stop; target touch → fill at
target. When both levels are reached on the same bar (gap, big move),
SL fires first — the conservative assumption operators expect.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from src.risk.position_sizing import pip_size, pip_value

from .path_store import PathStore


@dataclass(frozen=True)
class ReplayRequest:
    """Either pass absolute levels OR multipliers — multipliers are applied
    to the original SL/TP distance from entry."""
    stop_loss: float | None = None
    take_profit: float | None = None
    sl_mult: float | None = None
    tp_mult: float | None = None


@dataclass(frozen=True)
class ReplayResult:
    trade_id: int
    side: str
    symbol: str
    entry_price: float
    original_stop: float
    original_target: float
    original_pnl: float
    original_close_reason: str | None
    replay_stop: float
    replay_target: float
    replay_exit_price: float | None
    replay_close_reason: str   # 'target', 'stop', 'open_at_end', 'no_path'
    replay_pnl: float
    replay_r_multiple: float | None
    pnl_delta: float
    bars_walked: int

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "side": self.side,
            "symbol": self.symbol,
            "entry_price": self.entry_price,
            "original_stop": self.original_stop,
            "original_target": self.original_target,
            "original_pnl": self.original_pnl,
            "original_close_reason": self.original_close_reason,
            "replay_stop": self.replay_stop,
            "replay_target": self.replay_target,
            "replay_exit_price": self.replay_exit_price,
            "replay_close_reason": self.replay_close_reason,
            "replay_pnl": self.replay_pnl,
            "replay_r_multiple": self.replay_r_multiple,
            "pnl_delta": self.pnl_delta,
            "bars_walked": self.bars_walked,
        }


class ReplayEngine:
    def __init__(
        self,
        path_store: PathStore,
        db_path: Path | str = "data/trades.db",
    ) -> None:
        self.path_store = path_store
        self.db_path = Path(db_path)

    def replay(self, trade_id: int, request: ReplayRequest) -> ReplayResult | None:
        """Resolve the alternative outcome. Returns None if the trade is
        unknown or hasn't closed (we only replay completed trades — open
        ones don't have a finalized PnL to compare against).
        """
        trade = self._load_trade(trade_id)
        if trade is None:
            return None
        if trade["status"] != "CLOSED":
            return None

        side = trade["side"]
        entry = float(trade["entry_price"])
        orig_sl = float(trade["stop_loss"])
        orig_tp = float(trade["take_profit"])
        replay_sl, replay_tp = self._resolve_levels(side, entry, orig_sl, orig_tp, request)

        bars = self.path_store.read(trade_id)
        if not bars:
            return ReplayResult(
                trade_id=trade_id,
                side=side,
                symbol=trade["symbol"],
                entry_price=entry,
                original_stop=orig_sl,
                original_target=orig_tp,
                original_pnl=float(trade["pnl"]),
                original_close_reason=trade["close_reason"],
                replay_stop=replay_sl,
                replay_target=replay_tp,
                replay_exit_price=None,
                replay_close_reason="no_path",
                replay_pnl=0.0,
                replay_r_multiple=None,
                pnl_delta=-float(trade["pnl"]),
                bars_walked=0,
            )

        exit_price, reason, walked = self._walk(side, replay_sl, replay_tp, bars)
        if exit_price is None:
            # The recorded path didn't reach a stop or target. Use the last
            # close as the mark-to-market exit so the operator has something
            # to compare against.
            exit_price = bars[-1].close
            reason = "open_at_end"

        replay_pnl = self._pnl(trade["symbol"], side, entry, exit_price, float(trade["lot_size"]))
        r = self._r_multiple(side, entry, replay_sl, exit_price)

        return ReplayResult(
            trade_id=trade_id,
            side=side,
            symbol=trade["symbol"],
            entry_price=entry,
            original_stop=orig_sl,
            original_target=orig_tp,
            original_pnl=float(trade["pnl"]),
            original_close_reason=trade["close_reason"],
            replay_stop=replay_sl,
            replay_target=replay_tp,
            replay_exit_price=exit_price,
            replay_close_reason=reason,
            replay_pnl=replay_pnl,
            replay_r_multiple=r,
            pnl_delta=replay_pnl - float(trade["pnl"]),
            bars_walked=walked,
        )

    # ------------------------------------------------------------------ internals

    def _load_trade(self, trade_id: int) -> dict | None:
        with sqlite3.connect(self.db_path) as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT * FROM trades WHERE id = ?", (trade_id,),
            ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _resolve_levels(
        side: str,
        entry: float,
        orig_sl: float,
        orig_tp: float,
        req: ReplayRequest,
    ) -> tuple[float, float]:
        """Multiplier applies to the original distance from entry. Absolute
        levels override multipliers if both are given.
        """
        sl = req.stop_loss
        tp = req.take_profit
        if sl is None and req.sl_mult is not None:
            sl_dist = abs(entry - orig_sl) * float(req.sl_mult)
            sl = (entry - sl_dist) if side.upper() == "BUY" else (entry + sl_dist)
        if tp is None and req.tp_mult is not None:
            tp_dist = abs(orig_tp - entry) * float(req.tp_mult)
            tp = (entry + tp_dist) if side.upper() == "BUY" else (entry - tp_dist)
        if sl is None:
            sl = orig_sl
        if tp is None:
            tp = orig_tp
        return float(sl), float(tp)

    @staticmethod
    def _walk(
        side: str,
        sl: float,
        tp: float,
        bars: list,
    ) -> tuple[float | None, str, int]:
        """Step through bars in order. Conservative when both touched in
        the same bar — stop fires first.
        """
        is_buy = side.upper() == "BUY"
        for i, b in enumerate(bars, start=1):
            if is_buy:
                hits_stop = b.low <= sl
                hits_target = b.high >= tp
                if hits_stop and hits_target:
                    return sl, "stop", i
                if hits_stop:
                    return sl, "stop", i
                if hits_target:
                    return tp, "target", i
            else:  # SELL
                hits_stop = b.high >= sl
                hits_target = b.low <= tp
                if hits_stop and hits_target:
                    return sl, "stop", i
                if hits_stop:
                    return sl, "stop", i
                if hits_target:
                    return tp, "target", i
        return None, "open_at_end", len(bars)

    @staticmethod
    def _pnl(symbol: str, side: str, entry: float, exit_: float, lot_size: float) -> float:
        """Same convention as the mock executor's pnl calc:
        signed move (in pips) × pip_value(symbol) × lot_size.
        """
        size = pip_size(symbol)
        if size <= 0:
            return 0.0
        signed_pips = (exit_ - entry) / size if side.upper() == "BUY" else (entry - exit_) / size
        return signed_pips * pip_value(symbol) * lot_size

    @staticmethod
    def _r_multiple(side: str, entry: float, sl: float, exit_: float) -> float | None:
        risk = abs(entry - sl)
        if risk <= 0:
            return None
        signed = (exit_ - entry) if side.upper() == "BUY" else (entry - exit_)
        return signed / risk
