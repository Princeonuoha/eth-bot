"""
Backtest Data Fetcher
─────────────────────
Pulls historical OHLCV candles from Binance REST API.
No auth required — public endpoint.
Saves to CSV so you don't re-fetch every run.
"""

import os
import time

import pandas as pd
import requests
from loguru import logger

BINANCE_BASE = "https://api.binance.com"
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def fetch_candles(
    symbol: str = "ETHUSDC",
    interval: str = "15m",
    start_date: str = "2024-01-01",
    end_date: str | None = None,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Fetch historical candles for a symbol and interval.
    Caches to CSV — pass force_refresh=True to re-download.

    Args:
        symbol:        Trading pair e.g. 'ETHUSDC'
        interval:      Candle interval e.g. '15m', '1h'
        start_date:    ISO date string e.g. '2024-01-01'
        end_date:      ISO date string, defaults to today
        force_refresh: Re-download even if cached

    Returns:
        DataFrame with columns: open_time, open, high, low, close, volume
    """
    os.makedirs(DATA_DIR, exist_ok=True)

    end_date = end_date or pd.Timestamp.utcnow().strftime("%Y-%m-%d")
    cache_file = os.path.join(
        DATA_DIR, f"{symbol}_{interval}_{start_date}_{end_date}.csv"
    )

    if os.path.exists(cache_file) and not force_refresh:
        logger.info(f"Loading cached candles from {cache_file}")
        df = pd.read_csv(cache_file, parse_dates=["open_time"])
        logger.info(f"Loaded {len(df)} candles ({start_date} → {end_date})")
        return df

    logger.info(
        f"Fetching {symbol} {interval} candles from {start_date} to {end_date}..."
    )

    start_ms = int(pd.Timestamp(start_date).timestamp() * 1000)
    end_ms = int(pd.Timestamp(end_date).timestamp() * 1000)

    all_candles = []
    current_ms = start_ms
    limit = 1000  # Binance max per request

    while current_ms < end_ms:
        url = f"{BINANCE_BASE}/api/v3/klines"
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": current_ms,
            "endTime": end_ms,
            "limit": limit,
        }

        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            batch = resp.json()
        except Exception as e:
            logger.error(f"Fetch failed: {e}")
            break

        if not batch:
            break

        all_candles.extend(batch)
        last_open_time = batch[-1][0]

        logger.debug(
            f"Fetched {len(batch)} candles | "
            f"total={len(all_candles)} | "
            f"up to {pd.Timestamp(last_open_time, unit='ms')}"
        )

        if len(batch) < limit:
            break

        current_ms = last_open_time + 1
        time.sleep(0.2)  # rate limit courtesy

    if not all_candles:
        raise ValueError(f"No candles returned for {symbol} {interval}")

    df = pd.DataFrame(
        all_candles,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_base", "taker_quote", "ignore",
        ],
    )

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df = df[["open_time", "open", "high", "low", "close", "volume"]].copy()
    df = df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)

    df.to_csv(cache_file, index=False)
    logger.info(f"Saved {len(df)} candles to {cache_file}")

    return df


def fetch_multi_timeframe(
    symbol: str = "ETHUSDC",
    start_date: str = "2024-01-01",
    end_date: str | None = None,
    force_refresh: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Fetches both 15m and 1h candles needed by the strategy.
    Returns (df_15m, df_1h).
    """
    df_15m = fetch_candles(symbol, "15m", start_date, end_date, force_refresh)
    df_1h = fetch_candles(symbol, "1h", start_date, end_date, force_refresh)
    return df_15m, df_1h
