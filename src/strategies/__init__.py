from .base import Signal, SignalType, Strategy
from .breakout import DonchianBreakoutStrategy
from .ma_crossover import MACrossoverStrategy
from .rsi_mean_reversion import RSIMeanReversionStrategy

__all__ = [
    "Signal",
    "SignalType",
    "Strategy",
    "MACrossoverStrategy",
    "RSIMeanReversionStrategy",
    "DonchianBreakoutStrategy",
]

STRATEGY_REGISTRY: dict[str, type[Strategy]] = {
    MACrossoverStrategy.name: MACrossoverStrategy,
    RSIMeanReversionStrategy.name: RSIMeanReversionStrategy,
    DonchianBreakoutStrategy.name: DonchianBreakoutStrategy,
}
