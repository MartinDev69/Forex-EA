"""PropFirmGuard — the gate that enforces challenge rules.

Two surface methods:
  * `observe(account_balance)` — called every tick. Rolls daily window,
    bumps peak, flips kill flags when limits breach.
  * `check(account_balance, signal_has_stop, candidate_lot)` — called per
    candidate trade. Returns approve/reject + reason.

Decoupled from the bot — no imports back into bot.py. The risk manager
holds the guard and calls both methods in the right places.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable

from .policy import PropFirmPolicy
from .store import PropFirmState, PropFirmStore

log = logging.getLogger(__name__)


@dataclass
class PropFirmDecision:
    approved: bool
    reason: str


class PropFirmGuard:
    def __init__(
        self,
        policy: PropFirmPolicy,
        store: PropFirmStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ----------------------------------------------------------------- observe

    def observe(self, account_balance: float) -> PropFirmState:
        """Update peak / daily window / kill flags from the latest balance.

        Called per tick. Cheap — one read, at most one write.
        """
        now = self._clock()
        today = now.date()
        state = self.store.read()
        if state is None:
            state = self.store.initialize(self.policy.initial_balance, today, now)

        dirty = False

        # Day rollover — start fresh window, clear today's kill flag.
        if state.daily_start_date != today:
            state.daily_start_date = today
            state.daily_start_equity = account_balance
            state.killed_today = False
            dirty = True

        # Peak track (only matters when drawdown_from_peak is set).
        if account_balance > state.peak_equity:
            state.peak_equity = account_balance
            dirty = True

        # Daily DD — flip killed_today if we've crossed the line. Don't
        # re-flip once already set; the operator may have manually flat-lined.
        if not state.killed_today:
            daily_loss = state.daily_start_equity - account_balance
            if daily_loss > 0 and daily_loss >= self.policy.daily_loss_amount:
                state.killed_today = True
                state.killed_reason = (
                    f"daily DD {daily_loss / state.daily_start_equity:.2%}"
                    f" ≥ {self.policy.max_daily_loss_pct:.2%}"
                )
                dirty = True
                log.error("propfirm: daily DD breached: %s", state.killed_reason)

        # Total DD — irreversible kill.
        if not state.killed_permanently:
            baseline = (
                max(state.peak_equity, state.initial_balance)
                if self.policy.drawdown_from_peak
                else state.initial_balance
            )
            total_loss = baseline - account_balance
            if total_loss > 0 and total_loss >= self.policy.max_drawdown_amount:
                state.killed_permanently = True
                state.killed_reason = (
                    f"total DD {total_loss / baseline:.2%}"
                    f" ≥ {self.policy.max_total_drawdown_pct:.2%}"
                )
                dirty = True
                log.error("propfirm: total DD breached: %s", state.killed_reason)

        if dirty:
            state.updated_at = now
            self.store.write(state)
        return state

    # ----------------------------------------------------------------- check

    def check(
        self,
        *,
        account_balance: float,
        signal_has_stop: bool,
        candidate_lot: float,
    ) -> PropFirmDecision:
        """Pre-trade gate. Caller must have just called `observe()` so state is fresh."""
        state = self.store.read()
        if state is None:
            # Shouldn't happen — observe() initializes. Defensive: approve.
            return PropFirmDecision(True, "propfirm: no state yet")

        if state.killed_permanently:
            return PropFirmDecision(False, f"propfirm: account killed — {state.killed_reason or 'total DD'}")
        if state.killed_today:
            return PropFirmDecision(False, f"propfirm: daily limit hit — {state.killed_reason or 'daily DD'}")

        if self.policy.require_stop_loss and not signal_has_stop:
            return PropFirmDecision(False, "propfirm: stop loss required for every trade")

        if self.policy.max_lot_size is not None and candidate_lot > self.policy.max_lot_size:
            return PropFirmDecision(
                False,
                f"propfirm: lot {candidate_lot:.2f} > cap {self.policy.max_lot_size:.2f}",
            )

        return PropFirmDecision(True, "approved")

    # ----------------------------------------------------------------- bookkeeping

    def note_trade_opened(self) -> None:
        """Bump trading_days_count when this is the first trade of a new day."""
        now = self._clock()
        today = now.date()
        state = self.store.read()
        if state is None:
            return
        if state.last_trading_date != today:
            state.trading_days_count += 1
            state.last_trading_date = today
            state.updated_at = now
            self.store.write(state)

    # ----------------------------------------------------------------- read

    def progress(self, account_balance: float) -> dict:
        """Snapshot for the dashboard. No side-effects."""
        state = self.store.read()
        if state is None:
            return {
                "preset": self.policy.preset_name,
                "initialized": False,
            }
        baseline = (
            max(state.peak_equity, state.initial_balance)
            if self.policy.drawdown_from_peak
            else state.initial_balance
        )
        daily_loss = max(0.0, state.daily_start_equity - account_balance)
        total_loss = max(0.0, baseline - account_balance)
        profit = account_balance - state.initial_balance
        return {
            "preset": self.policy.preset_name,
            "initialized": True,
            "initial_balance": state.initial_balance,
            "current_equity": account_balance,
            "peak_equity": state.peak_equity,
            "profit_amount": profit,
            "profit_target_amount": self.policy.profit_target_amount,
            "profit_target_pct": self.policy.profit_target_pct,
            "profit_remaining_amount": max(0.0, self.policy.profit_target_amount - profit),
            "daily_start_equity": state.daily_start_equity,
            "daily_loss_amount": daily_loss,
            "daily_loss_limit_amount": self.policy.daily_loss_amount,
            "daily_loss_pct": (daily_loss / state.daily_start_equity) if state.daily_start_equity else 0.0,
            "max_daily_loss_pct": self.policy.max_daily_loss_pct,
            "total_drawdown_amount": total_loss,
            "total_drawdown_limit_amount": self.policy.max_drawdown_amount,
            "total_drawdown_pct": (total_loss / baseline) if baseline else 0.0,
            "max_total_drawdown_pct": self.policy.max_total_drawdown_pct,
            "drawdown_from_peak": self.policy.drawdown_from_peak,
            "trading_days_count": state.trading_days_count,
            "min_trading_days": self.policy.min_trading_days,
            "killed_today": state.killed_today,
            "killed_permanently": state.killed_permanently,
            "killed_reason": state.killed_reason,
            "max_lot_size": self.policy.max_lot_size,
            "require_stop_loss": self.policy.require_stop_loss,
            "updated_at": state.updated_at.isoformat(),
        }
