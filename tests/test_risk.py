import pytest

from src.risk.position_sizing import lot_size_from_risk, pip_value
from src.risk.risk_manager import RiskLimits, RiskManager


def test_pip_value_defaults():
    assert pip_value("EURUSD") == 10.0
    assert pip_value("USDJPY") == 9.0


def test_lot_size_basic():
    # 10000 balance, 1% risk, 50 pip stop, EURUSD → $100 / (50 × $10) = 0.2 lots
    lots = lot_size_from_risk(10_000, 0.01, 50, "EURUSD")
    assert lots == pytest.approx(0.2, abs=0.01)


def test_lot_size_minimum_applied():
    lots = lot_size_from_risk(100, 0.01, 200, "EURUSD")
    assert lots >= 0.01


def test_risk_manager_blocks_over_max_trades():
    rm = RiskManager(RiskLimits(max_open_trades=1))
    rm.register_trade_opened(risk_pct=0.01)
    decision = rm.evaluate(10_000, 50, "EURUSD", lot_sizer=lot_size_from_risk)
    assert not decision.approved
    assert "max open trades" in decision.reason


def test_risk_manager_blocks_low_balance():
    rm = RiskManager(RiskLimits(min_balance=500))
    decision = rm.evaluate(100, 50, "EURUSD", lot_sizer=lot_size_from_risk)
    assert not decision.approved


def test_risk_manager_daily_loss_circuit():
    rm = RiskManager(RiskLimits(max_daily_loss_pct=0.05))
    rm.register_trade_closed(risk_pct=0.01, pnl=-600)  # 6% loss on 10k
    decision = rm.evaluate(10_000, 50, "EURUSD", lot_sizer=lot_size_from_risk)
    assert not decision.approved
    assert "circuit breaker" in decision.reason


def test_risk_manager_approves_clean_request():
    rm = RiskManager(RiskLimits())
    decision = rm.evaluate(10_000, 50, "EURUSD", lot_sizer=lot_size_from_risk)
    assert decision.approved
    assert decision.lot_size and decision.lot_size > 0
