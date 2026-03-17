# 🤖 ETH/USDC Spot Trading Bot

A rules-based Ethereum trading bot for Binance Spot, built as a clean Python project with Docker, CI, and testnet support.

> ⚠️ **Risk Warning**: This bot does not guarantee any returns. A 2% daily profit is a hypothesis to test — not a promise. Always start on testnet. Never risk capital you cannot afford to lose.

---

## Architecture

```
eth-bot/
├── app/
│   ├── strategy/
│   │   ├── signal_engine.py   # Pure buy/sell logic (easy to test)
│   │   ├── risk_manager.py    # Daily loss limits, position sizing
│   │   └── indicators.py      # RSI, EMA, Bollinger, pullback %
│   ├── exchange/
│   │   └── binance_client.py  # Binance SDK wrapper (testnet/live)
│   ├── services/
│   │   ├── trader.py          # Main loop — ties everything together
│   │   └── notifier.py        # Telegram alerts
│   ├── models/
│   │   ├── position.py        # Open position state
│   │   └── trade_event.py     # Trade log entry
│   ├── config.py              # Pydantic settings from .env
│   └── main.py                # Entry point + logging setup
├── tests/
│   ├── test_signal_engine.py
│   └── test_risk_manager.py
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── .github/workflows/ci.yml
└── requirements.txt
```

---

## Strategy (v1)

| Step | Rule |
|------|------|
| **Trend filter** | Price must be above the 200 EMA on 1h candles |
| **Entry signal** | RSI(14) on 15m < 38 AND price pulled back ≥ 0.8% from recent high |
| **Take profit** | Close trade at +1.0% gain |
| **Stop loss** | Close trade at -0.7% loss |
| **Daily halt** | Bot stops trading if daily losses exceed $30 |
| **One position** | Only one open trade at a time |

---

## Quickstart

### 1. Clone & configure

```bash
git clone https://github.com/YOUR_USERNAME/eth-bot.git
cd eth-bot
cp .env.example .env
# Edit .env with your Binance API keys
```

### 2. Install dependencies

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Run tests

```bash
pytest tests/ -v
```

### 4. Run on testnet first (REQUIRED)

Make sure `.env` has `TESTNET=true`, then:

```bash
python -m app.main
```

### 5. Deploy with Docker

```bash
docker compose up -d
docker compose logs -f
```

---

## Configuration (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `BINANCE_API_KEY` | — | Your Binance API key |
| `BINANCE_API_SECRET` | — | Your Binance API secret |
| `TESTNET` | `true` | Use Binance Spot Testnet |
| `SYMBOL` | `ETHUSDC` | Trading pair |
| `TRADE_AMOUNT_USDC` | `100` | USDC spent per trade |
| `TAKE_PROFIT_PCT` | `1.0` | % gain to sell |
| `STOP_LOSS_PCT` | `0.7` | % loss to sell |
| `DAILY_LOSS_LIMIT_USDC` | `30` | Max daily loss before halt |
| `RSI_OVERSOLD` | `38` | RSI entry threshold |
| `PULLBACK_MIN_PCT` | `0.8` | Min pullback % for entry |
| `LOOP_INTERVAL_SECONDS` | `30` | Polling interval |
| `TELEGRAM_BOT_TOKEN` | — | Optional: Telegram notifications |
| `TELEGRAM_CHAT_ID` | — | Optional: Telegram chat ID |

---

## Deployment (VPS)

```bash
# On your server
git clone https://github.com/YOUR_USERNAME/eth-bot.git
cd eth-bot
cp .env.example .env
nano .env  # add real keys, set TESTNET=false only when ready
docker compose up -d
```

---

## Development phases

- **Phase 1** ✅ — Signal-only mode: bot prints signals without placing orders
- **Phase 2** 🔄 — Testnet: real order flow, no real money
- **Phase 3** ⏳ — Live with small capital + all risk rules active

---

## Security

- **Never commit `.env`** — it's in `.gitignore`
- Restrict your Binance API key to **Spot trading only**, no withdrawals
- Whitelist your server IP in Binance API settings
- Rotate keys if you ever accidentally expose them

---

## Disclaimer

This project is for educational purposes. Trading cryptocurrencies carries significant financial risk. Past performance does not guarantee future results.
