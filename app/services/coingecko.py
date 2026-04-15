"""
CoinGecko Market Sentiment
──────────────────────────
Fetches free market data from CoinGecko's public API:
  - ETH price trend (1h, 24h, 7d % change)
  - Market sentiment (bullish / neutral / bearish)
  - Extreme Fear detection — the only condition that blocks trades
  - Market cap rank and volume spike detection

No API key required for basic endpoints.
Data is cached for 5 minutes to avoid rate limiting.

Sentiment philosophy:
  Old behaviour: ANY bearish reading blocked trades.
  New behaviour: Only EXTREME FEAR + price below 200 EMA blocks trades.
  Regular bearish = trade with caution, not abstain completely.
  This prevents the filter from killing good setups during mild pullbacks.
"""

import time
from dataclasses import dataclass
from typing import Literal

import requests
from loguru import logger

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
CACHE_TTL_SECONDS = 300  # 5 minutes

# Score thresholds
BULLISH_THRESHOLD = 25
BEARISH_THRESHOLD = -25
EXTREME_FEAR_THRESHOLD = -60   # Much worse than normal bearish — score -60 or below


@dataclass
class MarketSentiment:
    price_usd: float
    change_1h: float          # % change last 1 hour
    change_24h: float         # % change last 24 hours
    change_7d: float          # % change last 7 days
    volume_24h: float         # USD volume
    volume_change_24h: float  # % change in volume
    sentiment: Literal["bullish", "neutral", "bearish"]
    signal_strength: int      # 0–100 (higher = stronger bullish signal)
    raw_score: int            # raw score before normalisation (-100 to +100)
    summary: str              # Human-readable one-liner

    def is_bullish(self) -> bool:
        return self.sentiment == "bullish"

    def is_bearish(self) -> bool:
        return self.sentiment == "bearish"

    def is_extreme_fear(self) -> bool:
        """
        True only when ALL timeframes are deeply negative.
        This is the only sentiment condition that should block trades.

        Score -60 means: 1h deeply red AND 24h deeply red AND 7d deeply red.
        Example: 1h=-1.5%, 24h=-4%, 7d=-8% → score = -100 → extreme fear.
        Example: 1h=-0.5%, 24h=-2%, 7d=-6% → score = -55 → NOT extreme fear.

        Regular bearish (score -25 to -59) = normal market pullback.
        Extreme fear (score <= -60) = systemic sell-off, avoid new entries.
        """
        return self.raw_score <= EXTREME_FEAR_THRESHOLD

    def should_block_trade(self, trend_bullish: bool) -> bool:
        """
        The single source of truth for whether sentiment should block a trade.

        Blocks ONLY when BOTH conditions are true:
          1. Sentiment is extreme fear (raw_score <= -60)
          2. Price is already below the 200 EMA (trend_bullish=False)

        Logic: if price is above 200 EMA AND sentiment is extreme fear,
        the price action is telling a different story — trust price action.
        If price is below EMA AND extreme fear, we're in a genuine downtrend
        collapse — stay out.
        """
        if not self.is_extreme_fear():
            return False  # Normal bearish / neutral / bullish → never block

        if trend_bullish:
            return False  # Price above EMA overrides even extreme fear

        # Extreme fear AND price below EMA → block
        logger.warning(
            f"CoinGecko: EXTREME FEAR block active | "
            f"score={self.raw_score} | trend_bullish={trend_bullish} | "
            f"{self.summary}"
        )
        return True


class CoinGeckoSentiment:
    def __init__(self):
        self._cache: MarketSentiment | None = None
        self._cache_time: float = 0
        self._consecutive_failures: int = 0

    def get_sentiment(self) -> MarketSentiment | None:
        """
        Returns latest ETH market sentiment.
        Returns None if API is unavailable (bot continues without it).
        """
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

            fear_tag = " ⚠️ EXTREME FEAR" if sentiment.is_extreme_fear() else ""
            logger.info(
                f"CoinGecko: {sentiment.sentiment.upper()}{fear_tag} | "
                f"score={sentiment.raw_score} | "
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
            return self._cache  # return stale cache if available

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
        volume_change = 0.0  # not in this endpoint, placeholder

        # ── Sentiment scoring ─────────────────────────────────────────────────
        # Score each timeframe: positive = bullish points, negative = bearish
        # Range: -100 to +100
        score = 0

        # 1h weight: 40 points max
        if change_1h > 1.0:
            score += 40
        elif change_1h > 0.3:
            score += 20
        elif change_1h < -1.0:
            score -= 40
        elif change_1h < -0.3:
            score -= 20

        # 24h weight: 35 points max
        if change_24h > 3.0:
            score += 35
        elif change_24h > 1.0:
            score += 18
        elif change_24h < -3.0:
            score -= 35
        elif change_24h < -1.0:
            score -= 18

        # 7d weight: 25 points max
        if change_7d > 5.0:
            score += 25
        elif change_7d > 2.0:
            score += 12
        elif change_7d < -5.0:
            score -= 25
        elif change_7d < -2.0:
            score -= 12

        # Normalise score to 0–100 signal strength
        raw_max = 100
        signal_strength = int(max(0, min(100, (score + raw_max) / (raw_max * 2) * 100)))

        # Classify sentiment (unchanged thresholds)
        if score >= BULLISH_THRESHOLD:
            sentiment: Literal["bullish", "neutral", "bearish"] = "bullish"
        elif score <= BEARISH_THRESHOLD:
            sentiment = "bearish"
        else:
            sentiment = "neutral"

        summary = (
            f"ETH {sentiment} | score={score} | "
            f"1h {change_1h:+.2f}% | 24h {change_24h:+.2f}% | 7d {change_7d:+.2f}%"
        )

        return MarketSentiment(
            price_usd=d.get("current_price") or 0.0,
            change_1h=change_1h,
            change_24h=change_24h,
            change_7d=change_7d,
            volume_24h=volume_24h,
            volume_change_24h=volume_change,
            sentiment=sentiment,
            signal_strength=signal_strength,
            raw_score=score,
            summary=summary,
        )
