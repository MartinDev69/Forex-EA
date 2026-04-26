"""Breakeven + trailing stop management.

Trades get a fixed SL/TP on entry, but once they move favorably we want to
protect profit. Two-stage policy:

  1. At +`breakeven_trigger_r` (default +1R), move SL to entry.
  2. At +`trail_start_r` (default +2R), start trailing SL `trail_distance_r`
     R behind the best price seen since entry.

R = the initial stop distance (|entry - initial_stop|). Using R-multiples
means the same policy works on EURUSD, XAUUSD, and indices without retuning.

Invariant: SL only ratchets in the favorable direction. A pullback never
loosens a stop, and the trailing stop never retreats past entry.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.strategies.base import SignalType

from .base import Order


@dataclass
class StopPolicy:
    breakeven_trigger_r: float = 1.0
    trail_start_r: float = 2.0
    trail_distance_r: float = 1.0


class StopManager:
    """Computes new SL values for open orders given recent bar extremes.

    Stateless w.r.t. its own attrs — peak tracking lives on the Order via
    `order.extra['peak_price']`, so multiple managers can cooperate and
    restarted processes can reconstruct state from the broker ticket.
    """

    def __init__(self, policy: StopPolicy | None = None) -> None:
        self.policy = policy or StopPolicy()

    def update_peak(self, order: Order, bar_high: float, bar_low: float) -> None:
        """Record the best favorable price reached since entry."""
        current = order.extra.get("peak_price")
        if order.side == SignalType.BUY:
            best = bar_high if current is None else max(current, bar_high)
        else:
            best = bar_low if current is None else min(current, bar_low)
        order.extra["peak_price"] = best

    def proposed_stop(self, order: Order) -> float | None:
        """Return a new SL if the policy wants to tighten it, else None.

        Caller is expected to have already invoked `update_peak` for the
        current bar.
        """
        initial_stop = order.extra.get("initial_stop_loss", order.stop_loss)
        risk = abs(order.entry_price - initial_stop)
        if risk <= 0:
            return None

        peak = order.extra.get("peak_price")
        if peak is None:
            return None

        if order.side == SignalType.BUY:
            gain_r = (peak - order.entry_price) / risk
        else:
            gain_r = (order.entry_price - peak) / risk

        new_sl = _target_stop(order, peak, risk, gain_r, self.policy)
        if new_sl is None:
            return None

        # Ratchet: only tighten, never loosen.
        if order.side == SignalType.BUY and new_sl > order.stop_loss:
            return new_sl
        if order.side == SignalType.SELL and new_sl < order.stop_loss:
            return new_sl
        return None


def _target_stop(order: Order, peak: float, risk: float, gain_r: float, pol: StopPolicy) -> float | None:
    # Float-precision cushion — matters at exact R multiples like 1.0 or 2.0.
    eps = 1e-9
    # Stage 2: trailing — kicks in after trail_start_r, takes precedence over BE
    # because it's always at least as tight as entry by construction.
    if gain_r + eps >= pol.trail_start_r:
        offset = pol.trail_distance_r * risk
        return peak - offset if order.side == SignalType.BUY else peak + offset

    # Stage 1: move to breakeven.
    if gain_r + eps >= pol.breakeven_trigger_r:
        return order.entry_price

    return None
