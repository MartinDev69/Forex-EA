"""Broker presets.

Every broker here uses MetaTrader 5 under the hood — switching is just a matter
of pointing the client at a different `server` string. Servers change as brokers
add/remove load-balanced endpoints; the values below are common 2024-2026 ones.
Users can always type a custom server string if their broker isn't listed or
their account lives on a different server than the preset suggests.

For Deriv users: use the MT5 account servers below. Deriv's native WebSocket API
(synthetic indices, Deriv-specific order types) is a separate integration and
not wired into this bot.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BrokerPreset:
    id: str
    display_name: str
    # Typical MT5 server strings. Brokers add/retire servers over time; the user's
    # account email has the authoritative one.
    servers: tuple[str, ...]
    # Common Windows install path. Blank if it's the default MT5 terminal.
    mt5_path_hint: str = ""
    notes: str = ""


PRESETS: tuple[BrokerPreset, ...] = (
    BrokerPreset(
        id="exness",
        display_name="Exness",
        servers=(
            "Exness-MT5Real",
            "Exness-MT5Real2",
            "Exness-MT5Real4",
            "Exness-MT5Real5",
            "Exness-MT5Real8",
            "Exness-MT5Trial",
            "Exness-MT5Trial4",
            "Exness-MT5Trial6",
        ),
        mt5_path_hint=r"C:\Program Files\Exness MetaTrader 5\terminal64.exe",
    ),
    BrokerPreset(
        id="xm",
        display_name="XM Global",
        servers=(
            "XMGlobal-MT5",
            "XMGlobal-MT5 2",
            "XMGlobal-MT5 3",
            "XMGlobal-MT5 4",
            "XMGlobal-Demo 3",
        ),
        mt5_path_hint=r"C:\Program Files\XM Global MT5\terminal64.exe",
    ),
    BrokerPreset(
        id="deriv_mt5",
        display_name="Deriv (MT5)",
        servers=(
            "DerivSVG-Server",
            "DerivSVG-Server-02",
            "DerivSVG-Server-03",
            "Deriv-Demo",
        ),
        mt5_path_hint=r"C:\Program Files\Deriv MT5\terminal64.exe",
        notes="For Deriv synthetic indices via the native WebSocket API, use a separate adapter — not supported here.",
    ),
    BrokerPreset(
        id="icmarkets",
        display_name="IC Markets",
        servers=(
            "ICMarkets-Live01",
            "ICMarkets-Live02",
            "ICMarkets-Live03",
            "ICMarkets-Live04",
            "ICMarkets-Live05",
            "ICMarkets-Live07",
            "ICMarketsSC-Live",
            "ICMarkets-Demo",
        ),
        mt5_path_hint=r"C:\Program Files\IC Markets Global MetaTrader 5\terminal64.exe",
    ),
    BrokerPreset(
        id="fbs",
        display_name="FBS",
        servers=(
            "FBS-Real",
            "FBS-Real-2",
            "FBS-Real-3",
            "FBS-Demo",
        ),
        mt5_path_hint=r"C:\Program Files\FBS MetaTrader 5\terminal64.exe",
    ),
    BrokerPreset(
        id="pepperstone",
        display_name="Pepperstone",
        servers=(
            "Pepperstone-MT5-01",
            "Pepperstone-MT5-02",
            "Pepperstone-Demo",
            "Pepperstone-EDGE01",
        ),
        mt5_path_hint=r"C:\Program Files\Pepperstone MetaTrader 5\terminal64.exe",
    ),
    BrokerPreset(
        id="custom",
        display_name="Custom / Other",
        servers=(),
        mt5_path_hint="",
        notes="Enter any MT5 server string manually — works with any MT5 broker.",
    ),
)


PRESET_BY_ID: dict[str, BrokerPreset] = {p.id: p for p in PRESETS}


def as_dicts() -> list[dict]:
    """JSON-friendly list for the frontend dropdown."""
    return [
        {
            "id": p.id,
            "display_name": p.display_name,
            "servers": list(p.servers),
            "mt5_path_hint": p.mt5_path_hint,
            "notes": p.notes,
        }
        for p in PRESETS
    ]
