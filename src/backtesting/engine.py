"""Simple vectorized-ish event backtester.

Walks bar-by-bar, asks the strategy for a signal, enforces one open position
at a time, applies SL/TP exits on the next bar's high/low, and records equity.

Good enough for strategy sanity-checking. Swap in Backtrader for serious work —
see `notebooks/backtrader_example.ipynb` (to be created).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from src.risk.position_sizing import lot_size_from_risk, pip_size, pip_value
from src.strategies.base import Signal, SignalType, Strategy


@dataclass
class Trade:
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp | None
    side: SignalType
    symbol: str
    entry_price: float
    exit_price: float | None
    stop_loss: float
    take_profit: float
    lot_size: float
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
    symbol: str | None = None,
) -> BacktestResult:
    """Walk the dataframe, one bar at a time, and simulate the strategy.

    `symbol` defaults to `strategy.symbol` and drives per-pair pip size +
    pip value used in PnL accounting. Pass it explicitly to override (e.g.
    running a generic strategy on XAUUSD bars).
    """
    sym = symbol or strategy.symbol
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
                open_trade = _open_from_signal(signal, equity, risk_per_trade_pct, sym)

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


def _open_from_signal(signal: Signal, equity: float, risk_pct: float, symbol: str) -> Trade:
    stop = signal.stop_loss or 0.0
    stop_distance = abs(signal.price - stop) if stop else 0.0
    stop_pips = stop_distance / pip_size(symbol) if stop_distance > 0 else 0.0
    if stop_pips > 0:
        lots = lot_size_from_risk(
            account_balance=equity,
            risk_pct=risk_pct,
            stop_distance_pips=stop_pips,
            symbol=symbol,
        )
    else:
        # Strategy didn't set a stop — fall back to min lot so PnL isn't zero
        # but risk isn't runaway either. Strategies should always set stops.
        lots = 0.01
    return Trade(
        entry_time=signal.timestamp,
        exit_time=None,
        side=signal.type,
        symbol=symbol,
        entry_price=signal.price,
        exit_price=None,
        stop_loss=stop,
        take_profit=signal.take_profit or 0.0,
        lot_size=lots,
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
    diff = exit_price - trade.entry_price
    if trade.side == SignalType.SELL:
        diff = -diff
    pips = diff / pip_size(trade.symbol)
    return pips * pip_value(trade.symbol, trade.lot_size)
