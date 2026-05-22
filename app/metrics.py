"""
Prometheus metrics registry for eth-bot.
All metric objects are module-level singletons.
Import and update these from trader.py — never instantiate elsewhere.
"""
from prometheus_client import Counter, Gauge

# ── Trade counters ───────────────────────────────────────────────────────────

bot_trades_total = Counter(
    "bot_trades_total",
    "Completed trades by symbol and exit reason",
    ["symbol", "reason"],
)

bot_signal_skip_total = Counter(
    "bot_signal_skip_total",
    "Signal evaluations skipped by reason (key regime-change indicator)",
    ["reason"],
)

bot_exchange_errors_total = Counter(
    "bot_exchange_errors_total",
    "Exchange API errors caught by the trading loop, by error type",
    ["error_type"],
    # error_type values: http_502 | http_503 | http_429 | timeout | other
)

# ── Per-symbol gauges ────────────────────────────────────────────────────────

bot_pnl_usdc = Gauge(
    "bot_pnl_usdc",
    "Cumulative realised PnL in USDC for this symbol (resets daily)",
    ["symbol"],
)

bot_position_open = Gauge(
    "bot_position_open",
    "1 if a position is currently open for this symbol, else 0",
    ["symbol"],
)

bot_sl_order_active = Gauge(
    "bot_sl_order_active",
    "1 if an exchange-side SL order is active for this symbol, else 0",
    ["symbol"],
)

bot_consecutive_stop_losses = Gauge(
    "bot_consecutive_stop_losses",
    "Consecutive SL exits for this symbol (drives cooldown duration)",
    ["symbol"],
)

# ── Session-level gauges ─────────────────────────────────────────────────────

bot_daily_pnl_usdc = Gauge(
    "bot_daily_pnl_usdc",
    "Running PnL in USDC for the current UTC day (all symbols combined)",
)

# ── Strategy info ────────────────────────────────────────────────────────────

bot_strategy_info = Gauge(
    "bot_strategy_info",
    "Active strategy info (always 1.0; the name is carried in the label).",
    ["strategy"],
)
