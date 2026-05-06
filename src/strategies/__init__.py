from .base import Signal, SignalType, Strategy
from .breakout import DonchianBreakoutStrategy
from .ma_crossover import MACrossoverStrategy
from .more import (
    ADXBreakoutStrategy,
    BollingerBounceStrategy,
    BollingerSqueezeStrategy,
    EMAPullbackStrategy,
    EngulfingPatternStrategy,
    InsideBarBreakoutStrategy,
    MACDCrossStrategy,
    StochasticReversalStrategy,
    TripleMAStrategy,
)
from .rsi_mean_reversion import RSIMeanReversionStrategy

__all__ = [
    "Signal",
    "SignalType",
    "Strategy",
    "MACrossoverStrategy",
    "RSIMeanReversionStrategy",
    "DonchianBreakoutStrategy",
    "MACDCrossStrategy",
    "BollingerBounceStrategy",
    "BollingerSqueezeStrategy",
    "StochasticReversalStrategy",
    "TripleMAStrategy",
    "InsideBarBreakoutStrategy",
    "EngulfingPatternStrategy",
    "EMAPullbackStrategy",
    "ADXBreakoutStrategy",
]

STRATEGY_REGISTRY: dict[str, type[Strategy]] = {
    MACrossoverStrategy.name: MACrossoverStrategy,
    RSIMeanReversionStrategy.name: RSIMeanReversionStrategy,
    DonchianBreakoutStrategy.name: DonchianBreakoutStrategy,
    MACDCrossStrategy.name: MACDCrossStrategy,
    BollingerBounceStrategy.name: BollingerBounceStrategy,
    BollingerSqueezeStrategy.name: BollingerSqueezeStrategy,
    StochasticReversalStrategy.name: StochasticReversalStrategy,
    TripleMAStrategy.name: TripleMAStrategy,
    InsideBarBreakoutStrategy.name: InsideBarBreakoutStrategy,
    EngulfingPatternStrategy.name: EngulfingPatternStrategy,
    EMAPullbackStrategy.name: EMAPullbackStrategy,
    ADXBreakoutStrategy.name: ADXBreakoutStrategy,
}
