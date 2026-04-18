"""Simple vectorized-ish event backtester.

Walks bar-by-bar, asks the strategy for a signal, enforces one open position
at a time, applies SL/TP exits on the next bar's high/low, and records equity.

Good enough for strategy sanity-checking. Swap in Backtrader for serious work —
see `notebooks/backtrader_example.ipynb` (to be created).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from src.strategies.base import Signal, SignalType, Strategy


@dataclass
class Trade:
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp | None
    side: SignalType
    entry_price: float
    exit_price: float | None
    stop_loss: float
    take_profit: float
    pnl: float = 0.0
    reason: str = ""


@dataclass
class BacktestResult:
    trades: list[Trade]
    equity_curve: pd.Series
    final_equity: float
    total_return: float
    win_rate: float
    total_trades: int
    profit_factor: float

    def summary(self) -> str:
        return (
            f"Trades: {self.total_trades} | "
            f"Win rate: {self.win_rate:.1%} | "
            f"Profit factor: {self.profit_factor:.2f} | "
            f"Final equity: {self.final_equity:.2f} | "
            f"Return: {self.total_return:.2%}"
        )


def run_backtest(
    ohlc: pd.DataFrame,
    strategy: Strategy,
    starting_equity: float = 10_000.0,
    risk_per_trade_pct: float = 0.01,
    lookback: int = 100,
) -> BacktestResult:
    """Walk the dataframe, one bar at a time, and simulate the strategy."""
    equity = starting_equity
    equity_curve: list[float] = []
    trades: list[Trade] = []
    open_trade: Trade | None = None

    for i in range(lookback, len(ohlc)):
        window = ohlc.iloc[: i + 1]
        bar = ohlc.iloc[i]
        ts = ohlc.index[i]

        if open_trade is not None:
            filled = _check_stop_or_target(open_trade, bar, ts)
            if filled:
                equity += open_trade.pnl
                trades.append(open_trade)
                open_trade = None

        if open_trade is None:
            signal = strategy.generate_signal(window)
            if signal.type in (SignalType.BUY, SignalType.SELL):
                open_trade = _open_from_signal(signal, equity, risk_per_trade_pct)

        equity_curve.append(equity + (open_trade.pnl if open_trade else 0))

    if open_trade is not None:
        last = ohlc.iloc[-1]
        open_trade.exit_time = ohlc.index[-1]
        open_trade.exit_price = float(last["close"])
        open_trade.pnl = _pnl(open_trade, float(last["close"]))
        open_trade.reason = "end of data"
        equity += open_trade.pnl
        trades.append(open_trade)

    curve = pd.Series(equity_curve, index=ohlc.index[lookback:])
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses)) or 1e-9

    return BacktestResult(
        trades=trades,
        equity_curve=curve,
        final_equity=equity,
        total_return=(equity - starting_equity) / starting_equity,
        win_rate=len(wins) / len(trades) if trades else 0.0,
        total_trades=len(trades),
        profit_factor=gross_win / gross_loss,
    )


def _open_from_signal(signal: Signal, equity: float, risk_pct: float) -> Trade:
    return Trade(
        entry_time=signal.timestamp,
        exit_time=None,
        side=signal.type,
        entry_price=signal.price,
        exit_price=None,
        stop_loss=signal.stop_loss or 0.0,
        take_profit=signal.take_profit or 0.0,
    )


def _check_stop_or_target(trade: Trade, bar: pd.Series, ts: pd.Timestamp) -> bool:
    high, low = float(bar["high"]), float(bar["low"])
    if trade.side == SignalType.BUY:
        if low <= trade.stop_loss:
            _close(trade, trade.stop_loss, ts, "stop")
            return True
        if high >= trade.take_profit:
            _close(trade, trade.take_profit, ts, "target")
            return True
    else:
        if high >= trade.stop_loss:
            _close(trade, trade.stop_loss, ts, "stop")
            return True
        if low <= trade.take_profit:
            _close(trade, trade.take_profit, ts, "target")
            return True
    return False


def _close(trade: Trade, price: float, ts: pd.Timestamp, reason: str) -> None:
    trade.exit_time = ts
    trade.exit_price = price
    trade.pnl = _pnl(trade, price)
    trade.reason = reason


def _pnl(trade: Trade, exit_price: float) -> float:
    # Simplified: per-pip PnL assumes 1 lot EURUSD-like ($10 / pip).
    # Real backtests should pass symbol info; this is a placeholder.
    diff = exit_price - trade.entry_price
    if trade.side == SignalType.SELL:
        diff = -diff
    return diff * 10_000  # ~pips × $10 equivalent for a 4-decimal pair
