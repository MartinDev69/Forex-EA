"""RiskManager — gatekeeper for every trade signal.

Implements the portfolio-level safeguards from the guide:
  - max open trades
  - max daily loss (circuit breaker)
  - max total portfolio heat (sum of open risks)
  - minimum account balance
  - (optional) economic-calendar blackout around high-impact events
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Optional, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from src.econ_calendar.blackout import BlackoutChecker
    from src.correlation.throttle import OpenPosition, PortfolioThrottle
    from src.propfirm.guard import PropFirmGuard


@dataclass
class RiskLimits:
    risk_per_trade: float = 0.01
    max_open_trades: int = 5
    max_daily_loss_pct: float = 0.05
    max_portfolio_heat_pct: float = 0.06
    min_balance: float = 100.0


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    lot_size: float | None = None


@dataclass
class RiskState:
    open_trade_count: int = 0
    open_risk_pct: float = 0.0
    daily_pnl: float = 0.0
    # UTC date of the last daily_pnl reset. Stays None until the first
    # daily_reset_if_needed() call so we honour the RiskManager's
    # injected clock (tests pin "now" to a fixed UTC date that doesn't
    # match the host's local date). The previous default of date.today()
    # was *local* date, so on a non-UTC VPS the circuit breaker reset
    # at local midnight rather than UTC — typically a couple of hours
    # off from when the dashboard, propfirm guard, and daily summary
    # all rolled over, giving operators a brief "free loss" window.
    last_reset: date | None = None


class RiskManager:
    def __init__(
        self,
        limits: RiskLimits | None = None,
        blackout_checker: Optional["BlackoutChecker"] = None,
        portfolio_throttle: Optional["PortfolioThrottle"] = None,
        propfirm_guard: Optional["PropFirmGuard"] = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.limits = limits or RiskLimits()
        self.state = RiskState()
        self.blackout_checker = blackout_checker
        self.portfolio_throttle = portfolio_throttle
        # None = personal account, no challenge rules. When set, the guard
        # vetoes any signal that would breach daily/total DD or other firm
        # constraints, and observe() rolls the daily window each tick.
        self.propfirm_guard = propfirm_guard
        # Clock is injectable so tests can pin "now" without patching datetime globally.
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def daily_reset_if_needed(self) -> None:
        # UTC day boundary — matches PropFirmGuard, the dashboard's
        # TODAY P&L, and the bot's daily-summary digest. Without this,
        # `date.today()` (local) reset the daily-loss circuit breaker
        # at local midnight, which on a SAST VPS = 22:00 UTC the
        # previous day. Operators saw a ~2h window where the gate had
        # already reset but the dashboard still showed the prior day's
        # loss against today's budget.
        today = self._clock().date()
        if self.state.last_reset is None:
            # First call after process start — seed without zeroing
            # daily_pnl, so register_trade_closed pnls from earlier
            # in the same UTC day aren't lost on bot restart.
            self.state.last_reset = today
            return
        if today != self.state.last_reset:
            self.state.daily_pnl = 0.0
            self.state.last_reset = today

    def register_trade_opened(self, risk_pct: float) -> None:
        self.state.open_trade_count += 1
        self.state.open_risk_pct += risk_pct

    def register_trade_closed(self, risk_pct: float, pnl: float) -> None:
        self.state.open_trade_count = max(0, self.state.open_trade_count - 1)
        self.state.open_risk_pct = max(0.0, self.state.open_risk_pct - risk_pct)
        self.state.daily_pnl += pnl

    def evaluate(
        self,
        account_balance: float,
        stop_distance_pips: float,
        symbol: str,
        lot_sizer,
        side: str | None = None,
        open_positions: Sequence["OpenPosition"] | None = None,
        risk_multiplier: float = 1.0,
    ) -> RiskDecision:
        """Check portfolio rules, then compute lot size.

        `lot_sizer` is a callable with signature matching `lot_size_from_risk`.
        Passed in so this module stays unit-testable without importing
        position_sizing directly.

        `side` and `open_positions` are required to evaluate the correlation
        throttle. When either is missing the throttle is skipped (the rest of
        the gates still run) so callers from older code paths keep working.

        `risk_multiplier` scales `risk_per_trade` for this single decision —
        the allocator uses it to give challengers/probes a fraction of full
        risk. 0 = reject outright (cold variant).
        """
        self.daily_reset_if_needed()
        lim = self.limits

        if risk_multiplier <= 0:
            return RiskDecision(False, "allocator: variant has zero weight")

        effective_risk = lim.risk_per_trade * risk_multiplier

        if account_balance < lim.min_balance:
            return RiskDecision(False, f"balance {account_balance:.2f} < min {lim.min_balance:.2f}")

        if self.state.open_trade_count >= lim.max_open_trades:
            return RiskDecision(False, f"max open trades reached ({lim.max_open_trades})")

        if self.state.open_risk_pct + effective_risk > lim.max_portfolio_heat_pct:
            return RiskDecision(False, "portfolio heat limit would be exceeded")

        daily_loss_pct = -self.state.daily_pnl / account_balance if account_balance > 0 else 0
        if daily_loss_pct >= lim.max_daily_loss_pct:
            return RiskDecision(False, f"daily loss circuit breaker ({daily_loss_pct:.2%})")

        if self.blackout_checker is not None:
            event = self.blackout_checker.current_blackout(symbol, now=self._clock())
            if event is not None:
                return RiskDecision(
                    False,
                    f"calendar:{event.currency}:{event.title}",
                )

        if (
            self.portfolio_throttle is not None
            and side is not None
            and open_positions is not None
        ):
            decision = self.portfolio_throttle.decide(
                candidate_symbol=symbol,
                candidate_side=side,
                candidate_risk_pct=effective_risk,
                open_positions=list(open_positions),
            )
            if not decision.approved:
                return RiskDecision(False, decision.reason)

        lots = lot_sizer(
            account_balance=account_balance,
            risk_pct=effective_risk,
            stop_distance_pips=stop_distance_pips,
            symbol=symbol,
        )
        if lots <= 0:
            return RiskDecision(False, "computed lot size is zero")

        if self.propfirm_guard is not None:
            pf = self.propfirm_guard.check(
                account_balance=account_balance,
                signal_has_stop=stop_distance_pips > 0,
                candidate_lot=lots,
            )
            if not pf.approved:
                return RiskDecision(False, pf.reason)

        return RiskDecision(True, "approved", lot_size=lots)
