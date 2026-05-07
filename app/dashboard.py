"""
Dashboard
─────────
Flask web dashboard with SQLite persistence.
- Multi-pair support (ETHUSDC, BTCUSDC, etc.)
- All trades saved to trades.db — survives restarts
- Live balance for all held assets
- Open positions per symbol with trailing stop tracker
- Signal log — last 10 ticks per symbol
- Date + time in trade history
"""

import json
import os
import sqlite3
from collections import deque
from datetime import datetime
from threading import Lock, Thread

from flask import Flask, jsonify, render_template_string, request
from loguru import logger

from app.config import settings
from app.exchange.binance_client import BinanceClient
from app.services.coingecko import CoinGeckoSentiment

app = Flask(__name__)

DB_PATH = os.environ.get("DB_PATH", "/app/data/trades.db")
_db_lock = Lock()

_state = {
    "prices": {},           # {symbol: price}
    "price_updated": None,
    "sentiment": None,
    "positions": {},        # {symbol: position_dict}
    "trades": [],
    "daily_pnl": 0.0,
    "daily_trades": 0,
    "bot_running": False,
    "last_error": None,
    "balance_usdc": 0.0,
    "balances": {},         # {asset: amount}
    "total_value_usdc": 0.0,
    "trailing_activation_pct": settings.trailing_activation_pct,
    "trailing_stop_pct": settings.trailing_stop_pct,
    "stop_loss_pct": settings.stop_loss_pct,
    "symbols": settings.active_symbols,
}

# Signal log — last 10 ticks per symbol
_signal_logs: dict[str, deque] = {
    sym: deque(maxlen=10) for sym in settings.active_symbols
}

_client = None
_coingecko = CoinGeckoSentiment()


# ── SQLite ────────────────────────────────────────────────────────────────────

def _init_db():
    with _db_lock:
        con = sqlite3.connect(DB_PATH)
        con.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                symbol    TEXT,
                side      TEXT,
                price     REAL,
                quantity  REAL,
                pnl_usdc  REAL,
                pnl_pct   REAL,
                daily_pnl REAL,
                reason    TEXT,
                raw_json  TEXT
            )
        """)
        con.commit()
        con.close()
    logger.info(f"Dashboard: SQLite DB ready at {DB_PATH}")


def _save_trade(trade: dict):
    with _db_lock:
        con = sqlite3.connect(DB_PATH)
        con.execute(
            """INSERT INTO trades
               (timestamp, symbol, side, price, quantity, pnl_usdc, pnl_pct, daily_pnl, reason, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                trade.get("timestamp"),
                trade.get("symbol", settings.active_symbols[0]),
                trade.get("side"),
                trade.get("price"),
                trade.get("quantity"),
                trade.get("pnl_usdc"),
                trade.get("pnl_pct"),
                trade.get("daily_pnl"),
                trade.get("reason"),
                json.dumps(trade),
            ),
        )
        con.commit()
        con.close()


def _load_trades() -> list:
    db = os.environ.get("DB_PATH", "/app/data/trades.db")
    con = sqlite3.connect(db)
    rows = con.execute("SELECT raw_json FROM trades ORDER BY id DESC LIMIT 200").fetchall()
    trades = []
    for (raw,) in rows:
        trades.append(json.loads(raw))

    # Prepend any open positions
    try:
        open_rows = con.execute("SELECT * FROM open_position").fetchall()
        cols = [c[1] for c in con.execute("PRAGMA table_info(open_position)").fetchall()]
        for row in open_rows:
            pos = dict(zip(cols, row))
            already_present = any(
                t.get("side") == "BUY" and
                t.get("symbol") == pos["symbol"] and
                abs(t.get("price", 0) - pos["entry_price"]) < 0.01
                for t in trades[:5]
            )
            if not already_present:
                trades.insert(0, {
                    "symbol": pos["symbol"],
                    "side": "BUY",
                    "price": pos["entry_price"],
                    "quantity": pos["quantity"],
                    "reason": "signal",
                    "timestamp": pos["entry_time"],
                    "pnl_usdc": None,
                    "pnl_pct": None,
                    "daily_pnl": None,
                    "peak_pct": None,
                })
    except Exception:
        pass

    con.close()
    return trades


def _compute_daily_pnl(trades: list) -> tuple:
    today = datetime.utcnow().date().isoformat()
    daily_pnl = 0.0
    daily_trades = 0
    for t in trades:
        ts = (t.get("timestamp") or "")[:10]
        if ts == today and t.get("side") == "SELL":
            daily_pnl += t.get("pnl_usdc") or 0.0
            daily_trades += 1
    return round(daily_pnl, 4), daily_trades


