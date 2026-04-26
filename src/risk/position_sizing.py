"""Position sizing: convert risk% + stop distance → lot size."""
from __future__ import annotations


def pip_size(symbol: str) -> float:
    """Price delta corresponding to one pip for `symbol`.

    JPY pairs quote to 0.01; metals to 0.1; everything else to 0.0001.
    Matches what MT5 reports via `symbol_info.point * 10` for most brokers.
    """
    s = symbol.upper()
    if s in {"XAUUSD", "GOLD", "XAGUSD", "SILVER"}:
        return 0.1
    if s.endswith("JPY"):
        return 0.01
    return 0.0001


def pip_value(symbol: str, lot_size: float = 1.0) -> float:
    """Approximate USD pip value per lot for common pairs.

    Standard lot (100_000 units):
      - *USD quote (EURUSD, GBPUSD, AUDUSD, NZDUSD): ~$10/pip
      - JPY quote (USDJPY, EURJPY): ~$9/pip (depends on JPY rate — this is nominal)
      - XAUUSD (gold): $10 per $1 move per standard lot

    For production use, fetch live tick values from MT5 via
    `mt5.symbol_info_tick()` instead of this lookup.
    """
    s = symbol.upper()
    if s in {"XAUUSD", "GOLD"}:
        return 10.0 * lot_size  # $1 move × 100 oz / lot = $100 (but 1 pip = $0.10 × 100 = $10)
    if s.endswith("JPY"):
        return 9.0 * lot_size
    return 10.0 * lot_size


def lot_size_from_risk(
    account_balance: float,
    risk_pct: float,
    stop_distance_pips: float,
    symbol: str,
    min_lot: float = 0.01,
    max_lot: float = 100.0,
    lot_step: float = 0.01,
) -> float:
    """Return lot size that risks `risk_pct` of balance given stop distance.

    risk_amount = balance × risk_pct
    lots = risk_amount / (stop_distance_pips × pip_value_per_lot)
    """
    if stop_distance_pips <= 0:
        raise ValueError("stop_distance_pips must be > 0")
    if not 0 < risk_pct < 1:
        raise ValueError("risk_pct must be between 0 and 1 (e.g. 0.01 for 1%)")

    risk_amount = account_balance * risk_pct
    per_lot_risk = stop_distance_pips * pip_value(symbol)
    raw_lots = risk_amount / per_lot_risk

    stepped = round(raw_lots / lot_step) * lot_step
    return max(min_lot, min(max_lot, round(stepped, 2)))
