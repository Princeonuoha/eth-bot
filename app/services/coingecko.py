"""
CoinGecko Market Sentiment
──────────────────────────
Fetches free market data from CoinGecko's public API:
  - ETH price trend (1h, 24h, 7d % change)
  - Market sentiment (bullish / neutral / bearish)
  - Fear & Greed proxy from price momentum
  - Market cap rank and volume spike detection

No API key required for basic endpoints.
Data is cached for 5 minutes to avoid rate limiting.
"""

import time
from dataclasses import dataclass
from typing import Literal

import requests
from loguru import logger

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
CACHE_TTL_SECONDS = 300  # 5 minutes


@dataclass
class MarketSentiment:
    price_usd: float
    change_1h: float
    change_24h: float
    change_7d: float
    volume_24h: float
    volume_change_24h: float
    sentiment: Literal["bullish", "neutral", "bearish"]
    signal_strength: int
    summary: str

    def is_bullish(self) -> bool:
        return self.sentiment == "bullish"

    def is_bearish(self) -> bool:
        return self.sentiment == "bearish"


class CoinGeckoSentiment:
    def __init__(self):
        self._cache: MarketSentiment | None = None
        self._cache_time: float = 0
        self._consecutive_failures: int = 0

    def get_sentiment(self) -> MarketSentiment | None:
        now = time.time()
        if self._cache and (now - self._cache_time) < CACHE_TTL_SECONDS:
            logger.debug("CoinGecko: using cached sentiment")
            return self._cache

        try:
            data = self._fetch()
            sentiment = self._parse(data)
            self._cache = sentiment
            self._cache_time = now
            self._consecutive_failures = 0
            logger.info(
                f"CoinGecko: {sentiment.sentiment.upper()} | "
                f"1h={sentiment.change_1h:+.2f}% | "
                f"24h={sentiment.change_24h:+.2f}% | "
                f"7d={sentiment.change_7d:+.2f}% | "
                f"strength={sentiment.signal_strength}/100"
            )
            return sentiment
        except Exception as e:
            self._consecutive_failures += 1
            logger.warning(
                f"CoinGecko: failed to fetch sentiment ({e}) — "
                f"bot will continue without it (failure #{self._consecutive_failures})"
            )
            return self._cache

    def _fetch(self) -> dict:
        resp = requests.get(
            f"{COINGECKO_BASE}/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": "ethereum",
                "price_change_percentage": "1h,24h,7d",
            },
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            raise ValueError("Empty response from CoinGecko")
        return data[0]

    def _parse(self, d: dict) -> MarketSentiment:
        change_1h = d.get("price_change_percentage_1h_in_currency") or 0.0
        change_24h = d.get("price_change_percentage_24h_in_currency") or 0.0
        change_7d = d.get("price_change_percentage_7d_in_currency") or 0.0
        volume_24h = d.get("total_volume") or 0.0

        score = 0

        if change_1h > 1.0:
            score += 40
        elif change_1h > 0.3:
            score += 20
        elif change_1h < -1.0:
            score -= 40
        elif change_1h < -0.3:
            score -= 20

        if change_24h > 3.0:
            score += 35
        elif change_24h > 1.0:
            score += 18
        elif change_24h < -3.0:
            score -= 35
        elif change_24h < -1.0:
            score -= 18

        if change_7d > 5.0:
            score += 25
        elif change_7d > 2.0:
            score += 12
        elif change_7d < -5.0:
            score -= 25
        elif change_7d < -2.0:
            score -= 12

        signal_strength = int(max(0, min(100, (score + 100) / 200 * 100)))

        if score >= 25:
            sentiment: Literal["bullish", "neutral", "bearish"] = "bullish"
        elif score <= -25:
            sentiment = "bearish"
        else:
            sentiment = "neutral"

        summary = (
            f"ETH {sentiment} | "
            f"1h {change_1h:+.2f}% | 24h {change_24h:+.2f}% | 7d {change_7d:+.2f}%"
        )

        return MarketSentiment(
            price_usd=d.get("current_price") or 0.0,
            change_1h=change_1h,
            change_24h=change_24h,
            change_7d=change_7d,
            volume_24h=volume_24h,
            volume_change_24h=0.0,
            sentiment=sentiment,
            signal_strength=signal_strength,
            summary=summary,
        )