# ── Background refresh ────────────────────────────────────────────────────────

def _refresh_loop():
    global _client
    try:
        _client = BinanceClient()
        _state["bot_running"] = True
    except Exception as e:
        _state["last_error"] = str(e)
        return

    import time

    while True:
        try:
            total_value = 0.0
            usdc = _client.get_balance("USDC")
            _state["balance_usdc"] = round(usdc, 2)
            total_value += usdc

            for sym in settings.active_symbols:
                price = _client.get_price(sym)
                _state["prices"][sym] = price
                base = sym.replace("USDC", "").replace("USDT", "")
                base_bal = _client.get_balance(base)
                _state["balances"][base] = round(base_bal, 6)
                total_value += base_bal * price

            _state["total_value_usdc"] = round(total_value, 2)
            _state["price_updated"] = datetime.utcnow().strftime("%H:%M:%S UTC")

            # Update open position current prices
            for sym, pos in _state["positions"].items():
                if pos and sym in _state["prices"]:
                    price = _state["prices"][sym]
                    pos["current_price"] = price
                    if price > pos.get("peak_price", 0):
                        pos["peak_price"] = price

            cg = _coingecko.get_sentiment()
            if cg:
                _state["sentiment"] = {
                    "label": cg.sentiment,
                    "strength": cg.signal_strength,
                    "change_1h": cg.change_1h,
                    "change_24h": cg.change_24h,
                    "change_7d": cg.change_7d,
                    "summary": cg.summary,
                }
            _state["last_error"] = None
        except Exception as e:
            _state["last_error"] = str(e)
        time.sleep(15)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/state")
def api_state():
    state = dict(_state)
    # Load all open positions from DB
    try:
        db = os.environ.get("DB_PATH", "/app/data/trades.db")
        con = sqlite3.connect(db)
        open_rows = con.execute("SELECT * FROM open_position").fetchall()
        cols = [c[1] for c in con.execute("PRAGMA table_info(open_position)").fetchall()]
        con.close()
        positions = {}
        for row in open_rows:
            pos = dict(zip(cols, row))
            sym = pos["symbol"]
            pos["current_price"] = state["prices"].get(sym, pos["entry_price"])
            pos["peak_price"] = pos.get("peak_price") or pos["current_price"]
            positions[sym] = pos
        state["positions"] = positions
    except Exception:
        state["positions"] = {}

    # Include signal logs per symbol
    state["signal_logs"] = {sym: list(log) for sym, log in _signal_logs.items()}
    # Keep legacy signal_log for backwards compat — use first symbol
    first = settings.active_symbols[0]
    state["signal_log"] = list(_signal_logs.get(first, []))
    return jsonify(state)


@app.route("/health")
def health():
    import time as _time
    _time.time()
    price_age = None
    if _state.get("price_updated"):
        try:
            last = datetime.strptime(_state["price_updated"], "%H:%M:%S UTC").replace(
                year=datetime.utcnow().year,
                month=datetime.utcnow().month,
                day=datetime.utcnow().day,
            )
            price_age = round((datetime.utcnow() - last).total_seconds(), 1)
        except Exception:
            price_age = None

    price_fresh = price_age is not None and price_age < 60
    healthy = _state.get("bot_running", False) and price_fresh

    payload = {
        "status": "ok" if healthy else "degraded",
        "prices": _state.get("prices"),
        "price_age_s": price_age,
        "bot_running": _state.get("bot_running", False),
        "last_error": _state.get("last_error"),
        "open_positions": list(_state.get("positions", {}).keys()),
    }
    return jsonify(payload), (200 if healthy else 503)


@app.route("/api/trade", methods=["POST"])
def add_trade():
    trade = request.json
    trade["timestamp"] = datetime.utcnow().isoformat()
    symbol = trade.get("symbol", settings.active_symbols[0])

    _save_trade(trade)

    _state["trades"].insert(0, trade)
    _state["trades"] = _state["trades"][:200]
    _state["daily_pnl"], _state["daily_trades"] = _compute_daily_pnl(_state["trades"])

    if trade["side"] == "BUY":
        _state["positions"][symbol] = {
            "symbol": symbol,
            "entry_price": trade["price"],
            "quantity": trade["quantity"],
            "entry_time": trade["timestamp"],
            "current_price": _state["prices"].get(symbol, trade["price"]),
            "peak_price": _state["prices"].get(symbol, trade["price"]),
        }
    elif trade["side"] == "SELL":
        _state["positions"].pop(symbol, None)

    return jsonify({"ok": True})


