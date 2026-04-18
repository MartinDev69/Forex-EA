"""RiskManager — gatekeeper for every trade signal.

Implements the portfolio-level safeguards from the guide:
  - max open trades
  - max daily loss (circuit breaker)
  - max total portfolio heat (sum of open risks)
  - minimum account balance
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


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
    last_reset: date = field(default_factory=date.today)


class RiskManager:
    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()
        self.state = RiskState()

    def daily_reset_if_needed(self) -> None:
        today = date.today()
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
    ) -> RiskDecision:
        """Check portfolio rules, then compute lot size.

        `lot_sizer` is a callable with signature matching `lot_size_from_risk`.
        Passed in so this module stays unit-testable without importing
        position_sizing directly.
        """
        self.daily_reset_if_needed()
        lim = self.limits

        if account_balance < lim.min_balance:
            return RiskDecision(False, f"balance {account_balance:.2f} < min {lim.min_balance:.2f}")

        if self.state.open_trade_count >= lim.max_open_trades:
            return RiskDecision(False, f"max open trades reached ({lim.max_open_trades})")

        if self.state.open_risk_pct + lim.risk_per_trade > lim.max_portfolio_heat_pct:
            return RiskDecision(False, "portfolio heat limit would be exceeded")

        daily_loss_pct = -self.state.daily_pnl / account_balance if account_balance > 0 else 0
        if daily_loss_pct >= lim.max_daily_loss_pct:
            return RiskDecision(False, f"daily loss circuit breaker ({daily_loss_pct:.2%})")

        lots = lot_sizer(
            account_balance=account_balance,
            risk_pct=lim.risk_per_trade,
            stop_distance_pips=stop_distance_pips,
            symbol=symbol,
        )
        if lots <= 0:
            return RiskDecision(False, "computed lot size is zero")

        return RiskDecision(True, "approved", lot_size=lots)
