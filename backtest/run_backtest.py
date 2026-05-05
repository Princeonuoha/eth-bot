"""
Run Backtest
────────────
CLI entry point for the backtesting framework.

Usage (from ~/eth-bot):

  # Run with default config (matches current .env testnet settings)
  python -m backtest.run_backtest

  # Run with custom date range
  python -m backtest.run_backtest --start 2024-01-01 --end 2024-06-01

  # Run with tighter live settings
  python -m backtest.run_backtest --rsi-oversold 40 --pullback-min 0.6 --ema-slope-min 0.0

  # Force re-download candles
  python -m backtest.run_backtest --refresh

  # Save trade log to CSV
  python -m backtest.run_backtest --save-csv

  # Simulate live bot cooldown (default — 15min base, 60min max)
  python -m backtest.run_backtest --symbol SOLUSDC --start 2026-01-01

  # Custom cooldown base
  python -m backtest.run_backtest --symbol SOLUSDC --start 2026-01-01 --cooldown-min 30

  # Disable cooldown to see raw signal performance
  python -m backtest.run_backtest --symbol SOLUSDC --start 2026-01-01 --no-cooldown

Examples to validate before going live:
  # Current testnet loose params
  python -m backtest.run_backtest --rsi-oversold 62 --pullback-min 0.2

  # Tighter live params
  python -m backtest.run_backtest --rsi-oversold 40 --pullback-min 0.6 --ema-slope-min 0.0
"""

import argparse
import os
import sys

# Allow running from ~/eth-bot directly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger

from backtest.data_fetcher import fetch_multi_timeframe
from backtest.engine import BacktestConfig, run
from backtest.reporter import print_report, to_dataframe


def parse_args():
    parser = argparse.ArgumentParser(description="ETH Bot Backtester")

    # Date range
    parser.add_argument("--start", default="2024-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="End date (YYYY-MM-DD), defaults to today")
    parser.add_argument("--symbol", default="ETHUSDC", help="Trading pair")

    # Entry params
    parser.add_argument("--rsi-oversold", type=float, default=62.0, help="15m RSI threshold")
    parser.add_argument("--rsi-1h-min", type=float, default=45.0, help="1h RSI minimum")
    parser.add_argument("--pullback-min", type=float, default=0.2, help="Min pullback %")
    parser.add_argument("--ema-slope-min", type=float, default=-0.1, help="Min EMA slope %")
    parser.add_argument("--max-vol-ratio", type=float, default=1.5, help="Max volume ratio")

    # Exit params
    parser.add_argument("--stop-loss", type=float, default=1.5, help="Stop loss %")
    parser.add_argument("--partial-tp", type=float, default=0.8, help="Partial TP %")
    parser.add_argument("--partial-ratio", type=float, default=0.5, help="Partial TP sell ratio")
    parser.add_argument("--trail-activation", type=float, default=1.0, help="Trail activation %")
    parser.add_argument("--trail-stop", type=float, default=0.8, help="Trail stop %")

    # ATR
    parser.add_argument("--atr-mult", type=float, default=2.0, help="ATR SL multiplier")
    parser.add_argument("--atr-tp-mult", type=float, default=1.5, help="ATR TP multiplier")
    parser.add_argument("--atr-tp-min", type=float, default=0.8, help="ATR TP min %")
    parser.add_argument("--atr-tp-max", type=float, default=4.0, help="ATR TP max %")

    # Position sizing
    parser.add_argument("--trade-amount", type=float, default=1000.0, help="USDC per trade")
    parser.add_argument("--fee", type=float, default=0.1, help="Fee % per side")
    parser.add_argument("--slippage", type=float, default=0.05, help="Slippage %")

    # Cooldown simulation — mirrors trader.py SymbolState cooldown logic
    parser.add_argument(
        "--cooldown-min",
        type=int,
        default=15,
        help=(
            "Base cooldown minutes after a stop loss. Scales with consecutive stop losses "
            "(e.g. 15min base: SL#1=15min, SL#2=30min, SL#3=45min). "
            "Mirrors live bot default of 900s base. (default: 15)"
        ),
    )
    parser.add_argument(
        "--cooldown-max",
        type=int,
        default=60,
        help=(
            "Max cooldown minutes regardless of consecutive stop losses. "
            "Mirrors live bot max_cd of 3600s. (default: 60)"
        ),
    )
    parser.add_argument(
        "--no-cooldown",
        action="store_true",
        help=(
            "Disable cooldown simulation entirely. "
            "Use this to see raw signal performance without cooldown filtering. "
            "WARNING: results will be optimistic vs live bot behaviour."
        ),
    )

    # Other
    parser.add_argument("--refresh", action="store_true", help="Re-download candle data")
    parser.add_argument("--save-csv", action="store_true", help="Save trade log to CSV")
    parser.add_argument("--quiet", action="store_true", help="Suppress debug logs")

    return parser.parse_args()


def main():
    args = parse_args()

    if args.quiet:
        logger.remove()
        logger.add(lambda msg: print(msg, end=""), level="INFO")

    # Cooldown: 0 disables it in the engine
    cooldown_min = 0 if args.no_cooldown else args.cooldown_min
    cooldown_max = 0 if args.no_cooldown else args.cooldown_max

    config = BacktestConfig(
        trade_amount_usdc=args.trade_amount,
        rsi_oversold=args.rsi_oversold,
        rsi_1h_min=args.rsi_1h_min,
        pullback_min_pct=args.pullback_min,
        ema_slope_min_pct=args.ema_slope_min,
        max_volume_ratio=args.max_vol_ratio,
        stop_loss_pct=args.stop_loss,
        partial_tp_pct=args.partial_tp,
        partial_tp_ratio=args.partial_ratio,
        trailing_activation_pct=args.trail_activation,
        trailing_stop_pct=args.trail_stop,
        atr_multiplier=args.atr_mult,
        atr_tp_multiplier=args.atr_tp_mult,
        atr_tp_min_pct=args.atr_tp_min,
        atr_tp_max_pct=args.atr_tp_max,
        fee_pct=args.fee,
        slippage_pct=args.slippage,
        stop_loss_cooldown_minutes=cooldown_min,
        stop_loss_cooldown_max_minutes=cooldown_max,
    )

    # Fetch data
    df_15m, df_1h = fetch_multi_timeframe(
        symbol=args.symbol,
        start_date=args.start,
        end_date=args.end,
        force_refresh=args.refresh,
    )

    # Run backtest
    result = run(df_15m, df_1h, config=config, symbol=args.symbol)

    # Print report
    stats = print_report(result)

    # Save CSV if requested
    if args.save_csv and result.trades:
        df = to_dataframe(result)
        csv_path = os.path.join(
            os.path.dirname(__file__),
            f"results_{args.symbol}_{args.start}_{args.end or 'today'}.csv"
        )
        df.to_csv(csv_path, index=False)
        logger.info(f"Trade log saved to {csv_path}")

    return stats


if __name__ == "__main__":
    main()
