# eth-bot 🤖

A multi-pair cryptocurrency spot trading bot deployed on AWS EC2. Built with Python, Docker, and the Binance API. Trades ETH, BTC, and SOL against USDC using a momentum/pullback strategy with dynamic risk management.

> ⚠️ This bot trades real money. Use at your own risk. Past backtest performance does not guarantee future results.

---

## Features

- **Multi-pair trading** — ETHUSDC, BTCUSDC, SOLUSDC running concurrently with isolated per-symbol state
- **Momentum/pullback strategy** — enters on oversold dips within confirmed uptrends
- **Dynamic exits** — partial take profit + trailing stop system to capture extended moves
- **ATR-based stop loss** — adaptive stop widens in volatile markets
- **Multi-timeframe confirmation** — 15m entry signals gated by 1h RSI and 200 EMA trend
- **CoinGecko sentiment gate** — blocks entries during extreme fear conditions
- **Risk management** — daily loss limit, max trades per day, portfolio drawdown halt
- **Live dashboard** — Flask web UI with per-symbol signal logs, holdings, PnL chart
- **Telegram notifications** — buy/sell alerts, daily summary
- **Position persistence** — SQLite survives container restarts
- **Vectorized backtester** — ~50x faster than candle-by-candle engines, with cooldown simulation

---

## Strategy

### Entry conditions (all must be true)
| Condition | Parameter | Default |
|-----------|-----------|---------|
| Price above 200 EMA (1h) | trend filter | required |
| 50 EMA above 200 EMA (1h) | ema cross | required |
| 15m RSI oversold | `RSI_OVERSOLD` | 38 |
| 1h RSI minimum | `RSI_1H_MIN` | 50 |
| Pullback from recent high | `PULLBACK_MIN_PCT` | per-symbol |
| EMA slope positive | `EMA_SLOPE_MIN_PCT` | 0.02% |
| Volume not spiking | `MAX_VOL_RATIO` | 1.5x |
| Not in stop-loss cooldown | auto | 15–60 min |

### Per-symbol pullback thresholds
| Symbol | Pullback min |
|--------|-------------|
| ETHUSDC | 2.0% |
| BTCUSDC | 1.5% |
| SOLUSDC | 1.2% |

### Exit system
1. **Partial TP** — sell 40% of position at +1.0% gain
2. **Trailing stop** — activates at +0.9% peak, trails 0.35% below peak
3. **Hard stop loss** — ATR-based, minimum 0.5%

---

## Validated Backtest Results

### Full history (2024-01-01 → 2026-04-15)

| Pair | Trades | Win Rate | Profit Factor | Max Drawdown |
|------|--------|----------|---------------|--------------|
| ETHUSDC | 38 | 68.4% | 1.80 | 2.56% |
| BTCUSDC | 27 | 66.7% | 1.83 | 3.18% |
| SOLUSDC | 130 | 66.9% | 2.66 | 3.04% |

### 2026 bear market (2026-01-01 → 2026-04-15)

| Pair | Trades | Win Rate | Profit Factor | Notes |
|------|--------|----------|---------------|-------|
| ETHUSDC | 0 | — | — | Below 200 EMA, trend filter blocking correctly |
| BTCUSDC | 5 | 80.0% | 2.25 | Low frequency, high quality |
| SOLUSDC | 15 | 53.3% | 1.24 | Profitable despite bear conditions |

---

## Architecture

```
eth-bot/
├── app/
│   ├── main.py                  # Entry point
│   ├── config.py                # Pydantic settings, per-symbol pullback
│   ├── dashboard.py             # Flask dashboard, SQLite trade log
│   ├── exchange/
│   │   └── binance_client.py    # Binance API wrapper
│   ├── models/
│   │   ├── position.py          # Position dataclass
│   │   └── trade_event.py       # Trade event dataclass
│   ├── services/
│   │   ├── trader.py            # Main loop, SymbolState per pair
│   │   ├── position_store.py    # SQLite persistence (keyed by symbol)
│   │   ├── coingecko.py         # Sentiment gate
│   │   └── notifier.py          # Telegram notifications
│   └── strategy/
│       ├── signal_engine.py     # Pure entry/exit functions
│       ├── indicators.py        # RSI, EMA, ATR, pullback, etc.
│       └── risk_manager.py      # Daily limits, drawdown halt
├── backtest/
│   ├── engine.py                # Vectorized backtest engine
│   ├── run_backtest.py          # CLI entry point
│   ├── data_fetcher.py          # Binance OHLCV downloader
│   └── reporter.py              # Results formatter
├── tests/                       # pytest suite (50 tests)
├── Dockerfile
├── docker-compose.yml
└── .env                         # secrets — never committed
```

---

## Infrastructure

- **Server**: AWS EC2 t2.micro, Ubuntu 24, IP `13.38.86.84`
- **Runtime**: Docker with `restart: always`
- **Dashboard**: `http://13.38.86.84:5000`
- **Database**: SQLite via Docker volume (`eth-bot_data`)
- **CI/CD**: GitHub Actions — ruff lint + pytest on every push
- **Branch**: `feature/multi-pair` (merge to `main` after testnet validation)

