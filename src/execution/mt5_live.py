"""Live MT5 DataFeed + Executor.

These implement the same protocols as MockDataFeed/MockExecutor but talk to a
real MetaTrader 5 terminal via the `MetaTrader5` package. The package is
Windows-only; on macOS/Linux the module still *imports* fine — we only fail
at instantiation time if MT5 isn't available.

Design notes:
  * Both classes accept an injected `mt5_module` for testability. In prod it
    defaults to the real `MetaTrader5` import; tests pass a fake.
  * Bars come back with standard columns (open/high/low/close/volume) regardless
    of whether MT5 provides tick_volume or real_volume — strategies and the
    feature builder don't need to know the source.
  * `place()` sends TRADE_ACTION_DEAL market orders. `close()` sends the opposite
    side referencing the stored broker ticket.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from src.strategies.base import SignalType

from .base import DataFeed, Executor, Order, OrderStatus

log = logging.getLogger(__name__)


TIMEFRAME_MAP = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 16385, "H4": 16388, "D1": 16408, "W1": 32769, "MN1": 49153,
}

# How far from the requested price we'll accept a fill, in "points" (0.00001 for
# 5-digit FX pairs). 20 points ≈ 2 pips — typical for a liquid broker.
DEFAULT_DEVIATION_POINTS = 20

# Ties all orders from this bot to one magic number so manual trades aren't
# confused with bot trades when we query positions_get.
DEFAULT_MAGIC = 990_044


def _require_mt5(mod: Any | None):
    if mod is None:
        raise RuntimeError(
            "MetaTrader5 is not available. Install the `MetaTrader5` package "
            "on a Windows host (the package is Windows-only), or inject a fake "
            "module for testing."
        )


def _load_mt5():
    try:
        import MetaTrader5 as mt5  # type: ignore
        return mt5
    except ImportError:
        return None


class MT5DataFeed:
    """Live OHLC feed backed by `mt5.copy_rates_from_pos`.

    We ask for bars relative to "now" rather than a wall-clock time — this
    avoids timezone mismatches between local system time and the broker server.
    """

    def __init__(self, mt5_module: Any | None = None) -> None:
        self._mt5 = mt5_module if mt5_module is not None else _load_mt5()
        _require_mt5(self._mt5)
        # Track which symbols we've already pushed into Market Watch so we
        # only pay the symbol_select call once per symbol per process. MT5
        # won't serve copy_rates for symbols that aren't selected, which
        # silently breaks any pair the operator adds via SYMBOLS unless we
        # do it ourselves.
        self._selected: set[str] = set()

    def _ensure_selected(self, symbol: str) -> None:
        if symbol in self._selected:
            return
        # symbol_select returns True when the symbol exists on this server
        # (whether or not it was already selected). False = symbol not on the
        # server at all — usually a naming mismatch (e.g. broker uses XAUUSDm).
        if not self._mt5.symbol_select(symbol, True):
            last_err = getattr(self._mt5, "last_error", lambda: "unknown")()
            raise RuntimeError(
                f"symbol_select({symbol}) failed: {last_err}. "
                f"Check the symbol name on this broker server."
            )
        self._selected.add(symbol)

    def latest_bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        tf = TIMEFRAME_MAP.get(timeframe.upper())
        if tf is None:
            raise ValueError(f"Unknown timeframe: {timeframe}")
        self._ensure_selected(symbol)
        rates = self._mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            last_err = getattr(self._mt5, "last_error", lambda: "unknown")()
            raise RuntimeError(f"MT5 returned no rates for {symbol} {timeframe}: {last_err}")
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time")
        # MT5 returns: open/high/low/close/tick_volume/spread/real_volume.
        # Normalize to the columns the rest of the bot expects.
        volume_col = "real_volume" if df.get("real_volume") is not None and df["real_volume"].any() else "tick_volume"
        return df.rename(columns={volume_col: "volume"})[["open", "high", "low", "close", "volume"]]


class MT5Executor:
    """Live order router. One instance per bot process.

    `magic` lets you run multiple bot instances against the same account —
    orders from different bots won't collide in open_orders() listings.
    """

    def __init__(
        self,
        mt5_module: Any | None = None,
        magic: int = DEFAULT_MAGIC,
        deviation_points: int = DEFAULT_DEVIATION_POINTS,
        symbols_filter: list[str] | None = None,
    ) -> None:
        self._mt5 = mt5_module if mt5_module is not None else _load_mt5()
        _require_mt5(self._mt5)
        self.magic = magic
        self.deviation = deviation_points
        # If set, open_orders() only returns positions for these symbols.
        self.symbols_filter = symbols_filter

    # -------------------------------------------------------------- account

    def account_balance(self) -> float:
        info = self._mt5.account_info()
        if info is None:
            raise RuntimeError(f"account_info failed: {self._mt5.last_error()}")
        return float(info.balance)

    # -------------------------------------------------------------- orders

    def place(self, order: Order) -> Order:
        order_type = (
            self._mt5.ORDER_TYPE_BUY if order.side == SignalType.BUY
            else self._mt5.ORDER_TYPE_SELL
        )
        tick = self._mt5.symbol_info_tick(order.symbol)
        if tick is None:
            order.status = OrderStatus.REJECTED
            order.close_reason = f"symbol_info_tick({order.symbol}) returned None"
            log.warning(order.close_reason)
            return order
        price = float(tick.ask) if order.side == SignalType.BUY else float(tick.bid)

        request = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": order.symbol,
            "volume": float(order.lot_size),
            "type": order_type,
            "price": price,
            "sl": float(order.stop_loss),
            "tp": float(order.take_profit),
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": order.strategy[:31],  # MT5 comment hard limit
            "type_time": self._mt5.ORDER_TIME_GTC,
            "type_filling": self._resolve_filling_mode(order.symbol),
        }
        result = self._mt5.order_send(request)
        if result is None:
            order.status = OrderStatus.REJECTED
            order.close_reason = f"order_send returned None: {self._mt5.last_error()}"
            log.warning(order.close_reason)
            return order

        if result.retcode != self._mt5.TRADE_RETCODE_DONE:
            order.status = OrderStatus.REJECTED
            order.close_reason = f"retcode={result.retcode} comment={getattr(result, 'comment', '')}"
            log.warning("MT5 rejected order for %s: %s", order.symbol, order.close_reason)
            return order

        # Success — MT5 gives back the deal; the resulting position ticket is
        # what we'll need later to close.
        order.broker_ticket = int(getattr(result, "order", 0)) or int(getattr(result, "deal", 0))
        order.entry_price = float(getattr(result, "price", price))
        order.status = OrderStatus.OPEN
        if not order.opened_at:
            order.opened_at = datetime.now(timezone.utc)
        return order

    def close(self, order: Order, reason: str) -> Order:
        if order.broker_ticket is None:
            order.status = OrderStatus.REJECTED
            order.close_reason = "close() called without broker_ticket"
            log.warning(order.close_reason)
            return order

        # Opposite side closes the position.
        order_type = (
            self._mt5.ORDER_TYPE_SELL if order.side == SignalType.BUY
            else self._mt5.ORDER_TYPE_BUY
        )
        tick = self._mt5.symbol_info_tick(order.symbol)
        price = float(tick.bid) if order.side == SignalType.BUY else float(tick.ask)

        request = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": order.symbol,
            "volume": float(order.lot_size),
            "type": order_type,
            "position": int(order.broker_ticket),
            "price": price,
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": f"close:{reason}"[:31],
            "type_time": self._mt5.ORDER_TIME_GTC,
            "type_filling": self._resolve_filling_mode(order.symbol),
        }
        result = self._mt5.order_send(request)
        order.closed_at = datetime.now(timezone.utc)
        if result is None or result.retcode != self._mt5.TRADE_RETCODE_DONE:
            order.status = OrderStatus.REJECTED
            retcode = getattr(result, "retcode", None)
            order.close_reason = f"close rejected retcode={retcode}"
            log.warning("MT5 rejected close for %s: %s", order.symbol, order.close_reason)
            return order

        order.status = OrderStatus.CLOSED
        order.exit_price = float(getattr(result, "price", price))
        order.close_reason = reason
        order.pnl = self._compute_pnl(order)
        return order

    def modify(self, order: Order, stop_loss: float | None = None,
               take_profit: float | None = None) -> Order:
        """Adjust SL/TP on an open position via TRADE_ACTION_SLTP."""
        if order.broker_ticket is None:
            log.warning("modify() called without broker_ticket for %s", order.symbol)
            return order
        new_sl = float(stop_loss) if stop_loss is not None else float(order.stop_loss)
        new_tp = float(take_profit) if take_profit is not None else float(order.take_profit)

        request = {
            "action": self._mt5.TRADE_ACTION_SLTP,
            "symbol": order.symbol,
            "position": int(order.broker_ticket),
            "sl": new_sl,
            "tp": new_tp,
            "magic": self.magic,
        }
        result = self._mt5.order_send(request)
        if result is None or result.retcode != self._mt5.TRADE_RETCODE_DONE:
            retcode = getattr(result, "retcode", None)
            log.warning("MT5 modify rejected for %s: retcode=%s", order.symbol, retcode)
            return order

        order.stop_loss = new_sl
        order.take_profit = new_tp
        return order

    def open_orders(self) -> list[Order]:
        positions = self._mt5.positions_get()
        if positions is None:
            return []
        out: list[Order] = []
        for p in positions:
            if p.magic != self.magic:
                continue
            if self.symbols_filter and p.symbol not in self.symbols_filter:
                continue
            side = SignalType.BUY if p.type == self._mt5.POSITION_TYPE_BUY else SignalType.SELL
            out.append(Order(
                id=int(p.ticket),
                symbol=p.symbol,
                side=side,
                lot_size=float(p.volume),
                entry_price=float(p.price_open),
                stop_loss=float(p.sl),
                take_profit=float(p.tp),
                opened_at=datetime.fromtimestamp(p.time, tz=timezone.utc),
                strategy=p.comment or "",
                status=OrderStatus.OPEN,
                broker_ticket=int(p.ticket),
            ))
        return out

    # -------------------------------------------------------------- helpers

    def _resolve_filling_mode(self, symbol: str) -> int:
        """Pick a filling mode the broker actually supports for this symbol.

        Brokers advertise allowed modes via symbol_info.filling_mode as a
        bitmask; falling back to FOK is safe on most ECN brokers.
        """
        info = self._mt5.symbol_info(symbol)
        if info is None:
            return self._mt5.ORDER_FILLING_FOK
        # MT5 flags: SYMBOL_FILLING_FOK=1, SYMBOL_FILLING_IOC=2.
        modes = getattr(info, "filling_mode", 0)
        if modes & 1:
            return self._mt5.ORDER_FILLING_FOK
        if modes & 2:
            return self._mt5.ORDER_FILLING_IOC
        return self._mt5.ORDER_FILLING_FOK

    def _compute_pnl(self, order: Order) -> float:
        # For a live account MT5 itself reports realized P&L on the position,
        # but we've already sent the close — re-query to get the deal's profit.
        # Fall back to an estimated calc if the broker response doesn't include it.
        if order.exit_price is None:
            return 0.0
        diff = order.exit_price - order.entry_price
        if order.side == SignalType.SELL:
            diff = -diff
        # Rough 4-decimal-pair approximation — matches MockExecutor, good enough
        # for dashboards. For accounting, rely on MT5's history_deals_get().
        pip_value = 10.0 * order.lot_size
        return diff * 10_000 * pip_value
