"""
Dashboard
─────────
A lightweight Flask web dashboard that shows:
  - Live ETH price + bot status
  - Open position details
  - Trade history
  - Daily PnL
  - CoinGecko sentiment

Run alongside the bot:
  python -m app.dashboard

Then open: http://localhost:5000
"""

import json
import os
from datetime import datetime, date
from threading import Thread

from flask import Flask, jsonify, render_template_string
from loguru import logger

from app.config import settings
from app.exchange.binance_client import BinanceClient
from app.services.coingecko import CoinGeckoSentiment

app = Flask(__name__)

# ── Shared state (updated by background thread) ───────────────────────────────
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
}

_client = None
_coingecko = CoinGeckoSentiment()


def _refresh_loop():
    global _client
    try:
        _client = BinanceClient()
        _state["bot_running"] = True
    except Exception as e:
        _state["last_error"] = str(e)
        return

    while True:
        import time
        try:
            _state["price"] = _client.get_price(settings.symbol)
            _state["price_updated"] = datetime.utcnow().strftime("%H:%M:%S UTC")
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
    """Called by trader.py to log trades into the dashboard."""
    from flask import request
    trade = request.json
    trade["timestamp"] = datetime.utcnow().isoformat()
    _state["trades"].insert(0, trade)
    _state["trades"] = _state["trades"][:50]  # keep last 50
    if trade["side"] == "SELL":
        _state["daily_pnl"] = round(_state["daily_pnl"] + trade.get("pnl_usdc", 0), 4)
        _state["daily_trades"] += 1
    return jsonify({"ok": True})


