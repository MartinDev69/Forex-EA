"""Champion-challenger allocator.

Given a list of `StrategyScore` objects (one per active strategy, all
trading the same symbol), pick the champion (best avg-R with enough
samples), and grade the rest:

    champion    -> weight 1.0   — best performer, full risk
    challenger  -> weight 0.5   — close to champion, keep collecting data
    probe       -> weight 0.1   — well behind, but we keep a tiny stake
                                  so it can prove itself if conditions shift
    cold        -> weight 0.0   — not enough data; no live risk yet

Per-symbol grouping matters: champions of different symbols don't compete
with each other. The output is per-(strategy, symbol) so the bot can apply
the weight as a risk multiplier when it sees a signal from that pair.

Pure function — no I/O. The caller owns persistence and refresh cadence.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from .score import StrategyScore


@dataclass(frozen=True)
class AllocatorPolicy:
    # Below this many closed trades for a (strategy, symbol), it stays cold
    # (weight 0). Tunable so a brand-new strategy doesn't hog risk on noise.
    min_samples: int = 15
    # The challenger must be within this avg-R of the champion to keep
    # mid weight. Wider = more variants run at meaningful size.
    challenger_tolerance: float = 0.20
    # Weights for each role.
    champion_weight: float = 1.0
    challenger_weight: float = 0.5
    probe_weight: float = 0.1
    # If even the champion's avg-R is below this, *nothing* trades —
    # the whole symbol is in "all variants underwater" mode.
    floor_avg_r: float = -0.10

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "AllocatorPolicy":
        e = env if env is not None else os.environ
        return cls(
            min_samples=int(e.get("ALLOCATOR_MIN_SAMPLES", "15")),
            challenger_tolerance=float(e.get("ALLOCATOR_CHALLENGER_TOLERANCE", "0.20")),
            champion_weight=float(e.get("ALLOCATOR_CHAMPION_WEIGHT", "1.0")),
            challenger_weight=float(e.get("ALLOCATOR_CHALLENGER_WEIGHT", "0.5")),
            probe_weight=float(e.get("ALLOCATOR_PROBE_WEIGHT", "0.1")),
            floor_avg_r=float(e.get("ALLOCATOR_FLOOR_AVG_R", "-0.10")),
        )


@dataclass(frozen=True)
class Allocation:
    strategy: str
    symbol: str
    role: str          # 'champion' | 'challenger' | 'probe' | 'cold'
    weight: float
    sample_size: int
    avg_r: float
    win_rate: float
    note: str
    updated_at: str    # ISO-8601 UTC

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "role": self.role,
            "weight": self.weight,
            "sample_size": self.sample_size,
            "avg_r": self.avg_r,
            "win_rate": self.win_rate,
            "note": self.note,
            "updated_at": self.updated_at,
        }


class ChampionChallengerAllocator:
    def __init__(self, policy: AllocatorPolicy | None = None) -> None:
        self.policy = policy or AllocatorPolicy()

    def allocate(self, scores: list[StrategyScore]) -> list[Allocation]:
        """Group scores by symbol, then crown a champion per group."""
        by_symbol: dict[str, list[StrategyScore]] = {}
        for s in scores:
            by_symbol.setdefault(s.symbol, []).append(s)

        now = datetime.now(timezone.utc).isoformat()
        out: list[Allocation] = []
        for symbol, group in by_symbol.items():
            out.extend(self._allocate_symbol(symbol, group, now))
        # Stable order so the dashboard doesn't shuffle on every refresh.
        out.sort(key=lambda a: (a.symbol, a.strategy))
        return out

    def _allocate_symbol(
        self,
        symbol: str,
        group: list[StrategyScore],
        now: str,
    ) -> list[Allocation]:
        p = self.policy
        # Eligible = enough samples to have an opinion. Cold variants stay
        # at weight 0 but we still emit a row so the UI shows them.
        eligible = [s for s in group if s.sample_size >= p.min_samples]
        cold = [s for s in group if s.sample_size < p.min_samples]

        out: list[Allocation] = []
        for s in cold:
            out.append(_make(s, "cold", 0.0,
                             f"need {p.min_samples - s.sample_size} more closed trades", now))

        if not eligible:
            return out

        champion = max(eligible, key=lambda s: s.avg_r)

        # Symbol-wide kill switch: if even the best variant is bleeding,
        # nothing trades live. They all drop to probe weight at most so
        # data keeps flowing for the eventual recovery check.
        if champion.avg_r < p.floor_avg_r:
            for s in eligible:
                out.append(_make(s, "probe", p.probe_weight,
                                 f"all variants below floor ({p.floor_avg_r:+.2f}R) — probing only", now))
            return out

        for s in eligible:
            if s is champion:
                out.append(_make(s, "champion", p.champion_weight,
                                 f"best of {len(eligible)} variants on {symbol}", now))
                continue
            gap = champion.avg_r - s.avg_r
            if gap <= p.challenger_tolerance:
                out.append(_make(s, "challenger", p.challenger_weight,
                                 f"within {p.challenger_tolerance:.2f}R of champion (gap {gap:.2f}R)", now))
            else:
                out.append(_make(s, "probe", p.probe_weight,
                                 f"trailing champion by {gap:.2f}R — probe only", now))
        return out


def _make(s: StrategyScore, role: str, weight: float, note: str, now: str) -> Allocation:
    return Allocation(
        strategy=s.strategy,
        symbol=s.symbol,
        role=role,
        weight=weight,
        sample_size=s.sample_size,
        avg_r=s.avg_r,
        win_rate=s.win_rate,
        note=note,
        updated_at=now,
    )
