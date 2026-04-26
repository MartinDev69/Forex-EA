"""PropFirmPolicy — the rules of the challenge.

Presets cover the headline rules of the major firms as of 2026. Operators
override individual values via env vars; `PROPFIRM_PRESET=custom` skips the
preset and reads everything from the env directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PropFirmPolicy:
    initial_balance: float
    max_daily_loss_pct: float       # e.g. 0.05 = 5% of daily-start equity
    max_total_drawdown_pct: float   # e.g. 0.10 = 10% drawdown limit
    profit_target_pct: float        # e.g. 0.08 = 8% to pass Phase 1
    min_trading_days: int           # operator-facing only — bot doesn't gate trades on it
    max_lot_size: float | None      # hard cap; None = no cap
    require_stop_loss: bool
    # FTMO-style "max loss from initial" vs trailing-from-peak. Different firms
    # use different conventions — store both possibilities and pick at check time.
    drawdown_from_peak: bool
    # Operator-facing label for the dashboard.
    preset_name: str = "custom"

    @property
    def daily_loss_amount(self) -> float:
        return self.initial_balance * self.max_daily_loss_pct

    @property
    def max_drawdown_amount(self) -> float:
        return self.initial_balance * self.max_total_drawdown_pct

    @property
    def profit_target_amount(self) -> float:
        return self.initial_balance * self.profit_target_pct


# Headline rules per firm, normalized. Operators verify in their challenge
# account dashboard before relying on these.
PRESETS: dict[str, dict] = {
    "ftmo": dict(
        # https://ftmo.com/en/account-types/
        max_daily_loss_pct=0.05,
        max_total_drawdown_pct=0.10,
        profit_target_pct=0.10,
        min_trading_days=4,
        max_lot_size=None,
        require_stop_loss=False,  # FTMO doesn't strictly require, but bot-side default is on
        drawdown_from_peak=False,
        preset_name="ftmo",
    ),
    "fundednext": dict(
        # https://fundednext.com/evaluation
        max_daily_loss_pct=0.05,
        max_total_drawdown_pct=0.10,
        profit_target_pct=0.10,
        min_trading_days=5,
        max_lot_size=None,
        require_stop_loss=False,
        drawdown_from_peak=True,
        preset_name="fundednext",
    ),
    "the5ers": dict(
        # https://the5ers.com — high-stakes challenge baseline.
        max_daily_loss_pct=0.04,
        max_total_drawdown_pct=0.04,
        profit_target_pct=0.06,
        min_trading_days=0,
        max_lot_size=None,
        require_stop_loss=True,
        drawdown_from_peak=True,
        preset_name="the5ers",
    ),
    "ftuk": dict(
        # https://ftuk.com — instant-funded baseline.
        max_daily_loss_pct=0.05,
        max_total_drawdown_pct=0.06,
        profit_target_pct=0.10,
        min_trading_days=0,
        max_lot_size=None,
        require_stop_loss=True,
        drawdown_from_peak=False,
        preset_name="ftuk",
    ),
}


def policy_from_env(env: dict | None = None) -> PropFirmPolicy:
    """Resolve policy from env. Preset values fill defaults; explicit env vars override."""
    e = env if env is not None else os.environ
    preset_id = e.get("PROPFIRM_PRESET", "custom").strip().lower()
    base = PRESETS.get(preset_id, {}).copy() if preset_id != "custom" else {}

    def _f(key: str, default: float | None) -> float | None:
        raw = e.get(key, "")
        if raw == "":
            return default
        return float(raw)

    def _i(key: str, default: int) -> int:
        raw = e.get(key, "")
        return int(raw) if raw else default

    def _b(key: str, default: bool) -> bool:
        raw = e.get(key, "").strip().lower()
        if raw == "":
            return default
        return raw not in ("0", "false", "no", "off")

    return PropFirmPolicy(
        initial_balance=_f("PROPFIRM_INITIAL_BALANCE", 10_000.0),
        max_daily_loss_pct=_f("PROPFIRM_MAX_DAILY_LOSS_PCT", base.get("max_daily_loss_pct", 0.05)),
        max_total_drawdown_pct=_f("PROPFIRM_MAX_TOTAL_DD_PCT", base.get("max_total_drawdown_pct", 0.10)),
        profit_target_pct=_f("PROPFIRM_PROFIT_TARGET_PCT", base.get("profit_target_pct", 0.10)),
        min_trading_days=_i("PROPFIRM_MIN_TRADING_DAYS", base.get("min_trading_days", 0)),
        max_lot_size=_f("PROPFIRM_MAX_LOT_SIZE", base.get("max_lot_size", None)),
        require_stop_loss=_b("PROPFIRM_REQUIRE_STOP_LOSS", base.get("require_stop_loss", True)),
        drawdown_from_peak=_b("PROPFIRM_DD_FROM_PEAK", base.get("drawdown_from_peak", False)),
        preset_name=base.get("preset_name", preset_id),
    )