@app.route("/api/signal", methods=["POST"])
def add_signal():
    signal = request.json
    symbol = signal.get("symbol", settings.active_symbols[0])
    if symbol not in _signal_logs:
        _signal_logs[symbol] = deque(maxlen=10)
    _signal_logs[symbol].appendleft(signal)
    return jsonify({"ok": True})

# ── HTML ──────────────────────────────────────────────────────────────────────

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Crypto Bot Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f1117; color: #e2e8f0; min-height: 100vh; padding: 24px; }
  h1 { font-size: 20px; font-weight: 600; color: #f8fafc; margin-bottom: 20px; display: flex; align-items: center; gap: 10px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #22c55e; animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
  .grid-5 { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 14px; }
  .grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 14px; }
  .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 14px; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 14px; }
  .card { background: #1e2130; border: 1px solid #2d3148; border-radius: 12px; padding: 16px 20px; }
  .card-label { font-size: 12px; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
  .card-value { font-size: 22px; font-weight: 700; color: #f8fafc; }
  .card-sub { font-size: 12px; color: #64748b; margin-top: 4px; }
  .positive { color: #22c55e; }
  .negative { color: #ef4444; }
  .warning { color: #f59e0b; }
  .balance-card { background: #0f1f17; border: 1px solid #166534; border-radius: 12px; padding: 16px 20px; }
  .balance-total { font-size: 24px; font-weight: 700; color: #22c55e; }
  .sentiment-bar { height: 6px; border-radius: 3px; background: #2d3148; margin-top: 10px; overflow: hidden; }
  .sentiment-fill { height: 100%; border-radius: 3px; transition: width 0.5s ease; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 10px 12px; color: #64748b; font-weight: 500; font-size: 11px; text-transform: uppercase; border-bottom: 1px solid #2d3148; }
  td { padding: 10px 12px; border-bottom: 1px solid #1a1f35; color: #cbd5e1; }
  tr:last-child td { border-bottom: none; }
  .side-buy { color: #22c55e; font-weight: 600; }
  .side-sell { color: #f97316; font-weight: 600; }
  .reason-trailing { color: #a78bfa; }
  .reason-stoploss { color: #ef4444; }
  .reason-tp { color: #22c55e; }
  .reason-partial { color: #60a5fa; }
  .chart-wrap { position: relative; height: 200px; }
  .section-title { font-size: 13px; font-weight: 600; color: #94a3b8; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.05em; }
  .position-card { background: #0f2a1a; border: 1px solid #166534; border-radius: 12px; padding: 18px 20px; margin-bottom: 14px; }
  .no-position { background: #1e2130; border: 1px dashed #2d3148; border-radius: 12px; padding: 14px 20px; margin-bottom: 14px; text-align: center; color: #475569; font-size: 13px; }
  .trail-bar-wrap { margin-top: 14px; }
  .trail-bar-label { display: flex; justify-content: space-between; font-size: 11px; color: #64748b; margin-bottom: 5px; }
  .trail-bar-bg { height: 8px; border-radius: 4px; background: #1a1f35; position: relative; overflow: visible; }
  .trail-bar-fill { height: 100%; border-radius: 4px; transition: width 0.5s ease; }
  .trail-marker { position: absolute; top: -4px; width: 2px; height: 16px; background: #f59e0b; border-radius: 1px; }
  .pos-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 14px; }
  .pos-grid-bottom { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }
  .pos-stat-label { font-size: 11px; color: #64748b; margin-bottom: 3px; }
  .pos-stat-value { font-size: 15px; font-weight: 600; color: #f8fafc; }
  .signal-table { width: 100%; border-collapse: collapse; font-size: 11px; font-family: 'SF Mono', 'Fira Code', monospace; }
  .signal-table th { padding: 6px 8px; color: #475569; font-weight: 500; border-bottom: 1px solid #2d3148; text-align: center; }
  .signal-table td { padding: 5px 8px; border-bottom: 1px solid #1a1f35; text-align: center; color: #94a3b8; }
  .signal-table tr:first-child td { color: #e2e8f0; background: #161b2e; }
  .sig-ok { color: #22c55e; }
  .sig-warn { color: #ef4444; }
  .sig-neutral { color: #64748b; }
  .sig-active { color: #f59e0b; }
  .sym-badge { display: inline-block; padding: 2px 8px; border-radius: 6px; font-size: 11px; font-weight: 700; background: #1e3a5f; color: #60a5fa; }
  .sym-badge.btc { background: #2a1f0a; color: #f59e0b; }
  .tab-bar { display: flex; gap: 8px; margin-bottom: 12px; }
  .tab-btn { padding: 6px 16px; border-radius: 8px; border: 1px solid #2d3148; background: #1e2130; color: #64748b; font-size: 12px; cursor: pointer; font-weight: 600; }
  .tab-btn.active { background: #1e3a5f; border-color: #3b82f6; color: #60a5fa; }
  .tab-btn.active.btc { background: #2a1f0a; border-color: #f59e0b; color: #f59e0b; }
</style>
</head>
<body>

<h1><div class="dot"></div> Crypto Bot Dashboard</h1>

<div class="grid-5">
  <div class="card">
    <div class="card-label">Prices</div>
    <div id="prices-display" style="font-size:14px;line-height:1.8;margin-top:4px;">—</div>
    <div class="updated" id="price-updated" style="font-size:11px;color:#475569;margin-top:4px;">—</div>
  </div>
  <div class="balance-card">
    <div class="card-label">💰 Total Value</div>
    <div class="balance-total" id="total-value">$0.00</div>
    <div class="card-sub" id="balance-sub">loading...</div>
  </div>
  <div class="card">
    <div class="card-label">USDC Balance</div>
    <div class="card-value" id="balance-usdc">$0.00</div>
    <div class="card-sub">free / available</div>
  </div>
  <div class="card">
    <div class="card-label">Holdings</div>
    <div id="holdings-display" style="font-size:13px;line-height:1.8;margin-top:4px;">—</div>
  </div>
  <div class="card">
    <div class="card-label">Daily PnL</div>
    <div class="card-value" id="daily-pnl">$0.00</div>
    <div class="card-sub" id="daily-trades">0 trades today</div>
  </div>
</div>

<div class="grid-3">
  <div class="card">
    <div class="card-label">Total Closed Trades</div>
    <div class="card-value" id="total-trades">0</div>
    <div class="card-sub">all time (from DB)</div>
  </div>
  <div class="card">
    <div class="card-label">Daily Loss Used</div>
    <div class="card-value" id="limit-used">$0.00</div>
    <div class="card-sub" id="limit-pct">0% of daily limit</div>
  </div>
  <div class="card">
    <div class="card-label">Bot Status</div>
    <div class="card-value" id="bot-status" style="font-size:16px;margin-top:4px;">—</div>
    <div class="card-sub" id="last-error" style="color:#ef4444;font-size:11px;"></div>
  </div>
</div>

<div id="position-section"></div>

<div class="grid-2">
  <div class="card">
    <div class="section-title">CoinGecko Sentiment</div>
    <div id="sentiment-content"><div style="color:#475569;font-size:13px;">Loading...</div></div>
  </div>
  <div class="card">
    <div class="section-title">Cumulative PnL</div>
    <div class="chart-wrap"><canvas id="pnlChart"></canvas></div>
  </div>
</div>

<!-- Signal Log -->
<div class="card" style="margin-bottom:14px;">
  <div class="section-title">Signal Log — Last 10 Ticks</div>
  <div class="tab-bar" id="signal-tabs"></div>
  <div style="overflow-x:auto;">
    <table class="signal-table">
      <thead><tr>
        <th>Time</th><th>Symbol</th><th>Price</th><th>15m RSI</th><th>1h RSI</th>
        <th>EMA Slope</th><th>Pullback</th><th>Vol Ratio</th><th>ATR%</th>
        <th>Trend</th><th>50>200</th><th>Squeeze</th><th>Sentiment</th><th>Position</th><th>PnL%</th>
      </tr></thead>
      <tbody id="signal-tbody"><tr><td colspan="15" style="color:#475569;text-align:center;padding:12px;">Waiting for ticks...</td></tr></tbody>
    </table>
  </div>
</div>

<div class="card">
  <div class="section-title">Trade History</div>
  <table>
    <thead><tr>
      <th>Date / Time</th><th>Symbol</th><th>Side</th><th>Price</th><th>Qty</th>
      <th>Spent/Received</th><th>Trade PnL</th><th>Peak</th><th>Day PnL</th><th>Reason</th>
    </tr></thead>
    <tbody id="trade-tbody"><tr><td colspan="10" style="color:#475569;text-align:center;">No trades yet</td></tr></tbody>
  </table>
</div>

<script>
let pnlChart;
let activeSignalTab = null;
let allSignalLogs = {};

function initChart() {
  const ctx = document.getElementById('pnlChart').getContext('2d');
  pnlChart = new Chart(ctx, {
    type: 'line',
    data: { labels: [], datasets: [{ label: 'PnL (USDC)', data: [], borderColor: '#22c55e', backgroundColor: 'rgba(34,197,94,0.08)', borderWidth: 2, pointRadius: 3, fill: true, tension: 0.3 }] },
    options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } },
      scales: { x: { ticks: { color: '#475569', font: { size: 10 } }, grid: { color: '#1a1f35' } }, y: { ticks: { color: '#475569', font: { size: 10 }, callback: v => '$'+v.toFixed(2) }, grid: { color: '#1a1f35' } } }
    }
  });
}

function symBadge(sym) {
  const isBtc = sym && sym.includes('BTC');
  return `<span class="sym-badge${isBtc?' btc':''}">${sym||'—'}</span>`;
}

function fmtDateTime(iso) {
  if (!iso) return '—';
  return `<span style="color:#94a3b8;">${iso.slice(0,10)}</span> ${iso.slice(11,16)} UTC`;
}

function fmtPnl(v) {
  if (v == null) return '—';
  const cls = v >= 0 ? 'positive' : 'negative';
  return `<span class="${cls}">${v>=0?'+':''}$${Math.abs(parseFloat(v)).toFixed(2)}</span>`;
}

function fmtReason(r) {
  if (!r) return '—';
  const map = {
    'trailing_stop': '<span class="reason-trailing">🎯 Trail Stop</span>',
    'stop_loss': '<span class="reason-stoploss">🛑 Stop Loss</span>',
    'take_profit': '<span class="reason-tp">✅ Take Profit</span>',
    'partial_take_profit': '<span class="reason-partial">💰 Partial TP</span>',
    'signal': '<span style="color:#64748b;">Signal</span>',
  };
  return map[r] || `<span style="color:#64748b;">${r}</span>`;
}

function updateSentiment(s) {
  if (!s) { document.getElementById('sentiment-content').innerHTML = '<div style="color:#475569;font-size:13px;">Unavailable</div>'; return; }
  const fc = s.label==='bullish'?'#22c55e':s.label==='bearish'?'#ef4444':'#64748b';
  document.getElementById('sentiment-content').innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;">
      <span style="background:${s.label==='bullish'?'#14532d':s.label==='bearish'?'#450a0a':'#1e293b'};color:${fc};padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;">${s.label}</span>
      <span style="font-size:13px;color:#94a3b8;">${s.strength}/100</span>
    </div>
    <div class="sentiment-bar"><div class="sentiment-fill" style="width:${s.strength}%;background:${fc};"></div></div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:14px;">
      <div style="text-align:center;"><div style="font-size:11px;color:#475569;margin-bottom:2px;">1H</div><div style="font-size:15px;font-weight:600;" class="${s.change_1h>=0?'positive':'negative'}">${s.change_1h>=0?'+':''}${parseFloat(s.change_1h).toFixed(2)}%</div></div>
      <div style="text-align:center;"><div style="font-size:11px;color:#475569;margin-bottom:2px;">24H</div><div style="font-size:15px;font-weight:600;" class="${s.change_24h>=0?'positive':'negative'}">${s.change_24h>=0?'+':''}${parseFloat(s.change_24h).toFixed(2)}%</div></div>
      <div style="text-align:center;"><div style="font-size:11px;color:#475569;margin-bottom:2px;">7D</div><div style="font-size:15px;font-weight:600;" class="${s.change_7d>=0?'positive':'negative'}">${s.change_7d>=0?'+':''}${parseFloat(s.change_7d).toFixed(2)}%</div></div>
    </div>`;
}

function updateSignalTabs(symbols) {
  const tabBar = document.getElementById('signal-tabs');
  if (!activeSignalTab || !symbols.includes(activeSignalTab)) activeSignalTab = symbols[0];
  tabBar.innerHTML = symbols.map(sym => {
    const isBtc = sym.includes('BTC');
    return `<button class="tab-btn${sym===activeSignalTab?' active'+(isBtc?' btc':''):''}" onclick="setSignalTab('${sym}')">${sym}</button>`;
  }).join('');
}

function setSignalTab(sym) {
  activeSignalTab = sym;
  renderSignalLog();
  document.querySelectorAll('.tab-btn').forEach(b => {
    const isBtc = sym.includes('BTC');
    b.className = 'tab-btn' + (b.textContent===sym ? ' active'+(isBtc?' btc':'') : '');
  });
}

function renderSignalLog() {
  const signals = allSignalLogs[activeSignalTab] || [];
  const tbody = document.getElementById('signal-tbody');
  if (!signals.length) { tbody.innerHTML = '<tr><td colspan="15" style="color:#475569;text-align:center;padding:12px;">Waiting for ticks...</td></tr>'; return; }
  tbody.innerHTML = signals.map(s => {
    const rsiCls = s.rsi_15m < 40 ? 'sig-ok' : s.rsi_15m > 60 ? 'sig-warn' : 'sig-neutral';
    const rsi1hCls = s.rsi_1h > 50 ? 'sig-ok' : 'sig-warn';
    const trendCls = s.trend ? 'sig-ok' : 'sig-warn';
    const crossCls = s.ema_cross ? 'sig-ok' : 'sig-warn';
    const slopeCls = s.ema_slope > 0 ? 'sig-ok' : 'sig-warn';
    const squeezeStr = s.squeeze ? '<span class="sig-active">🔥</span>' : '<span class="sig-neutral">—</span>';
    const sentCls = s.sentiment === 'bullish' ? 'sig-ok' : s.sentiment === 'bearish' ? 'sig-warn' : 'sig-neutral';
    const posCls = s.position ? 'sig-active' : 'sig-neutral';
    const pnlStr = s.pnl_pct != null
      ? `<span class="${s.pnl_pct >= 0 ? 'sig-ok' : 'sig-warn'}">${s.pnl_pct >= 0 ? '+' : ''}${s.pnl_pct}%</span>`
      : '<span class="sig-neutral">—</span>';
    return `<tr>
      <td>${s.timestamp||'—'}</td>
      <td>${symBadge(s.symbol||activeSignalTab)}</td>
      <td>$${s.price}</td>
      <td class="${rsiCls}">${s.rsi_15m}</td>
      <td class="${rsi1hCls}">${s.rsi_1h}</td>
      <td class="${slopeCls}">${s.ema_slope > 0 ? '+' : ''}${s.ema_slope}%</td>
      <td>${s.pullback}%</td>
      <td>${s.vol_ratio}x</td>
      <td>${s.atr_pct}%</td>
      <td class="${trendCls}">${s.trend ? '✅' : '❌'}</td>
      <td class="${crossCls}">${s.ema_cross ? '✅' : '❌'}</td>
      <td>${squeezeStr}</td>
      <td class="${sentCls}">${s.sentiment}</td>
      <td class="${posCls}">${s.position ? 'OPEN' : '—'}</td>
      <td>${pnlStr}</td>
    </tr>`;
  }).join('');
}

function renderPosition(sym, pos, prices, s) {
  if (!pos) return `<div class="no-position">[${sym}] No open position — watching for signals 👁</div>`;

  const price = prices[sym] || pos.entry_price;
  const cp = pos.current_price || price;
  const peak = pos.peak_price || cp;
  const entry = pos.entry_price;

  const pnlPct = ((cp - entry) / entry * 100);
  const pnlUsdc = ((cp - entry) * pos.quantity);
  const peakPct = ((peak - entry) / entry * 100);
  const pnlCls = pnlPct >= 0 ? 'positive' : 'negative';

  const activationPct = s.trailing_activation_pct || 0.9;
  const trailPct = s.trailing_stop_pct || 0.35;
  const hardStopPct = s.stop_loss_pct || 0.5;

  const activationPrice = entry * (1 + activationPct / 100);
  const trailActive = peakPct >= activationPct;
  const trailStopLevel = peak * (1 - trailPct / 100);
  const hardStopLevel = entry * (1 - hardStopPct / 100);

  const barMin = hardStopLevel * 0.999;
  const barMax = Math.max(peak * 1.005, activationPrice * 1.01);
  const barRange = barMax - barMin;
  const currentPct = Math.max(0, Math.min(100, (cp - barMin) / barRange * 100));
  const trailLevelPct = Math.max(0, Math.min(100, (trailStopLevel - barMin) / barRange * 100));
  const activationPctBar = Math.max(0, Math.min(100, (activationPrice - barMin) / barRange * 100));
  const barColor = trailActive ? '#22c55e' : '#3b82f6';
  const base = sym.replace('USDC','').replace('USDT','');

  return `
    <div class="position-card">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;">
        <div style="display:flex;align-items:center;gap:10px;">
          <div class="section-title" style="color:#22c55e;margin-bottom:0;">⚡ Open Position</div>
          ${symBadge(sym)}
        </div>
        <div>
          ${trailActive
            ? '<span style="background:#14532d;color:#22c55e;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;">🎯 TRAILING ACTIVE</span>'
            : `<span style="background:#1e3a5f;color:#60a5fa;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;">⏳ WAITING FOR +${activationPct}%</span>`
          }
        </div>
      </div>
      <div class="pos-grid">
        <div class="pos-stat"><div class="pos-stat-label">Entry Price</div><div class="pos-stat-value">$${parseFloat(entry).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:4})}</div></div>
        <div class="pos-stat"><div class="pos-stat-label">Current Price</div><div class="pos-stat-value">$${parseFloat(cp).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:4})}</div></div>
        <div class="pos-stat"><div class="pos-stat-label">Unrealised PnL</div><div class="pos-stat-value ${pnlCls}">${pnlPct>=0?'+':''}$${Math.abs(pnlUsdc).toFixed(2)} (${pnlPct.toFixed(2)}%)</div></div>
        <div class="pos-stat"><div class="pos-stat-label">Quantity</div><div class="pos-stat-value">${parseFloat(pos.quantity).toFixed(5)} ${base}</div></div>
      </div>
      <div class="pos-grid-bottom">
        <div class="pos-stat"><div class="pos-stat-label">🏔 Peak</div><div class="pos-stat-value" style="color:#f59e0b;">$${parseFloat(peak).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:4})} (+${peakPct.toFixed(2)}%)</div></div>
        <div class="pos-stat"><div class="pos-stat-label">${trailActive?'🎯 Trail Stop':'🎯 Trail Activates At'}</div><div class="pos-stat-value" style="color:#a78bfa;">$${trailActive?trailStopLevel.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:4}):activationPrice.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:4})}</div></div>
        <div class="pos-stat"><div class="pos-stat-label">🛑 Hard Stop</div><div class="pos-stat-value" style="color:#ef4444;">$${hardStopLevel.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:4})}</div></div>
        <div class="pos-stat"><div class="pos-stat-label">Opened</div><div class="pos-stat-value" style="font-size:13px;">${pos.entry_time?pos.entry_time.slice(0,10)+' '+pos.entry_time.slice(11,16)+' UTC':'—'}</div></div>
      </div>
      <div class="trail-bar-wrap">
        <div class="trail-bar-label"><span style="color:#ef4444;">🛑 $${hardStopLevel.toFixed(2)}</span><span style="color:#94a3b8;">Price Range</span><span style="color:#f59e0b;">🏔 $${peak.toFixed(2)}</span></div>
        <div class="trail-bar-bg">
          <div class="trail-bar-fill" style="width:${currentPct}%;background:${barColor};"></div>
          ${trailActive
            ? `<div class="trail-marker" style="left:${trailLevelPct}%;"></div>`
            : `<div class="trail-marker" style="left:${activationPctBar}%;background:#3b82f6;"></div>`
          }
        </div>
        <div style="font-size:11px;color:#64748b;margin-top:5px;text-align:center;">
          ${trailActive
            ? `Trail stop at $${trailStopLevel.toFixed(2)} — locking in ${Math.max(0,((trailStopLevel-entry)/entry*100)).toFixed(2)}%`
            : `Trailing activates at $${activationPrice.toFixed(2)} (+${activationPct}%)`
          }
        </div>
      </div>
    </div>`;
}

function updateTrades(trades) {
  const sells = trades.filter(t => t.side === 'SELL');
  document.getElementById('total-trades').textContent = sells.length;
  const tbody = document.getElementById('trade-tbody');
  if (!trades.length) return;
  tbody.innerHTML = trades.map(t => {
    const val = (t.price * t.quantity).toFixed(2);
    const spent = t.side==='BUY'
      ? `<span style="color:#ef4444;">-$${val}</span>`
      : `<span style="color:#22c55e;">+$${val}</span>`;
    const peakCol = t.peak_pct != null
      ? `<span style="color:#f59e0b;">+${parseFloat(t.peak_pct).toFixed(2)}%</span>` : '—';
    return `<tr>
      <td>${fmtDateTime(t.timestamp)}</td>
      <td>${symBadge(t.symbol)}</td>
      <td class="side-${t.side.toLowerCase()}">${t.side}</td>
      <td>$${parseFloat(t.price).toFixed(4)}</td>
      <td>${parseFloat(t.quantity).toFixed(5)}</td>
      <td>${spent}</td>
      <td>${t.pnl_usdc!=null?fmtPnl(t.pnl_usdc):'—'}</td>
      <td>${peakCol}</td>
      <td>${t.daily_pnl!=null?fmtPnl(t.daily_pnl):'—'}</td>
      <td>${fmtReason(t.reason)}</td>
    </tr>`;
  }).join('');

  const sorted = [...sells].reverse();
  let running = 0;
  const labels = sorted.map(t => t.timestamp ? t.timestamp.slice(0,10)+' '+t.timestamp.slice(11,16) : '');
  const values = sorted.map(t => { running += (t.pnl_usdc||0); return parseFloat(running.toFixed(2)); });
  if (pnlChart) { pnlChart.data.labels=labels; pnlChart.data.datasets[0].data=values; pnlChart.update(); }
}

async function refresh() {
  try {
    const res = await fetch('/api/state');
    const s = await res.json();

    // Prices
    const prices = s.prices || {};
    const pricesHtml = Object.entries(prices).map(([sym, p]) =>
      `${symBadge(sym)} <span style="font-weight:600;color:#f8fafc;">$${parseFloat(p).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}</span>`
    ).join('<br>');
    document.getElementById('prices-display').innerHTML = pricesHtml || '—';
    document.getElementById('price-updated').textContent = s.price_updated || '—';

    // Balances
    const usdc = parseFloat(s.balance_usdc||0);
    const total = parseFloat(s.total_value_usdc||0);
    document.getElementById('total-value').textContent = '$'+total.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
    document.getElementById('balance-usdc').textContent = '$'+usdc.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});

    const balances = s.balances || {};
    const holdingsHtml = Object.entries(balances).map(([asset, amt]) => {
      const sym = asset+'USDC';
      const val = prices[sym] ? (amt * prices[sym]).toFixed(2) : '—';
      return `<span style="color:#94a3b8;">${asset}:</span> <span style="font-weight:600;">${parseFloat(amt).toFixed(5)}</span> <span style="color:#475569;">≈$${val}</span>`;
    }).join('<br>');
    document.getElementById('holdings-display').innerHTML = holdingsHtml || '—';
    document.getElementById('balance-sub').textContent = '$'+usdc.toFixed(2)+' USDC free';

    // Daily PnL
    const pnl = parseFloat(s.daily_pnl||0);
    const pnlEl = document.getElementById('daily-pnl');
    pnlEl.textContent = (pnl>=0?'+':'')+' $'+Math.abs(pnl).toFixed(2);
    pnlEl.className = 'card-value '+(pnl>0?'positive':pnl<0?'negative':'');
    document.getElementById('daily-trades').textContent = (s.daily_trades||0)+' trades today';

    const lossUsed = Math.abs(Math.min(0,pnl));
    const limitEl = document.getElementById('limit-used');
    limitEl.textContent = '-$'+lossUsed.toFixed(2);
    limitEl.className = 'card-value '+(lossUsed>0?'negative':'');
    document.getElementById('limit-pct').textContent = (lossUsed/45*100).toFixed(0)+'% of daily limit';

    document.getElementById('bot-status').innerHTML = s.bot_running
      ? '<span class="positive">Running ✓</span>'
      : '<span class="negative">Offline</span>';
    document.getElementById('last-error').textContent = s.last_error || '';

    // Positions — render one card per symbol
    const symbols = s.symbols || Object.keys(prices);
    const positions = s.positions || {};
    const posHtml = symbols.map(sym => renderPosition(sym, positions[sym]||null, prices, s)).join('');
    document.getElementById('position-section').innerHTML = posHtml;

    updateSentiment(s.sentiment);
    updateTrades(s.trades||[]);

    // Signal log tabs
    allSignalLogs = s.signal_logs || {};
    updateSignalTabs(symbols);
    renderSignalLog();

  } catch(e) { console.error(e); }
}

initChart(); refresh(); setInterval(refresh, 10000);
</script>
</body>
</html>
"""


def start():
    _init_db()
    trades = _load_trades()
    _state["trades"] = trades
    _state["daily_pnl"], _state["daily_trades"] = _compute_daily_pnl(trades)
    logger.info(f"Dashboard: loaded {len(trades)} trades from database")

    t = Thread(target=_refresh_loop, daemon=True)
    t.start()
    logger.info("Dashboard running at http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == "__main__":
    start()