---

## Setup

### Prerequisites
- AWS EC2 instance (t2.micro or larger)
- Binance account with API key
- Telegram bot token + chat ID
- Docker + Docker Compose installed

### 1. Clone and configure

```bash
git clone https://github.com/Princeonuoha/eth-bot.git
cd eth-bot
cp .env.example .env
nano .env  # fill in your keys
```

### 2. Environment variables

```env
# Binance
BINANCE_API_KEY=your_key
BINANCE_API_SECRET=your_secret
TESTNET=true                        # set false for live trading

# Pairs
SYMBOLS=ETHUSDC,BTCUSDC,SOLUSDC

# Position sizing
TRADE_AMOUNT_USDC=1000

# Risk
STOP_LOSS_PCT=0.5
DAILY_LOSS_LIMIT_USDC=45
MAX_TRADES_PER_DAY=6

# Strategy
RSI_OVERSOLD=38
RSI_1H_MIN=50.0
PULLBACK_MIN_PCT=2.0
ETHUSDC_PULLBACK_MIN_PCT=2.0
BTCUSDC_PULLBACK_MIN_PCT=1.5
SOLUSDC_PULLBACK_MIN_PCT=1.2
EMA_SLOPE_MIN_PCT=0.02

# Exits
PARTIAL_TP_PCT=1.0
PARTIAL_TP_RATIO=0.4
TRAILING_ACTIVATION_PCT=0.9
TRAILING_STOP_PCT=0.35

# Notifications
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
```

### 3. Run

```bash
docker compose up -d --build
docker logs eth-bot --follow
```

### 4. Dashboard

Visit `http://your-ec2-ip:5000`

---

## Backtesting

```bash
source venv/bin/activate

# Full history — SOL
python -m backtest.run_backtest \
  --symbol SOLUSDC --start 2024-01-01 \
  --stop-loss 0.5 --rsi-oversold 38 \
  --pullback-min 1.2 --ema-slope-min 0.02 --atr-mult 0.0 \
  --partial-tp 1.0 --partial-ratio 0.4 \
  --trail-activation 0.9 --trail-stop 0.35 \
  --rsi-1h-min 50.0

# With cooldown simulation (mirrors live bot behaviour)
python -m backtest.run_backtest --symbol SOLUSDC --start 2026-01-01 \
  --stop-loss 0.5 --rsi-oversold 38 --pullback-min 1.2 \
  --ema-slope-min 0.02 --atr-mult 0.0 --partial-tp 1.0 \
  --partial-ratio 0.4 --trail-activation 0.9 --trail-stop 0.35 \
  --rsi-1h-min 50.0 --cooldown-min 15

# Disable cooldown to see raw signal performance
python -m backtest.run_backtest --symbol SOLUSDC --start 2026-01-01 \
  [same params] --no-cooldown

# Save results to CSV
python -m backtest.run_backtest --symbol BTCUSDC --save-csv
```

---

## Useful Commands

```bash
# Bot status
docker ps --format "table {{.Names}}\t{{.Status}}"
docker logs eth-bot --tail 20

# Trade history
sudo sqlite3 /var/lib/docker/volumes/eth-bot_data/_data/trades.db \
  "SELECT symbol, side, price, pnl_usdc, reason, timestamp FROM trades ORDER BY id DESC LIMIT 10;"

# Per-symbol summary
sudo sqlite3 /var/lib/docker/volumes/eth-bot_data/_data/trades.db \
  "SELECT symbol, COUNT(*) as trades, ROUND(SUM(pnl_usdc),2) as pnl FROM trades WHERE symbol IS NOT NULL GROUP BY symbol;"

# Restart bot
docker compose restart eth-bot

# Rebuild after code changes
docker compose build eth-bot && docker compose up -d --force-recreate eth-bot

# Run tests
source venv/bin/activate && pytest tests/ -v
```

---

## Going Live Checklist

- [ ] 10+ testnet trades with clean BUY + SELL DB logging across all pairs
- [ ] Win rate and PnL tracking as expected on dashboard
- [ ] Set `TESTNET=false` in `.env`
- [ ] Run `harden_secrets.sh`
- [ ] Set Elastic IP on EC2
- [ ] Whitelist EC2 IP on Binance API key
- [ ] Merge `feature/multi-pair` → `main`
- [ ] Tag release `v1.0.0`

---

## CI/CD

GitHub Actions runs on every push to any branch:
- `ruff` lint check
- `pytest` — 50 tests covering strategy logic, indicators, risk manager, position store

---

## Economics (€2,000 capital, 3 pairs)

| Pair | Est. trades/month | Est. monthly return |
|------|------------------|-------------------|
| ETHUSDC | ~1.5 | ~€6 |
| BTCUSDC | ~1.0 | ~€5 |
| SOLUSDC | ~4.8 | ~€21 |
| **Total** | **~7.3** | **~€33/month** |

Projected 24-month compounding: ~€2,700 (~17% annually, max drawdown under 3.5%)

*Based on 2024–2025 bull market backtest results. Bear market returns will be lower.*

---

## License

Private repository. All rights reserved.
