import pytest
from app.strategy.risk_manager import RiskManager


def make_rm() -> RiskManager:
    return RiskManager(daily_loss_limit_usdc=30.0, trade_amount_usdc=100.0)


def test_initial_state_can_trade():
    rm = make_rm()
    assert rm.can_trade() is True
    assert rm.is_halted is False


def test_halts_after_daily_loss_limit():
    rm = make_rm()
    rm.record_trade_result(-15.0)
    assert rm.can_trade() is True

    rm.record_trade_result(-16.0)  # cumulative -31 → past limit
    assert rm.is_halted is True
    assert rm.can_trade() is False


def test_profitable_trades_do_not_halt():
    rm = make_rm()
    for _ in range(10):
        rm.record_trade_result(5.0)
    assert rm.is_halted is False
    assert rm.can_trade() is True


def test_reset_clears_halt():
    rm = make_rm()
    rm.record_trade_result(-50.0)
    assert rm.is_halted is True

    rm.reset_daily()
    assert rm.is_halted is False
    assert rm.can_trade() is True


def test_position_size_matches_config():
    rm = make_rm()
    assert rm.position_size_usdc() == 100.0
