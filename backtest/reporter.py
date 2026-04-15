"""
Backtest Reporter
─────────────────
Takes a BacktestResult and produces:
  - Summary statistics (win rate, PnL, drawdown, Sharpe-like ratio)
  - Per-trade log
  - Parameter validation recommendations
"""

from dataclasses import dataclass

import pandas as pd
from loguru import logger

from backtest.engine import BacktestResult, BacktestTrade


@dataclass
class BacktestStats:
    total_trades: int
    wins: int
    losses: int
    win_rate_pct: float
    total_pnl_usdc: float
    avg_win_usdc: float
    avg_loss_usdc: float
    profit_factor: float        # gross wins / gross losses
    max_drawdown_usdc: float    # largest peak-to-trough drop
    max_drawdown_pct: float
    avg_trade_pnl_usdc: float
    best_trade_usdc: float
    worst_trade_usdc: float
    avg_peak_pct: float         # avg how far price moved in our favour before exit
    stop_loss_count: int
    trailing_stop_count: int
    take_profit_count: int
    partial_tp_count: int
    start_date: str
    end_date: str


def compute_stats(result: BacktestResult) -> BacktestStats:
    trades = result.trades
    if not trades:
        logger.warning("No trades to analyse")
        return BacktestStats(
            total_trades=0, wins=0, losses=0, win_rate_pct=0.0,
            total_pnl_usdc=0.0, avg_win_usdc=0.0, avg_loss_usdc=0.0,
            profit_factor=0.0, max_drawdown_usdc=0.0, max_drawdown_pct=0.0,
            avg_trade_pnl_usdc=0.0, best_trade_usdc=0.0, worst_trade_usdc=0.0,
            avg_peak_pct=0.0, stop_loss_count=0, trailing_stop_count=0,
            take_profit_count=0, partial_tp_count=0,
            start_date=result.start_date, end_date=result.end_date,
        )

    pnls = [t.pnl_usdc for t in trades if t.pnl_usdc is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    # Max drawdown — largest cumulative loss from peak equity
    equity = 0.0
    peak_equity = 0.0
    max_dd_usdc = 0.0
    for pnl in pnls:
        equity += pnl
        if equity > peak_equity:
            peak_equity = equity
        dd = peak_equity - equity
        if dd > max_dd_usdc:
            max_dd_usdc = dd

    max_dd_pct = (max_dd_usdc / result.config.trade_amount_usdc * 100) if max_dd_usdc > 0 else 0.0

    gross_wins = sum(wins) if wins else 0.0
    gross_losses = abs(sum(losses)) if losses else 0.0
    profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else float("inf")

    reasons = [t.reason for t in trades]

    return BacktestStats(
        total_trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate_pct=round(len(wins) / len(trades) * 100, 1),
        total_pnl_usdc=round(sum(pnls), 2),
        avg_win_usdc=round(sum(wins) / len(wins), 2) if wins else 0.0,
        avg_loss_usdc=round(sum(losses) / len(losses), 2) if losses else 0.0,
        profit_factor=profit_factor,
        max_drawdown_usdc=round(max_dd_usdc, 2),
        max_drawdown_pct=round(max_dd_pct, 2),
        avg_trade_pnl_usdc=round(sum(pnls) / len(pnls), 2),
        best_trade_usdc=round(max(pnls), 2),
        worst_trade_usdc=round(min(pnls), 2),
        avg_peak_pct=round(
            sum(t.peak_pct for t in trades if t.peak_pct) / len(trades), 2
        ),
        stop_loss_count=reasons.count("stop_loss"),
        trailing_stop_count=reasons.count("trailing_stop"),
        take_profit_count=reasons.count("take_profit"),
        partial_tp_count=sum(
            1 for t in trades if t.partial_pnl_usdc is not None
        ),
        start_date=result.start_date,
        end_date=result.end_date,
    )


def print_report(result: BacktestResult) -> BacktestStats:
    """Print a full backtest report to console and return stats."""
    stats = compute_stats(result)
    cfg = result.config

    sep = "=" * 60

    print(f"\n{sep}")
    print(f"  BACKTEST REPORT — {result.symbol}")
    print(f"  {stats.start_date} → {stats.end_date}")
    print(sep)

    print(f"\n  CONFIG")
    print(f"  {'RSI oversold':<28} {cfg.rsi_oversold}")
    print(f"  {'1h RSI min':<28} {cfg.rsi_1h_min}")
    print(f"  {'Pullback min %':<28} {cfg.pullback_min_pct}%")
    print(f"  {'EMA slope min %':<28} {cfg.ema_slope_min_pct}%")
    print(f"  {'Stop loss %':<28} {cfg.stop_loss_pct}%")
    print(f"  {'Partial TP %':<28} {cfg.partial_tp_pct}% (sell {int(cfg.partial_tp_ratio*100)}%)")
    print(f"  {'Trail activation %':<28} {cfg.trailing_activation_pct}%")
    print(f"  {'Trail stop %':<28} {cfg.trailing_stop_pct}%")
    print(f"  {'Fee %':<28} {cfg.fee_pct}%")
    print(f"  {'Slippage %':<28} {cfg.slippage_pct}%")

    print(f"\n  RESULTS")
    print(f"  {'Total trades':<28} {stats.total_trades}")
    print(f"  {'Wins / Losses':<28} {stats.wins} / {stats.losses}")
    print(f"  {'Win rate':<28} {stats.win_rate_pct}%")
    print(f"  {'Total PnL':<28} ${stats.total_pnl_usdc:+.2f} USDC")
    print(f"  {'Avg trade PnL':<28} ${stats.avg_trade_pnl_usdc:+.2f} USDC")
    print(f"  {'Avg win':<28} ${stats.avg_win_usdc:+.2f} USDC")
    print(f"  {'Avg loss':<28} ${stats.avg_loss_usdc:+.2f} USDC")
    print(f"  {'Profit factor':<28} {stats.profit_factor}")
    print(f"  {'Max drawdown':<28} ${stats.max_drawdown_usdc:.2f} ({stats.max_drawdown_pct:.2f}%)")
    print(f"  {'Best trade':<28} ${stats.best_trade_usdc:+.2f} USDC")
    print(f"  {'Worst trade':<28} ${stats.worst_trade_usdc:+.2f} USDC")
    print(f"  {'Avg peak gain':<28} +{stats.avg_peak_pct:.2f}%")

    print(f"\n  EXIT BREAKDOWN")
    print(f"  {'Stop losses':<28} {stats.stop_loss_count}")
    print(f"  {'Trailing stops':<28} {stats.trailing_stop_count}")
    print(f"  {'Take profits':<28} {stats.take_profit_count}")
    print(f"  {'Partial TPs fired':<28} {stats.partial_tp_count}")

    # Recommendations
    print(f"\n  RECOMMENDATIONS")
    if stats.total_trades == 0:
        print("  ⚠️  No trades — filters may be too strict")
    elif stats.total_trades < 10:
        print("  ⚠️  Very few trades — consider wider date range or looser filters")
    else:
        if stats.win_rate_pct < 40:
            print("  ❌ Win rate below 40% — entry filters need tightening")
        elif stats.win_rate_pct >= 55:
            print("  ✅ Win rate healthy")

        if stats.profit_factor < 1.0:
            print("  ❌ Profit factor < 1.0 — strategy is net losing")
        elif stats.profit_factor >= 1.5:
            print("  ✅ Profit factor healthy (>1.5)")

        if stats.stop_loss_count / max(stats.total_trades, 1) > 0.4:
            print("  ⚠️  >40% stop losses — entries may be poorly timed")

        if stats.max_drawdown_pct > 10:
            print(f"  ⚠️  Max drawdown {stats.max_drawdown_pct:.1f}% — consider tighter stops")

        if stats.avg_peak_pct > stats.trailing_stop_count and stats.trailing_stop_count < stats.wins * 0.5:
            print("  💡 Trailing stop could be tightened — avg peak gain is high")

    print(f"\n{sep}\n")

    # Per-trade log
    print("  TRADE LOG")
    print(f"  {'#':<4} {'Entry':<18} {'Exit':<18} {'Entry $':<10} {'Exit $':<10} {'PnL':<12} {'Peak%':<8} {'Reason'}")
    print("  " + "-" * 90)
    for i, t in enumerate(result.trades, 1):
        entry_str = str(t.entry_time)[:16] if t.entry_time else "—"
        exit_str = str(t.exit_time)[:16] if t.exit_time else "—"
        pnl_str = f"${t.pnl_usdc:+.2f}" if t.pnl_usdc is not None else "—"
        peak_str = f"+{t.peak_pct:.2f}%" if t.peak_pct else "—"
        partial_note = f" (partial ${t.partial_pnl_usdc:+.2f})" if t.partial_pnl_usdc else ""
        print(
            f"  {i:<4} {entry_str:<18} {exit_str:<18} "
            f"${t.entry_price:<9.2f} ${t.exit_price:<9.2f} "
            f"{pnl_str:<12} {peak_str:<8} {t.reason}{partial_note}"
        )

    print()
    return stats


def to_dataframe(result: BacktestResult) -> pd.DataFrame:
    """Convert trade list to DataFrame for further analysis."""
    rows = []
    for t in result.trades:
        rows.append({
            "entry_time": t.entry_time,
            "exit_time": t.exit_time,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "quantity": t.quantity,
            "pnl_usdc": t.pnl_usdc,
            "pnl_pct": t.pnl_pct,
            "peak_price": t.peak_price,
            "peak_pct": t.peak_pct,
            "reason": t.reason,
            "partial_pnl_usdc": t.partial_pnl_usdc,
            "stop_loss_pct": t.stop_loss_pct,
            "take_profit_pct": t.take_profit_pct,
        })
    return pd.DataFrame(rows)
