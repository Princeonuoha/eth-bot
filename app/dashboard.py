"""
Dashboard
─────────
Flask web dashboard with SQLite persistence.
- All trades saved to trades.db — survives restarts
- Live USDC + ETH balance
- Open position with trailing stop tracker
- Date + time in trade history
"""

import json
import os
import sqlite3
from datetime import datetime
from threading import Lock, Thread

from flask import Flask, jsonify, render_template_string, request
from loguru import logger

from app.config import settings
from app.exchange.binance_client import BinanceClient
from app.services.coingecko import CoinGeckoSentiment

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "trades.db")
_db_lock = Lock()

_state = {
    "price": 0.0,
    "price_updated": None,
    "sentiment": None,
    "position": None,
    "trades": [],
    "daily_pnl": 0.0,
    "daily_trades": 0,
    "bot_running": False,
    "last_error": None,
    "balance_usdc": 0.0,
    "balance_eth": 0.0,
    "total_value_usdc": 0.0,
    "trailing_activation_pct": 1.0,
    "trailing_stop_pct": 0.8,
    "stop_loss_pct": 1.5,
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
               (timestamp, side, price, quantity, pnl_usdc, pnl_pct, daily_pnl, reason, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                trade.get("timestamp"),
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
    with _db_lock:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute("SELECT raw_json FROM trades ORDER BY id DESC LIMIT 200").fetchall()
        con.close()
    trades = []
    for (raw,) in rows:
        try:
            trades.append(json.loads(raw))
        except Exception:
            pass
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
        _state["trailing_activation_pct"] = settings.trailing_activation_pct
        _state["trailing_stop_pct"] = settings.trailing_stop_pct
        _state["stop_loss_pct"] = settings.stop_loss_pct
    except Exception as e:
        _state["last_error"] = str(e)
        return

    import time

    while True:
        try:
            price = _client.get_price(settings.symbol)
            _state["price"] = price
            _state["price_updated"] = datetime.utcnow().strftime("%H:%M:%S UTC")

            usdc = _client.get_balance("USDC")
            eth = _client.get_balance("ETH")
            _state["balance_usdc"] = round(usdc, 2)
            _state["balance_eth"] = round(eth, 6)
            _state["total_value_usdc"] = round(usdc + eth * price, 2)

            # Keep position current price live + update peak
            if _state["position"]:
                _state["position"]["current_price"] = price
                if price > _state["position"].get("peak_price", 0):
                    _state["position"]["peak_price"] = price

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
    return jsonify(_state)


@app.route("/api/trade", methods=["POST"])
def add_trade():
    trade = request.json
    trade["timestamp"] = datetime.utcnow().isoformat()

    _save_trade(trade)

    _state["trades"].insert(0, trade)
    _state["trades"] = _state["trades"][:200]
    _state["daily_pnl"], _state["daily_trades"] = _compute_daily_pnl(_state["trades"])

    if trade["side"] == "BUY":
        _state["position"] = {
            "entry_price": trade["price"],
            "quantity": trade["quantity"],
            "entry_time": trade["timestamp"],
            "current_price": _state["price"] or trade["price"],
            "peak_price": _state["price"] or trade["price"],
        }
    elif trade["side"] == "SELL":
        _state["position"] = None

    return jsonify({"ok": True})


# ── HTML ───────────────────────────────────────────────────────────────────────

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ETH Bot Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f1117; color: #e2e8f0; min-height: 100vh; padding: 24px; }
  h1 { font-size: 20px; font-weight: 600; color: #f8fafc; margin-bottom: 20px; display: flex; align-items: center; gap: 10px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #22c55e; animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
  .grid-5 { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 14px; }
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
  .badge { display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 11px; font-weight: 600; text-transform: uppercase; }
  .badge-bullish { background: #14532d; color: #22c55e; }
  .badge-bearish { background: #450a0a; color: #ef4444; }
  .badge-neutral { background: #1e293b; color: #94a3b8; }
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
  .chart-wrap { position: relative; height: 200px; }
  .section-title { font-size: 13px; font-weight: 600; color: #94a3b8; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.05em; }
  .position-card { background: #0f2a1a; border: 1px solid #166534; border-radius: 12px; padding: 18px 20px; margin-bottom: 14px; }
  .no-position { background: #1e2130; border: 1px dashed #2d3148; border-radius: 12px; padding: 14px 20px; margin-bottom: 14px; text-align: center; color: #475569; font-size: 13px; }
  .trail-bar-wrap { margin-top: 14px; }
  .trail-bar-label { display: flex; justify-content: space-between; font-size: 11px; color: #64748b; margin-bottom: 5px; }
  .trail-bar-bg { height: 8px; border-radius: 4px; background: #1a1f35; position: relative; overflow: visible; }
  .trail-bar-fill { height: 100%; border-radius: 4px; transition: width 0.5s ease; }
  .trail-marker { position: absolute; top: -4px; width: 2px; height: 16px; background: #f59e0b; border-radius: 1px; }
  .updated { font-size: 11px; color: #475569; margin-top: 2px; }
  .pos-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 14px; }
  .pos-grid-bottom { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }
  .pos-stat { }
  .pos-stat-label { font-size: 11px; color: #64748b; margin-bottom: 3px; }
  .pos-stat-value { font-size: 15px; font-weight: 600; color: #f8fafc; }
</style>
</head>
<body>

<h1><div class="dot"></div> ETH Bot Dashboard</h1>

<div class="grid-5">
  <div class="card">
    <div class="card-label">ETH Price</div>
    <div class="card-value" id="price">—</div>
    <div class="updated" id="price-updated">—</div>
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
    <div class="card-label">ETH Holdings</div>
    <div class="card-value" id="balance-eth" style="font-size:18px;">0.00000</div>
    <div class="card-sub" id="eth-value-usdc">≈ $0.00</div>
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

<div class="card">
  <div class="section-title">Trade History</div>
  <table>
    <thead><tr>
      <th>Date / Time</th><th>Side</th><th>Price</th><th>Qty (ETH)</th><th>Spent/Received</th><th>Trade PnL</th><th>Peak</th><th>Day PnL</th><th>Reason</th>
    </tr></thead>
    <tbody id="trade-tbody"><tr><td colspan="9" style="color:#475569;text-align:center;">No trades yet</td></tr></tbody>
  </table>
</div>

<script>
let pnlChart;
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

function fmtDateTime(iso) {
  if (!iso) return '—';
  const date = iso.slice(0, 10);
  const time = iso.slice(11, 16);
  return `<span style="color:#94a3b8;">${date}</span> ${time} UTC`;
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
    'stop_loss':     '<span class="reason-stoploss">🛑 Stop Loss</span>',
    'take_profit':   '<span class="reason-tp">✅ Take Profit</span>',
    'signal':        '<span style="color:#64748b;">Signal</span>',
  };
  return map[r] || `<span style="color:#64748b;">${r}</span>`;
}

function updateSentiment(s) {
  if (!s) { document.getElementById('sentiment-content').innerHTML = '<div style="color:#475569;font-size:13px;">Unavailable</div>'; return; }
  const fc = s.label==='bullish'?'#22c55e':s.label==='bearish'?'#ef4444':'#64748b';
  document.getElementById('sentiment-content').innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;">
      <span class="badge badge-${s.label}">${s.label}</span>
      <span style="font-size:13px;color:#94a3b8;">${s.strength}/100</span>
    </div>
    <div class="sentiment-bar"><div class="sentiment-fill" style="width:${s.strength}%;background:${fc};"></div></div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:14px;">
      <div style="text-align:center;"><div style="font-size:11px;color:#475569;margin-bottom:2px;">1H</div><div style="font-size:15px;font-weight:600;" class="${s.change_1h>=0?'positive':'negative'}">${s.change_1h>=0?'+':''}${parseFloat(s.change_1h).toFixed(2)}%</div></div>
      <div style="text-align:center;"><div style="font-size:11px;color:#475569;margin-bottom:2px;">24H</div><div style="font-size:15px;font-weight:600;" class="${s.change_24h>=0?'positive':'negative'}">${s.change_24h>=0?'+':''}${parseFloat(s.change_24h).toFixed(2)}%</div></div>
      <div style="text-align:center;"><div style="font-size:11px;color:#475569;margin-bottom:2px;">7D</div><div style="font-size:15px;font-weight:600;" class="${s.change_7d>=0?'positive':'negative'}">${s.change_7d>=0?'+':''}${parseFloat(s.change_7d).toFixed(2)}%</div></div>
    </div>`;
}

function updatePosition(pos, price, s) {
  const el = document.getElementById('position-section');
  if (!pos) {
    el.innerHTML = '<div class="no-position">No open position — bot is watching for signals 👁</div>';
    return;
  }

  const cp = pos.current_price || price || pos.entry_price;
  const peak = pos.peak_price || cp;
  const entry = pos.entry_price;

  const pnlPct = ((cp - entry) / entry * 100);
  const pnlUsdc = ((cp - entry) * pos.quantity);
  const peakPct = ((peak - entry) / entry * 100);
  const pnlCls = pnlPct >= 0 ? 'positive' : 'negative';

  // Trailing stop settings from state
  const activationPct = s.trailing_activation_pct || 1.0;
  const trailPct = s.trailing_stop_pct || 0.8;
  const hardStopPct = s.stop_loss_pct || 1.5;

  const activationPrice = entry * (1 + activationPct / 100);
  const trailActive = peakPct >= activationPct;
  const trailStopLevel = peak * (1 - trailPct / 100);
  const hardStopLevel = entry * (1 - hardStopPct / 100);

  // Progress bar: from hard stop (-hardStopPct%) to peak
  const barMin = entry * (1 - hardStopPct / 100);
  const barMax = Math.max(peak * 1.005, activationPrice * 1.01);
  const barRange = barMax - barMin;
  const currentPct = Math.max(0, Math.min(100, (cp - barMin) / barRange * 100));
  const trailLevelPct = Math.max(0, Math.min(100, (trailStopLevel - barMin) / barRange * 100));
  const activationPctBar = Math.max(0, Math.min(100, (activationPrice - barMin) / barRange * 100));

  const barColor = trailActive ? '#22c55e' : '#3b82f6';

  el.innerHTML = `
    <div class="position-card">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;">
        <div class="section-title" style="color:#22c55e;margin-bottom:0;">⚡ Open Position</div>
        <div style="display:flex;gap:8px;align-items:center;">
          ${trailActive
            ? '<span style="background:#14532d;color:#22c55e;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;">🎯 TRAILING ACTIVE</span>'
            : `<span style="background:#1e3a5f;color:#60a5fa;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;">⏳ WAITING FOR +${activationPct}%</span>`
          }
        </div>
      </div>

      <div class="pos-grid">
        <div class="pos-stat">
          <div class="pos-stat-label">Entry Price</div>
          <div class="pos-stat-value">$${parseFloat(entry).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:4})}</div>
        </div>
        <div class="pos-stat">
          <div class="pos-stat-label">Current Price</div>
          <div class="pos-stat-value">$${parseFloat(cp).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:4})}</div>
        </div>
        <div class="pos-stat">
          <div class="pos-stat-label">Unrealised PnL</div>
          <div class="pos-stat-value ${pnlCls}">${pnlPct>=0?'+':''}$${Math.abs(pnlUsdc).toFixed(2)} (${pnlPct.toFixed(2)}%)</div>
        </div>
        <div class="pos-stat">
          <div class="pos-stat-label">Quantity</div>
          <div class="pos-stat-value">${parseFloat(pos.quantity).toFixed(5)} ETH</div>
        </div>
      </div>

      <div class="pos-grid-bottom">
        <div class="pos-stat">
          <div class="pos-stat-label">🏔 Peak Price</div>
          <div class="pos-stat-value" style="color:#f59e0b;">$${parseFloat(peak).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:4})} (+${peakPct.toFixed(2)}%)</div>
        </div>
        <div class="pos-stat">
          <div class="pos-stat-label">${trailActive ? '🎯 Trail Stop Level' : '🎯 Trail Activates At'}</div>
          <div class="pos-stat-value" style="color:#a78bfa;">$${trailActive ? trailStopLevel.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:4}) : activationPrice.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:4})}</div>
        </div>
        <div class="pos-stat">
          <div class="pos-stat-label">🛑 Hard Stop Level</div>
          <div class="pos-stat-value" style="color:#ef4444;">$${hardStopLevel.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:4})}</div>
        </div>
        <div class="pos-stat">
          <div class="pos-stat-label">Opened</div>
          <div class="pos-stat-value" style="font-size:13px;">${pos.entry_time ? pos.entry_time.slice(0,10)+' '+pos.entry_time.slice(11,16)+' UTC' : '—'}</div>
        </div>
      </div>

      <div class="trail-bar-wrap">
        <div class="trail-bar-label">
          <span style="color:#ef4444;">🛑 $${hardStopLevel.toFixed(2)}</span>
          <span style="color:#94a3b8;">Price Range</span>
          <span style="color:#f59e0b;">🏔 $${peak.toFixed(2)}</span>
        </div>
        <div class="trail-bar-bg">
          <div class="trail-bar-fill" style="width:${currentPct}%;background:${barColor};"></div>
          ${trailActive ? `<div class="trail-marker" style="left:${trailLevelPct}%;" title="Trail stop: $${trailStopLevel.toFixed(2)}"></div>` : `<div class="trail-marker" style="left:${activationPctBar}%;background:#3b82f6;" title="Trail activates: $${activationPrice.toFixed(2)}"></div>`}
        </div>
        <div style="font-size:11px;color:#64748b;margin-top:5px;text-align:center;">
          ${trailActive
            ? `Trail stop at $${trailStopLevel.toFixed(2)} — drops ${trailPct}% below peak — locking in ${Math.max(0,((trailStopLevel-entry)/entry*100)).toFixed(2)}%`
            : `Trailing activates when price reaches $${activationPrice.toFixed(2)} (+${activationPct}%)`
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
      ? `<span style="color:#f59e0b;">+${parseFloat(t.peak_pct).toFixed(2)}%</span>`
      : '—';
    return `<tr>
      <td>${fmtDateTime(t.timestamp)}</td>
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
    const price = parseFloat(s.price);

    document.getElementById('price').textContent = price ? '$'+price.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}) : '—';
    document.getElementById('price-updated').textContent = s.price_updated || '—';

    const usdc = parseFloat(s.balance_usdc||0);
    const eth = parseFloat(s.balance_eth||0);
    const total = parseFloat(s.total_value_usdc||0);
    document.getElementById('total-value').textContent = '$'+total.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
    document.getElementById('balance-sub').textContent = '$'+usdc.toFixed(2)+' USDC + '+eth.toFixed(5)+' ETH';
    document.getElementById('balance-usdc').textContent = '$'+usdc.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
    document.getElementById('balance-eth').textContent = eth.toFixed(5)+' ETH';
    document.getElementById('eth-value-usdc').textContent = '≈ $'+(eth*price).toFixed(2);

    const pnl = parseFloat(s.daily_pnl||0);
    const pnlEl = document.getElementById('daily-pnl');
    pnlEl.textContent = (pnl>=0?'+':'')+' $'+Math.abs(pnl).toFixed(2);
    pnlEl.className = 'card-value '+(pnl>0?'positive':pnl<0?'negative':'');
    document.getElementById('daily-trades').textContent = (s.daily_trades||0)+' trades today';

    const lossUsed = Math.abs(Math.min(0,pnl));
    const limitEl = document.getElementById('limit-used');
    limitEl.textContent = '-$'+lossUsed.toFixed(2);
    limitEl.className = 'card-value '+(lossUsed>0?'negative':'');
    document.getElementById('limit-pct').textContent = (lossUsed/(s.daily_loss_limit||80)*100).toFixed(0)+'% of daily limit';

    document.getElementById('bot-status').innerHTML = s.bot_running
      ? '<span class="positive">Running ✓</span>'
      : '<span class="negative">Offline</span>';
    document.getElementById('last-error').textContent = s.last_error || '';

    updateSentiment(s.sentiment);
    updatePosition(s.position, price, s);
    updateTrades(s.trades||[]);
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
