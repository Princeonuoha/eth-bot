"""
Backtest Data Fetcher
─────────────────────
Pulls historical OHLCV candles from Binance REST API.
No auth required — public endpoint.

Caching strategy — incremental:
  - Master file per symbol+interval: e.g. SOLUSDC_15m.csv
  - On first run: downloads full history from start_date
  - On subsequent runs: appends only new candles since last cached timestamp
  - Result: backtests are instant after first run, updates take seconds
  - force_refresh=True: re-downloads everything from scratch
"""

import os
import time

import pandas as pd
import requests
from loguru import logger

BINANCE_BASE = "https://api.binance.com"
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def _cache_path(symbol: str, interval: str) -> str:
    """Master cache file — keyed by symbol+interval only, not date range."""
    return os.path.join(DATA_DIR, f"{symbol}_{interval}.csv")


def _fetch_from_binance(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> pd.DataFrame:
    """Raw fetch from Binance REST API. Returns DataFrame of new candles."""
    all_candles = []
    current_ms = start_ms
    limit = 1000

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
        time.sleep(0.1)

    if not all_candles:
        return pd.DataFrame()

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
    return df


def fetch_candles(
    symbol: str = "ETHUSDC",
    interval: str = "15m",
    start_date: str = "2024-01-01",
    end_date: str | None = None,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Fetch historical candles with incremental caching.

    - First call: downloads full history, saves master cache file
    - Subsequent calls: loads cache, appends only new candles since last timestamp
    - force_refresh=True: wipes cache and re-downloads everything

    Args:
        symbol:        Trading pair e.g. 'ETHUSDC'
        interval:      Candle interval e.g. '15m', '1h'
        start_date:    ISO date string e.g. '2024-01-01'
        end_date:      ISO date string, defaults to today
        force_refresh: Re-download everything from scratch

    Returns:
        DataFrame with columns: open_time, open, high, low, close, volume
    """
    os.makedirs(DATA_DIR, exist_ok=True)

    end_date = end_date or pd.Timestamp.utcnow().strftime("%Y-%m-%d")
    end_ms = int(pd.Timestamp(end_date).timestamp() * 1000)
    cache_file = _cache_path(symbol, interval)

    # ── Load or initialise cache ──────────────────────────────────────────────
    if os.path.exists(cache_file) and not force_refresh:
        logger.info(f"Loading cache: {cache_file}")
        cached = pd.read_csv(cache_file, parse_dates=["open_time"])
        last_cached_ts = cached["open_time"].max()
        last_cached_ms = int(last_cached_ts.timestamp() * 1000)

        # Check if cache already covers the requested end date
        end_ts = pd.Timestamp(end_date)
        if last_cached_ts >= end_ts - pd.Timedelta(hours=2):
            logger.info(
                f"Cache up to date ({last_cached_ts.date()}) — "
                f"no fetch needed | {len(cached)} candles"
            )
        else:
            # Incremental update — fetch only new candles
            fetch_from_ms = last_cached_ms + 1
            logger.info(
                f"Incremental update: fetching {symbol} {interval} "
                f"from {last_cached_ts.date()} → {end_date}"
            )
            new_df = _fetch_from_binance(symbol, interval, fetch_from_ms, end_ms)

            if not new_df.empty:
                # Remove the last cached candle (may be incomplete) and append
                cached = cached[cached["open_time"] < last_cached_ts]
                cached = pd.concat([cached, new_df], ignore_index=True)
                cached = cached.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
                cached.to_csv(cache_file, index=False)
                logger.info(
                    f"Appended {len(new_df)} new candles → "
                    f"{len(cached)} total | saved to {cache_file}"
                )
            else:
                logger.info("No new candles to append")

        # Filter to requested date range and return
        start_ts = pd.Timestamp(start_date)
        result = cached[cached["open_time"] >= start_ts].copy().reset_index(drop=True)
        logger.info(
            f"Returning {len(result)} candles "
            f"({start_date} → {end_date})"
        )
        return result

    # ── Full download (no cache or force_refresh) ─────────────────────────────
    if force_refresh and os.path.exists(cache_file):
        logger.info(f"Force refresh — deleting {cache_file}")
        os.remove(cache_file)

    logger.info(
        f"Full download: {symbol} {interval} "
        f"from {start_date} to {end_date}..."
    )
    start_ms = int(pd.Timestamp(start_date).timestamp() * 1000)
    df = _fetch_from_binance(symbol, interval, start_ms, end_ms)

    if df.empty:
        raise ValueError(f"No candles returned for {symbol} {interval}")

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
