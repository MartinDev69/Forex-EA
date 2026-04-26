"""Portfolio correlation throttle.

Decides whether a new candidate position would push correlated portfolio
heat past a configured ceiling. The intuition:

  Two BUYs on EURUSD and GBPUSD (corr ≈ 0.8) are not two independent bets —
  they're closer to one bet at 1.6× size. The throttle catches that.

For each open position (Y, side_Y, risk_Y):
  contribution = risk_Y * effective_corr
  effective_corr = +1 * corr(X, Y)  if same side
                 = -1 * corr(X, Y)  if opposite (hedge)

Hedging contributions are clipped at zero — we don't *reward* a hedge by
allowing more concentration elsewhere; we just don't penalize it.

The candidate is rejected when:
  (sum of positive contributions) + risk_per_trade > max_correlated_heat_pct
"""
from __future__ import annotations

from dataclasses import dataclass

from .store import CorrelationStore


@dataclass(frozen=True)
class OpenPosition:
    symbol: str
    side: str  # "BUY" or "SELL"
    risk_pct: float


@dataclass(frozen=True)
class ThrottlePolicy:
    enabled: bool = True
    max_correlated_heat_pct: float = 0.04
    # Below this magnitude we treat pairs as effectively uncorrelated. Avoids
    # the throttle nibbling on noise — 0.05 corr on 10 positions still sums.
    correlation_floor: float = 0.30
    # How to behave when a pair correlation is missing from the store: be
    # conservative (treat unknown as fully correlated) or permissive (treat
    # as zero). Default conservative.
    unknown_pair_correlation: float = 1.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ThrottlePolicy":
        import os
        e = env if env is not None else os.environ
        return cls(
            enabled=e.get("CORRELATION_ENABLED", "1").strip() not in ("0", "false", "False", ""),
            max_correlated_heat_pct=float(e.get("CORRELATION_MAX_HEAT_PCT", "0.04")),
            correlation_floor=float(e.get("CORRELATION_FLOOR", "0.30")),
            unknown_pair_correlation=float(e.get("CORRELATION_UNKNOWN", "1.0")),
        )


@dataclass(frozen=True)
class ThrottleDecision:
    approved: bool
    effective_heat: float
    reason: str
    contributors: tuple[tuple[str, float], ...] = ()  # (symbol, contribution) per position


class PortfolioThrottle:
    def __init__(
        self,
        store: CorrelationStore,
        policy: ThrottlePolicy | None = None,
    ) -> None:
        self.store = store
        self.policy = policy or ThrottlePolicy()

    def decide(
        self,
        candidate_symbol: str,
        candidate_side: str,
        candidate_risk_pct: float,
        open_positions: list[OpenPosition],
    ) -> ThrottleDecision:
        pol = self.policy
        if not pol.enabled or not open_positions:
            return ThrottleDecision(True, 0.0, "throttle inactive")

        contributions: list[tuple[str, float]] = []
        for pos in open_positions:
            if pos.symbol == candidate_symbol:
                # Adding to an existing position: full self-correlation, no
                # need to hit the store.
                corr = 1.0 if pos.side == candidate_side else -1.0
            else:
                raw = self.store.pair(candidate_symbol, pos.symbol)
                if raw is None:
                    raw = pol.unknown_pair_correlation
                corr = raw if pos.side == candidate_side else -raw

            # Clip at zero — hedges (negative effective) shouldn't license
            # more concentration on the long side.
            if abs(corr) < pol.correlation_floor:
                corr = 0.0
            contrib = max(0.0, pos.risk_pct * corr)
            if contrib > 0:
                contributions.append((pos.symbol, contrib))

        effective = sum(c for _, c in contributions)
        projected = effective + candidate_risk_pct

        if projected > pol.max_correlated_heat_pct:
            top = sorted(contributions, key=lambda t: -t[1])[:3]
            top_str = ", ".join(f"{s}:{v:.2%}" for s, v in top)
            return ThrottleDecision(
                approved=False,
                effective_heat=effective,
                reason=(
                    f"correlated heat {projected:.2%} > limit "
                    f"{pol.max_correlated_heat_pct:.2%} (top: {top_str})"
                ),
                contributors=tuple(contributions),
            )

        return ThrottleDecision(
            approved=True,
            effective_heat=effective,
            reason=f"correlated heat {projected:.2%} within limit",
            contributors=tuple(contributions),
        )
