"""
Tests for signal_engine.py
Covers the updated should_buy() (with ema_slope + volume_ratio)
and should_sell() (trailing stop, no take_profit_pct).
"""

from app.strategy.signal_engine import is_trend_bullish, should_buy, should_sell

# ── Shared base kwargs for should_buy ─────────────────────────────────────────
# All conditions passing — tweak individual fields per test.

BASE_BUY_KWARGS = dict(
    trend_bullish=True,
    ema_slope=0.05,       # rising EMA
    rsi=35.0,             # below oversold threshold
    pullback_pct=1.0,     # above minimum pullback
    volume_ratio=1.0,     # normal volume
    in_position=False,
    rsi_oversold=38.0,
    pullback_min_pct=0.8,
    ema_slope_min_pct=0.0,
    max_volume_ratio=1.5,
)

# ── is_trend_bullish ──────────────────────────────────────────────────────────

def test_trend_bullish_when_above_ema():
    assert is_trend_bullish(2100.0, 2000.0) is True


def test_trend_not_bullish_when_below_ema():
    assert is_trend_bullish(1900.0, 2000.0) is False


def test_trend_not_bullish_when_equal_to_ema():
    assert is_trend_bullish(2000.0, 2000.0) is False


# ── should_buy — all conditions met ──────────────────────────────────────────

def test_buy_signal_all_conditions_met():
    assert should_buy(**BASE_BUY_KWARGS) is True


# ── should_buy — individual blockers ─────────────────────────────────────────

def test_buy_blocked_when_in_position():
    assert should_buy(**{**BASE_BUY_KWARGS, "in_position": True}) is False


def test_buy_blocked_when_trend_bearish():
    assert should_buy(**{**BASE_BUY_KWARGS, "trend_bullish": False}) is False


def test_buy_blocked_when_ema_slope_flat():
    """EMA slope exactly at 0 should be blocked (must be strictly above min)."""
    assert should_buy(**{**BASE_BUY_KWARGS, "ema_slope": 0.0, "ema_slope_min_pct": 0.0}) is False


def test_buy_blocked_when_ema_slope_negative():
    assert should_buy(**{**BASE_BUY_KWARGS, "ema_slope": -0.1}) is False


def test_buy_passes_when_ema_slope_positive():
    assert should_buy(**{**BASE_BUY_KWARGS, "ema_slope": 0.01}) is True


def test_buy_blocked_when_rsi_too_high():
    assert should_buy(**{**BASE_BUY_KWARGS, "rsi": 45.0}) is False


def test_buy_blocked_rsi_exactly_at_threshold():
    """rsi == rsi_oversold should NOT trigger (must be strictly below)."""
    assert should_buy(**{**BASE_BUY_KWARGS, "rsi": 38.0}) is False


def test_buy_signal_rsi_just_below_threshold():
    assert should_buy(**{**BASE_BUY_KWARGS, "rsi": 37.9}) is True


def test_buy_blocked_when_pullback_too_small():
    assert should_buy(**{**BASE_BUY_KWARGS, "pullback_pct": 0.3}) is False


def test_buy_blocked_when_volume_spike():
    """Volume at or above max_volume_ratio should block the buy."""
    assert should_buy(**{**BASE_BUY_KWARGS, "volume_ratio": 1.5}) is False


def test_buy_blocked_when_volume_extreme():
    assert should_buy(**{**BASE_BUY_KWARGS, "volume_ratio": 3.0}) is False


def test_buy_passes_when_volume_normal():
    assert should_buy(**{**BASE_BUY_KWARGS, "volume_ratio": 1.2}) is True


def test_buy_blocked_when_strict_slope_not_met():
    """With ema_slope_min_pct=0.05, a slope of 0.03 should be blocked."""
    assert should_buy(**{**BASE_BUY_KWARGS, "ema_slope": 0.03, "ema_slope_min_pct": 0.05}) is False


def test_buy_passes_when_strict_slope_met():
    assert should_buy(**{**BASE_BUY_KWARGS, "ema_slope": 0.06, "ema_slope_min_pct": 0.05}) is True


# ── should_sell — stop loss ───────────────────────────────────────────────────

BASE_SELL_KWARGS = dict(
    entry_price=2000.0,
    current_price=2000.0,
    peak_price=2000.0,
    stop_loss_pct=1.5,
    trailing_activation_pct=1.0,
    trailing_stop_pct=0.8,
)


def test_sell_stop_loss_fires():
    result = should_sell(**{**BASE_SELL_KWARGS, "current_price": 1969.0})  # -1.55%
    assert result == "stop_loss"


def test_sell_stop_loss_exactly_at_threshold():
    """PnL exactly at -stop_loss_pct should fire."""
    result = should_sell(**{**BASE_SELL_KWARGS, "current_price": 1970.0})  # -1.5%
    assert result == "stop_loss"


def test_sell_stop_loss_not_triggered_above_threshold():
    result = should_sell(**{**BASE_SELL_KWARGS, "current_price": 1985.0})  # -0.75%
    assert result is None


# ── should_sell — trailing stop ───────────────────────────────────────────────

def test_trailing_stop_not_active_before_activation():
    """Price has risen but not yet hit trailing_activation_pct — should hold."""
    result = should_sell(**{
        **BASE_SELL_KWARGS,
        "current_price": 2010.0,  # +0.5% — below activation at +1%
        "peak_price": 2010.0,
    })
    assert result is None


def test_trailing_stop_activates_and_holds():
    """Price above activation but hasn't dropped enough from peak — hold."""
    result = should_sell(**{
        **BASE_SELL_KWARGS,
        "current_price": 2030.0,  # +1.5%
        "peak_price": 2040.0,     # peak was higher
        # trailing level = 2040 * (1 - 0.008) = 2023.68 — current is above
    })
    assert result is None


def test_trailing_stop_fires_when_dropped_from_peak():
    """Price drops 0.8%+ from peak after activation — trailing stop fires."""
    peak = 2100.0  # +5% above entry — trailing is active
    trail_level = peak * (1 - 0.008)  # = 2083.20
    result = should_sell(**{
        **BASE_SELL_KWARGS,
        "current_price": trail_level - 0.01,  # just below trail level
        "peak_price": peak,
    })
    assert result == "trailing_stop"


def test_trailing_stop_does_not_fire_at_trail_level():
    """Price exactly at trail level should fire (<=)."""
    peak = 2100.0
    trail_level = round(peak * (1 - 0.008), 4)
    result = should_sell(**{
        **BASE_SELL_KWARGS,
        "current_price": trail_level,
        "peak_price": peak,
    })
    assert result == "trailing_stop"


# ── should_sell — hold scenarios ─────────────────────────────────────────────

def test_hold_at_entry_price():
    result = should_sell(**BASE_SELL_KWARGS)
    assert result is None


def test_hold_small_gain_trailing_not_active():
    result = should_sell(**{**BASE_SELL_KWARGS, "current_price": 2005.0, "peak_price": 2005.0})
    assert result is None


def test_hold_small_loss_above_stop():
    result = should_sell(**{**BASE_SELL_KWARGS, "current_price": 1990.0})  # -0.5%
    assert result is None
