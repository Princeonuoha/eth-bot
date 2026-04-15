"""
Unit tests for CoinGeckoSentiment._parse() and should_block_trade()
No network calls — uses mock API response data.
"""

import pytest
from unittest.mock import patch, MagicMock
from app.services.coingecko import CoinGeckoSentiment


def make_mock_data(change_1h=0.0, change_24h=0.0, change_7d=0.0, price=2000.0):
    return {
        "current_price": price,
        "price_change_percentage_1h_in_currency": change_1h,
        "price_change_percentage_24h_in_currency": change_24h,
        "price_change_percentage_7d_in_currency": change_7d,
        "total_volume": 10_000_000,
    }


@pytest.fixture
def cg():
    return CoinGeckoSentiment()


# ── Sentiment classification ──────────────────────────────────────────────────

def test_strong_bullish_signal(cg):
    data = make_mock_data(change_1h=2.0, change_24h=4.0, change_7d=6.0)
    result = cg._parse(data)
    assert result.sentiment == "bullish"
    assert result.signal_strength > 70


def test_strong_bearish_signal(cg):
    data = make_mock_data(change_1h=-2.0, change_24h=-4.0, change_7d=-6.0)
    result = cg._parse(data)
    assert result.sentiment == "bearish"
    assert result.signal_strength < 30


def test_neutral_signal(cg):
    data = make_mock_data(change_1h=0.1, change_24h=0.2, change_7d=0.3)
    result = cg._parse(data)
    assert result.sentiment == "neutral"


def test_is_bullish_helper(cg):
    data = make_mock_data(change_1h=2.0, change_24h=4.0, change_7d=6.0)
    result = cg._parse(data)
    assert result.is_bullish() is True
    assert result.is_bearish() is False


def test_summary_contains_percentages(cg):
    data = make_mock_data(change_1h=1.5, change_24h=3.0, change_7d=5.5)
    result = cg._parse(data)
    assert "1h" in result.summary
    assert "24h" in result.summary
    assert "7d" in result.summary


def test_raw_score_exposed(cg):
    """raw_score should be present and within -100..+100."""
    data = make_mock_data(change_1h=2.0, change_24h=4.0, change_7d=6.0)
    result = cg._parse(data)
    assert -100 <= result.raw_score <= 100


# ── Extreme fear detection ────────────────────────────────────────────────────

def test_extreme_fear_when_all_deeply_negative(cg):
    """All timeframes deeply negative → raw_score = -100 → extreme fear."""
    data = make_mock_data(change_1h=-2.0, change_24h=-4.0, change_7d=-6.0)
    result = cg._parse(data)
    assert result.raw_score == -100
    assert result.is_extreme_fear() is True


def test_not_extreme_fear_when_mildly_bearish(cg):
    """Mild negative across all frames → bearish but NOT extreme fear."""
    data = make_mock_data(change_1h=-0.5, change_24h=-1.5, change_7d=-3.0)
    result = cg._parse(data)
    assert result.sentiment == "bearish"
    assert result.is_extreme_fear() is False


def test_not_extreme_fear_when_neutral(cg):
    data = make_mock_data(change_1h=0.1, change_24h=0.2, change_7d=0.3)
    result = cg._parse(data)
    assert result.is_extreme_fear() is False


def test_not_extreme_fear_when_bullish(cg):
    data = make_mock_data(change_1h=2.0, change_24h=4.0, change_7d=6.0)
    result = cg._parse(data)
    assert result.is_extreme_fear() is False


# ── should_block_trade ────────────────────────────────────────────────────────

def test_blocks_when_extreme_fear_and_below_ema(cg):
    """Extreme fear + price below 200 EMA → block the trade."""
    data = make_mock_data(change_1h=-2.0, change_24h=-4.0, change_7d=-6.0)
    result = cg._parse(data)
    assert result.is_extreme_fear() is True
    assert result.should_block_trade(trend_bullish=False) is True


def test_does_not_block_when_extreme_fear_but_above_ema(cg):
    """
    Extreme fear BUT price is above 200 EMA → don't block.
    Price action overrides sentiment — the market is telling a different story.
    """
    data = make_mock_data(change_1h=-2.0, change_24h=-4.0, change_7d=-6.0)
    result = cg._parse(data)
    assert result.is_extreme_fear() is True
    assert result.should_block_trade(trend_bullish=True) is False


def test_does_not_block_when_bearish_but_not_extreme(cg):
    """Regular bearish (not extreme fear) should NEVER block trades."""
    data = make_mock_data(change_1h=-0.5, change_24h=-2.0, change_7d=-3.0)
    result = cg._parse(data)
    assert result.sentiment == "bearish"
    assert result.is_extreme_fear() is False
    assert result.should_block_trade(trend_bullish=False) is False
    assert result.should_block_trade(trend_bullish=True) is False


def test_does_not_block_when_neutral(cg):
    data = make_mock_data(change_1h=0.1, change_24h=0.2, change_7d=0.3)
    result = cg._parse(data)
    assert result.should_block_trade(trend_bullish=False) is False
    assert result.should_block_trade(trend_bullish=True) is False


def test_does_not_block_when_bullish(cg):
    data = make_mock_data(change_1h=2.0, change_24h=4.0, change_7d=6.0)
    result = cg._parse(data)
    assert result.should_block_trade(trend_bullish=False) is False
    assert result.should_block_trade(trend_bullish=True) is False


# ── API / caching ─────────────────────────────────────────────────────────────

def test_get_sentiment_returns_none_on_failure(cg):
    with patch("requests.get", side_effect=Exception("network error")):
        result = cg.get_sentiment()
    assert result is None


def test_get_sentiment_uses_cache(cg):
    mock_data = make_mock_data(change_1h=1.0, change_24h=2.0, change_7d=3.0)
    mock_resp = MagicMock()
    mock_resp.json.return_value = [mock_data]

    with patch("requests.get", return_value=mock_resp) as mock_get:
        cg.get_sentiment()
        cg.get_sentiment()  # second call should use cache
        assert mock_get.call_count == 1  # only one real HTTP call
