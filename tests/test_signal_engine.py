"""
Unit tests for signal_engine.py
Run with: pytest tests/
"""

import pytest
from app.strategy.signal_engine import should_buy, should_sell, is_trend_bullish


# ── is_trend_bullish ──────────────────────────────────────────────────────────

def test_trend_bullish_when_above_ema():
    assert is_trend_bullish(current_price=3200.0, ema_200=3000.0) is True


def test_trend_not_bullish_when_below_ema():
    assert is_trend_bullish(current_price=2800.0, ema_200=3000.0) is False


# ── should_buy ────────────────────────────────────────────────────────────────

BASE_BUY_KWARGS = dict(
    trend_bullish=True,
    rsi=32.0,
    pullback_pct=1.2,
    in_position=False,
    rsi_oversold=38.0,
    pullback_min_pct=0.8,
)


def test_buy_signal_all_conditions_met():
    assert should_buy(**BASE_BUY_KWARGS) is True


def test_buy_blocked_when_in_position():
    assert should_buy(**{**BASE_BUY_KWARGS, "in_position": True}) is False


def test_buy_blocked_when_trend_bearish():
    assert should_buy(**{**BASE_BUY_KWARGS, "trend_bullish": False}) is False


def test_buy_blocked_when_rsi_too_high():
    assert should_buy(**{**BASE_BUY_KWARGS, "rsi": 45.0}) is False


def test_buy_blocked_when_pullback_too_small():
    assert should_buy(**{**BASE_BUY_KWARGS, "pullback_pct": 0.3}) is False


def test_buy_blocked_rsi_exactly_at_threshold():
    # rsi == rsi_oversold should NOT trigger (must be strictly below)
    assert should_buy(**{**BASE_BUY_KWARGS, "rsi": 38.0}) is False


def test_buy_signal_rsi_just_below_threshold():
    assert should_buy(**{**BASE_BUY_KWARGS, "rsi": 37.9}) is True


# ── should_sell ───────────────────────────────────────────────────────────────

def test_sell_take_profit():
    result = should_sell(
        entry_price=3000.0,
        current_price=3031.0,  # +1.03%
        take_profit_pct=1.0,
        stop_loss_pct=0.7,
    )
    assert result == "take_profit"


def test_sell_stop_loss():
    result = should_sell(
        entry_price=3000.0,
        current_price=2978.0,  # -0.73%
        take_profit_pct=1.0,
        stop_loss_pct=0.7,
    )
    assert result == "stop_loss"


def test_hold_when_in_range():
    result = should_sell(
        entry_price=3000.0,
        current_price=3010.0,  # +0.33%
        take_profit_pct=1.0,
        stop_loss_pct=0.7,
    )
    assert result is None


def test_hold_exactly_at_entry():
    result = should_sell(
        entry_price=3000.0,
        current_price=3000.0,
        take_profit_pct=1.0,
        stop_loss_pct=0.7,
    )
    assert result is None
