"""Breakeven + trailing stop policy."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.execution.base import Order, OrderStatus
from src.execution.stops import StopManager, StopPolicy
from src.strategies.base import SignalType


def _order(side: SignalType, entry: float, initial_sl: float) -> Order:
    o = Order(
        id=1, symbol="EURUSD", side=side, lot_size=0.1,
        entry_price=entry, stop_loss=initial_sl, take_profit=entry + (entry - initial_sl) * 5,
        opened_at=datetime.now(timezone.utc), strategy="test",
        status=OrderStatus.OPEN,
    )
    o.extra["initial_stop_loss"] = initial_sl
    return o


# ---------------------------------------------------------------- breakeven


def test_breakeven_triggers_at_one_r_for_buy():
    o = _order(SignalType.BUY, entry=1.1000, initial_sl=1.0950)  # 50-pip risk
    mgr = StopManager(StopPolicy(breakeven_trigger_r=1.0, trail_start_r=99))

    mgr.update_peak(o, bar_high=1.1050, bar_low=1.1020)
    assert mgr.proposed_stop(o) == pytest.approx(1.1000)


def test_breakeven_triggers_at_one_r_for_sell():
    o = _order(SignalType.SELL, entry=1.1000, initial_sl=1.1050)  # 50-pip risk
    mgr = StopManager(StopPolicy(breakeven_trigger_r=1.0, trail_start_r=99))

    mgr.update_peak(o, bar_high=1.0980, bar_low=1.0950)
    assert mgr.proposed_stop(o) == pytest.approx(1.1000)


def test_no_move_when_below_trigger():
    o = _order(SignalType.BUY, entry=1.1000, initial_sl=1.0950)
    mgr = StopManager(StopPolicy(breakeven_trigger_r=1.0))

    mgr.update_peak(o, bar_high=1.1040, bar_low=1.1000)  # +0.8R
    assert mgr.proposed_stop(o) is None


# ---------------------------------------------------------------- trailing


def test_trail_buy_follows_peak_at_one_r_distance():
    o = _order(SignalType.BUY, entry=1.1000, initial_sl=1.0950)
    mgr = StopManager(StopPolicy(breakeven_trigger_r=1.0, trail_start_r=2.0, trail_distance_r=1.0))

    # Peak at +3R = 1.1150; SL should trail to 1.1100 (peak - 1R).
    mgr.update_peak(o, bar_high=1.1150, bar_low=1.1100)
    assert mgr.proposed_stop(o) == pytest.approx(1.1100)


def test_trail_sell_mirrors_buy():
    o = _order(SignalType.SELL, entry=1.1000, initial_sl=1.1050)
    mgr = StopManager(StopPolicy(breakeven_trigger_r=1.0, trail_start_r=2.0, trail_distance_r=1.0))

    # Peak (low) at -3R = 1.0850; SL should trail to 1.0900.
    mgr.update_peak(o, bar_high=1.0900, bar_low=1.0850)
    assert mgr.proposed_stop(o) == pytest.approx(1.0900)


def test_sl_ratchets_and_never_loosens():
    o = _order(SignalType.BUY, entry=1.1000, initial_sl=1.0950)
    mgr = StopManager(StopPolicy(breakeven_trigger_r=1.0, trail_start_r=2.0, trail_distance_r=1.0))

    mgr.update_peak(o, bar_high=1.1150, bar_low=1.1100)
    new_sl = mgr.proposed_stop(o)
    assert new_sl is not None
    o.stop_loss = new_sl  # bot would do this via executor.modify

    # Next bar pulls back — peak doesn't retreat, so proposed SL stays at 1.1100.
    # Manager should return None because it's not tighter than current.
    mgr.update_peak(o, bar_high=1.1120, bar_low=1.1080)
    assert mgr.proposed_stop(o) is None


def test_peak_tracks_best_across_calls():
    o = _order(SignalType.BUY, entry=1.1000, initial_sl=1.0950)
    mgr = StopManager()

    mgr.update_peak(o, bar_high=1.1080, bar_low=1.1000)
    mgr.update_peak(o, bar_high=1.1040, bar_low=1.0990)
    assert o.extra["peak_price"] == pytest.approx(1.1080)


def test_unprofitable_position_leaves_stop_alone():
    o = _order(SignalType.BUY, entry=1.1000, initial_sl=1.0950)
    mgr = StopManager()

    mgr.update_peak(o, bar_high=1.0990, bar_low=1.0960)
    assert mgr.proposed_stop(o) is None


def test_zero_risk_order_returns_none():
    o = _order(SignalType.BUY, entry=1.1000, initial_sl=1.1000)
    mgr = StopManager()

    mgr.update_peak(o, bar_high=1.1100, bar_low=1.1000)
    assert mgr.proposed_stop(o) is None


def test_trail_stage_takes_precedence_over_breakeven():
    # At +2.5R we expect trailing (peak - 1R), not just breakeven.
    o = _order(SignalType.BUY, entry=1.1000, initial_sl=1.0950)  # 50-pip R
    mgr = StopManager(StopPolicy(breakeven_trigger_r=1.0, trail_start_r=2.0, trail_distance_r=1.0))

    mgr.update_peak(o, bar_high=1.1125, bar_low=1.1050)  # +2.5R
    # Trailing SL = 1.1125 - 1R (0.0050) = 1.1075, well above entry 1.1000.
    assert mgr.proposed_stop(o) == pytest.approx(1.1075)