# ── HTML Dashboard ─────────────────────────────────────────────────────────────

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
  h1 { font-size: 20px; font-weight: 600; color: #f8fafc; margin-bottom: 24px; display: flex; align-items: center; gap: 10px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #22c55e; animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
  .grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 20px; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }
  .card { background: #1e2130; border: 1px solid #2d3148; border-radius: 12px; padding: 16px 20px; }
  .card-label { font-size: 12px; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
  .card-value { font-size: 24px; font-weight: 700; color: #f8fafc; }
  .card-sub { font-size: 12px; color: #64748b; margin-top: 4px; }
  .positive { color: #22c55e; }
  .negative { color: #ef4444; }
  .neutral { color: #94a3b8; }
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
  .chart-wrap { position: relative; height: 220px; }
  .section-title { font-size: 13px; font-weight: 600; color: #94a3b8; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.05em; }
  .position-card { background: #0f2a1a; border: 1px solid #166534; border-radius: 12px; padding: 16px 20px; margin-bottom: 20px; }
  .no-position { background: #1e2130; border: 1px dashed #2d3148; border-radius: 12px; padding: 16px 20px; margin-bottom: 20px; text-align: center; color: #475569; font-size: 13px; }
  .updated { font-size: 11px; color: #475569; margin-top: 2px; }
</style>
</head>
<body>

<h1><div class="dot"></div> ETH Bot Dashboard</h1>

<div class="grid-4">
  <div class="card">
    <div class="card-label">ETH Price</div>
    <div class="card-value" id="price">—</div>
    <div class="updated" id="price-updated">—</div>
  </div>
  <div class="card">
    <div class="card-label">Daily PnL</div>
    <div class="card-value" id="daily-pnl">$0.00</div>
    <div class="card-sub" id="daily-trades">0 trades today</div>
  </div>
  <div class="card">
    <div class="card-label">Trades Total</div>
    <div class="card-value" id="total-trades">0</div>
    <div class="card-sub">all time</div>
  </div>
  <div class="card">
    <div class="card-label">Bot Status</div>
    <div class="card-value" id="bot-status" style="font-size:16px; margin-top:4px;">—</div>
    <div class="card-sub" id="last-error" style="color:#ef4444;"></div>
  </div>
</div>

<div id="position-section"></div>

<div class="grid-2">
  <div class="card">
    <div class="section-title">CoinGecko Sentiment</div>
    <div id="sentiment-content">
      <div style="color:#475569; font-size:13px;">Loading...</div>
    </div>
  </div>
  <div class="card">
    <div class="section-title">Daily PnL Chart</div>
    <div class="chart-wrap"><canvas id="pnlChart"></canvas></div>
  </div>
</div>

<div class="card">
  <div class="section-title">Trade History</div>
  <table>
    <thead><tr>
      <th>Time</th><th>Side</th><th>Price</th><th>Qty</th><th>PnL</th><th>Reason</th>
    </tr></thead>
    <tbody id="trade-tbody"><tr><td colspan="6" style="color:#475569; text-align:center;">No trades yet</td></tr></tbody>
  </table>
</div>

<script>
let pnlChart;
let pnlData = { labels: [], values: [] };

function initChart() {
  const ctx = document.getElementById('pnlChart').getContext('2d');
  pnlChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: pnlData.labels,
      datasets: [{
        label: 'PnL (USDC)',
        data: pnlData.values,
        borderColor: '#22c55e',
        backgroundColor: 'rgba(34,197,94,0.08)',
        borderWidth: 2,
        pointRadius: 4,
        pointBackgroundColor: '#22c55e',
        fill: true,
        tension: 0.3,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: '#475569', font: { size: 11 } }, grid: { color: '#1a1f35' } },
        y: { ticks: { color: '#475569', font: { size: 11 }, callback: v => '$' + v.toFixed(2) }, grid: { color: '#1a1f35' } }
      }
    }
  });
}

function fmtTime(iso) {
  if (!iso) return '—';
  return iso.slice(11, 16) + ' UTC';
}

function fmtPnl(v) {
  if (v == null) return '—';
  const cls = v >= 0 ? 'positive' : 'negative';
  return `<span class="${cls}">${v >= 0 ? '+' : ''}$${parseFloat(v).toFixed(2)}</span>`;
}

function updateSentiment(s) {
  if (!s) { document.getElementById('sentiment-content').innerHTML = '<div style="color:#475569;font-size:13px;">Unavailable</div>'; return; }
  const badgeClass = `badge-${s.label}`;
  const fillColor = s.label === 'bullish' ? '#22c55e' : s.label === 'bearish' ? '#ef4444' : '#64748b';
  document.getElementById('sentiment-content').innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;">
      <span class="badge ${badgeClass}">${s.label}</span>
      <span style="font-size:13px;color:#94a3b8;">${s.strength}/100 strength</span>
    </div>
    <div class="sentiment-bar"><div class="sentiment-fill" style="width:${s.strength}%;background:${fillColor};"></div></div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:14px;">
      <div style="text-align:center;">
        <div style="font-size:11px;color:#475569;margin-bottom:2px;">1H</div>
        <div style="font-size:15px;font-weight:600;" class="${s.change_1h >= 0 ? 'positive' : 'negative'}">${s.change_1h >= 0 ? '+' : ''}${parseFloat(s.change_1h).toFixed(2)}%</div>
      </div>
      <div style="text-align:center;">
        <div style="font-size:11px;color:#475569;margin-bottom:2px;">24H</div>
        <div style="font-size:15px;font-weight:600;" class="${s.change_24h >= 0 ? 'positive' : 'negative'}">${s.change_24h >= 0 ? '+' : ''}${parseFloat(s.change_24h).toFixed(2)}%</div>
      </div>
      <div style="text-align:center;">
        <div style="font-size:11px;color:#475569;margin-bottom:2px;">7D</div>
        <div style="font-size:15px;font-weight:600;" class="${s.change_7d >= 0 ? 'positive' : 'negative'}">${s.change_7d >= 0 ? '+' : ''}${parseFloat(s.change_7d).toFixed(2)}%</div>
      </div>
    </div>
  `;
}

function updatePosition(pos) {
  const el = document.getElementById('position-section');
  if (!pos) {
    el.innerHTML = '<div class="no-position">No open position — bot is watching for signals</div>';
    return;
  }
  const pnl = ((pos.current_price - pos.entry_price) / pos.entry_price * 100).toFixed(2);
  const pnlUsdc = ((pos.current_price - pos.entry_price) * pos.quantity).toFixed(2);
  const cls = pnl >= 0 ? 'positive' : 'negative';
  el.innerHTML = `
    <div class="position-card" style="margin-bottom:20px;">
      <div class="section-title" style="color:#22c55e;">Open Position</div>
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-top:8px;">
        <div><div style="font-size:11px;color:#64748b;margin-bottom:3px;">Entry Price</div><div style="font-weight:600;">$${parseFloat(pos.entry_price).toFixed(4)}</div></div>
        <div><div style="font-size:11px;color:#64748b;margin-bottom:3px;">Quantity</div><div style="font-weight:600;">${parseFloat(pos.quantity).toFixed(5)} ETH</div></div>
        <div><div style="font-size:11px;color:#64748b;margin-bottom:3px;">Unrealised PnL</div><div style="font-weight:600;" class="${cls}">${pnl >= 0 ? '+' : ''}${pnlUsdc} USDC (${pnl}%)</div></div>
        <div><div style="font-size:11px;color:#64748b;margin-bottom:3px;">Opened</div><div style="font-weight:600;">${fmtTime(pos.entry_time)}</div></div>
      </div>
    </div>
  `;
}

function updateTrades(trades) {
  const tbody = document.getElementById('trade-tbody');
  if (!trades.length) return;
  tbody.innerHTML = trades.map(t => `
    <tr>
      <td>${fmtTime(t.timestamp)}</td>
      <td class="side-${t.side.toLowerCase()}">${t.side}</td>
      <td>$${parseFloat(t.price).toFixed(4)}</td>
      <td>${parseFloat(t.quantity).toFixed(5)}</td>
      <td>${t.pnl_usdc != null ? fmtPnl(t.pnl_usdc) : '—'}</td>
      <td style="color:#64748b;">${t.reason || '—'}</td>
    </tr>
  `).join('');

  // Update PnL chart from sell trades
  const sells = trades.filter(t => t.side === 'SELL').reverse();
  pnlData.labels = sells.map(t => fmtTime(t.timestamp));
  let running = 0;
  pnlData.values = sells.map(t => { running += (t.pnl_usdc || 0); return parseFloat(running.toFixed(2)); });
  if (pnlChart) { pnlChart.data.labels = pnlData.labels; pnlChart.data.datasets[0].data = pnlData.values; pnlChart.update(); }
}

async function refresh() {
  try {
    const res = await fetch('/api/state');
    const s = await res.json();

    const price = parseFloat(s.price);
    document.getElementById('price').textContent = price ? '$' + price.toFixed(2) : '—';
    document.getElementById('price-updated').textContent = s.price_updated || '—';

    const pnl = parseFloat(s.daily_pnl || 0);
    const pnlEl = document.getElementById('daily-pnl');
    pnlEl.textContent = (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2);
    pnlEl.className = 'card-value ' + (pnl > 0 ? 'positive' : pnl < 0 ? 'negative' : '');

    document.getElementById('daily-trades').textContent = (s.daily_trades || 0) + ' trades today';
    document.getElementById('total-trades').textContent = (s.trades || []).length;
    document.getElementById('bot-status').innerHTML = s.bot_running
      ? '<span class="positive">Running</span>'
      : '<span class="negative">Offline</span>';
    document.getElementById('last-error').textContent = s.last_error || '';

    updateSentiment(s.sentiment);
    updatePosition(s.position);
    updateTrades(s.trades || []);
  } catch(e) { console.error(e); }
}

initChart();
refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""


def start():
    t = Thread(target=_refresh_loop, daemon=True)
    t.start()
    logger.info("Dashboard running at http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == "__main__":
    start()
