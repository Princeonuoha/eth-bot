from unittest.mock import MagicMock, patch

import pytest

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
        cg.get_sentiment()
        assert mock_get.call_count == 1
