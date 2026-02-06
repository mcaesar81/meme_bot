from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from meme_bot.config import Config
from meme_bot.runner import BotRunner
from meme_bot.utils import FileLock
from ui_web.log_tail import follow_file, tail_lines, touch_file

CFG_PATH = (Path(__file__).resolve().parent.parent / "config.yaml").resolve()
runner = BotRunner(str(CFG_PATH))


@asynccontextmanager
async def lifespan(_: FastAPI):
    cfg = Config.load(CFG_PATH)
    events_path, trades_path = _resolved_log_paths(cfg)
    touch_file(events_path)
    touch_file(trades_path)
    if bool(cfg.get("auto_start_bot", default=True)):
        runner.start()
    yield
    runner.stop()


app = FastAPI(title="meme_bot dashboard", version="2.0.0", lifespan=lifespan)


class ControlRequest(BaseModel):
    action: Literal["RUN", "PAUSE", "SAFE"]
    reason: Optional[str] = "manual_control"


class ConfigPayload(BaseModel):
    config: dict[str, Any]


def _cfg() -> Config:
    return Config.load(CFG_PATH)


def _resolved_log_paths(cfg: Config) -> tuple[str, str]:
    events_path = cfg.resolve_path("logging", "events_path", default="events.log")
    trades_path = cfg.resolve_path("logging", "trades_path", default="trades.jsonl")
    return events_path, trades_path


def _write_config(raw: dict[str, Any]) -> None:
    with FileLock(str(CFG_PATH)):
        temp = CFG_PATH.with_suffix(".tmp")
        temp.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        temp.replace(CFG_PATH)


@app.get("/api/status")
def get_status() -> dict[str, Any]:
    status = runner.status()
    return status


@app.post("/api/control")
def post_control(payload: ControlRequest) -> dict[str, Any]:
    reason = payload.reason or "manual_control"
    if payload.action == "RUN":
        return runner.run(reason)
    if payload.action == "PAUSE":
        return runner.pause(reason)
    if payload.action == "SAFE":
        return runner.safe(reason)
    raise HTTPException(status_code=400, detail="invalid_action")


@app.post("/api/bot/start")
def start_bot() -> dict[str, Any]:
    started = runner.start()
    return {"ok": True, "started": started, "runner_alive": runner.is_alive}


@app.post("/api/bot/stop")
def stop_bot() -> dict[str, Any]:
    stopped = runner.stop()
    return {"ok": True, "stopped": stopped, "runner_alive": runner.is_alive}


@app.post("/api/bot/restart")
def restart_bot() -> dict[str, Any]:
    runner.restart()
    return {"ok": True, "runner_alive": runner.is_alive}


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return _cfg().raw


@app.post("/api/config")
def update_config(payload: ConfigPayload) -> dict[str, Any]:
    _write_config(payload.config)
    cfg = _cfg()
    events_path, trades_path = _resolved_log_paths(cfg)
    touch_file(events_path)
    touch_file(trades_path)
    runner.restart()
    return {"ok": True, "restarted": True}


@app.get("/api/logs/events")
def get_events_logs(tail: int = 200) -> list[dict[str, Any]]:
    cfg = _cfg()
    events_path, _ = _resolved_log_paths(cfg)
    return tail_lines(events_path, tail)


@app.get("/api/logs/trades")
def get_trades_logs(tail: int = 200) -> list[dict[str, Any]]:
    cfg = _cfg()
    _, trades_path = _resolved_log_paths(cfg)
    return tail_lines(trades_path, tail)


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        cfg = _cfg()
        events_path, _ = _resolved_log_paths(cfg)
        await follow_file(events_path, websocket)
    except WebSocketDisconnect:
        return


@app.websocket("/ws/trades")
async def ws_trades(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        cfg = _cfg()
        _, trades_path = _resolved_log_paths(cfg)
        await follow_file(trades_path, websocket)
    except WebSocketDisconnect:
        return


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return """
<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>meme_bot dashboard</title>
  <style>
    :root { color-scheme: dark; }
    body { font-family: Arial, sans-serif; margin: 16px; background:#111827; color:#e5e7eb; }
    .row { display:flex; gap:16px; flex-wrap:wrap; }
    .card { background:#1f2937; border-radius:10px; padding:12px; flex:1; min-width:360px; }
    .status { font-size: 20px; font-weight:600; }
    .RUN { color: #22c55e; }
    .PAUSE { color: #f59e0b; }
    .SAFE { color: #ef4444; }
    button { margin:4px 6px 4px 0; padding:8px 10px; border-radius:8px; border:0; cursor:pointer; }
    button:hover { opacity:0.9; }
    input, textarea { width:100%; box-sizing:border-box; border-radius:8px; border:1px solid #374151; background:#111827; color:#e5e7eb; padding:8px; }
    .tabs button { background:#374151; color:#f9fafb; }
    .active-tab { background:#4b5563 !important; }
    .log-pane { height:350px; overflow:auto; border:1px solid #374151; border-radius:8px; padding:8px; background:#030712; }
    .line { padding:3px 0; border-bottom:1px solid #111827; color:#9ca3af; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size:12px; }
    .event-safe_mode, .event-loop_error { color:#ef4444; }
    .event-pause, .event-entry_blocked { color:#f59e0b; }
    .event-entry_rejected { color:#fb923c; }
    .event-bot_start, .event-resume_run { color:#22c55e; }
    .trade-buy { color:#60a5fa; }
    .trade-sell { color:#c084fc; }
  </style>
</head>
<body>
  <h1>meme_bot single-process dashboard</h1>
  <div id=\"mode\" class=\"status\">Loading...</div>
  <div id=\"runner\"></div>

  <div class=\"row\">
    <div class=\"card\">
      <h3>Bot Controls</h3>
      <button onclick=\"botAction('/api/bot/start')\">Start</button>
      <button onclick=\"sendControl('RUN')\">Run</button>
      <button onclick=\"sendControl('PAUSE')\">Pause</button>
      <button onclick=\"sendControl('SAFE')\">Safe</button>
      <button onclick=\"botAction('/api/bot/stop')\">Stop</button>
      <button onclick=\"botAction('/api/bot/restart')\">Restart</button>
      <pre id=\"statusJson\"></pre>
    </div>

    <div class=\"card\">
      <h3>Config Editor</h3>
      <label>mode</label><input id=\"cfg_mode\" />
      <label>loop_interval_sec</label><input id=\"cfg_loop\" type=\"number\" step=\"0.5\" />
      <label>risk.pause_duration_sec</label><input id=\"cfg_pause\" type=\"number\" />
      <label>risk.max_trades_per_day</label><input id=\"cfg_maxtrades\" type=\"number\" />
      <label>strategy.test_entry_usd</label><input id=\"cfg_entry\" type=\"number\" step=\"0.1\" />
      <label>strategy.max_position_usd</label><input id=\"cfg_maxpos\" type=\"number\" step=\"0.1\" />
      <label>providers.indexer.top_n</label><input id=\"cfg_topn\" type=\"number\" />
      <label>exclude_symbols (comma separated)</label><input id=\"cfg_excludes\" />
      <button onclick=\"saveConfig()\">Save Config + Restart Bot</button>
      <details><summary>Raw config JSON</summary><textarea id=\"cfg_raw\" rows=\"10\"></textarea></details>
    </div>
  </div>

  <div class=\"card\" style=\"margin-top:16px;\">
    <h3>Live Logs</h3>
    <div>
      <input id=\"search\" placeholder=\"Search logs\" oninput=\"renderLogs()\" />
      <label><input id=\"pauseScroll\" type=\"checkbox\" /> Pause Auto-scroll</label>
    </div>
    <div class=\"tabs\" style=\"margin:8px 0;\">
      <button id=\"tab_events\" class=\"active-tab\" onclick=\"setTab('events')\">Events</button>
      <button id=\"tab_trades\" onclick=\"setTab('trades')\">Trades</button>
    </div>
    <div id=\"logPane\" class=\"log-pane\"></div>
  </div>

<script>
let statusData = {};
let configData = {};
let activeTab = 'events';
const logs = { events: [], trades: [] };

function eventClass(item) {
  const t = item.event_type || 'default';
  return `event-${t}`;
}
function tradeClass(item) {
  const side = String(item.side || '').toLowerCase();
  return side === 'buy' ? 'trade-buy' : (side === 'sell' ? 'trade-sell' : '');
}
function logText(item) {
  return JSON.stringify(item);
}

async function refreshStatus() {
  const r = await fetch('/api/status');
  statusData = await r.json();
  const mode = document.getElementById('mode');
  mode.className = 'status ' + statusData.mode;
  mode.textContent = `Mode: ${statusData.mode}`;
  document.getElementById('runner').textContent = `Runner alive: ${statusData.runner_alive}`;
  document.getElementById('statusJson').textContent = JSON.stringify(statusData, null, 2);
}

async function sendControl(action) {
  await fetch('/api/control', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action, reason: 'dashboard_manual'})
  });
  await refreshStatus();
}

async function botAction(path) {
  await fetch(path, { method:'POST' });
  await refreshStatus();
}

function setInput(id, val) {
  document.getElementById(id).value = (val ?? '');
}

async function loadConfig() {
  const r = await fetch('/api/config');
  configData = await r.json();
  setInput('cfg_mode', configData.mode);
  setInput('cfg_loop', configData.loop_interval_sec);
  setInput('cfg_pause', configData.risk?.pause_duration_sec);
  setInput('cfg_maxtrades', configData.risk?.max_trades_per_day);
  setInput('cfg_entry', configData.strategy?.test_entry_usd);
  setInput('cfg_maxpos', configData.strategy?.max_position_usd);
  setInput('cfg_topn', configData.providers?.indexer?.top_n);
  setInput('cfg_excludes', (configData.risk?.exclude_symbols || []).join(', '));
  document.getElementById('cfg_raw').value = JSON.stringify(configData, null, 2);
}

function buildConfigFromEditor() {
  const cfg = structuredClone(configData || {});
  cfg.mode = document.getElementById('cfg_mode').value || 'paper';
  cfg.loop_interval_sec = Number(document.getElementById('cfg_loop').value || 3);
  cfg.risk = cfg.risk || {};
  cfg.strategy = cfg.strategy || {};
  cfg.providers = cfg.providers || {};
  cfg.providers.indexer = cfg.providers.indexer || {};
  cfg.risk.pause_duration_sec = Number(document.getElementById('cfg_pause').value || 120);
  cfg.risk.max_trades_per_day = Number(document.getElementById('cfg_maxtrades').value || 20);
  cfg.strategy.test_entry_usd = Number(document.getElementById('cfg_entry').value || 2);
  cfg.strategy.max_position_usd = Number(document.getElementById('cfg_maxpos').value || 5);
  cfg.providers.indexer.top_n = Number(document.getElementById('cfg_topn').value || 25);
  cfg.risk.exclude_symbols = document.getElementById('cfg_excludes').value
    .split(',').map(s => s.trim()).filter(Boolean);

  const rawText = document.getElementById('cfg_raw').value.trim();
  if (rawText) {
    try {
      return JSON.parse(rawText);
    } catch (_) {}
  }
  return cfg;
}

async function saveConfig() {
  const cfg = buildConfigFromEditor();
  await fetch('/api/config', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({config: cfg})
  });
  configData = cfg;
  document.getElementById('cfg_raw').value = JSON.stringify(cfg, null, 2);
  await refreshStatus();
}

function setTab(tab) {
  activeTab = tab;
  document.getElementById('tab_events').classList.toggle('active-tab', tab === 'events');
  document.getElementById('tab_trades').classList.toggle('active-tab', tab === 'trades');
  renderLogs();
}

function renderLogs() {
  const pane = document.getElementById('logPane');
  const q = document.getElementById('search').value.toLowerCase();
  const items = logs[activeTab].filter(it => logText(it).toLowerCase().includes(q));
  pane.innerHTML = items.map(item => {
    const cls = activeTab === 'events' ? eventClass(item) : tradeClass(item);
    return `<div class=\"line ${cls}\">${logText(item)}</div>`;
  }).join('');
  if (!document.getElementById('pauseScroll').checked) {
    pane.scrollTop = pane.scrollHeight;
  }
}

async function loadInitialLogs() {
  logs.events = await (await fetch('/api/logs/events?tail=200')).json();
  logs.trades = await (await fetch('/api/logs/trades?tail=200')).json();
  renderLogs();
}

function connectWs() {
  const eventsWs = new WebSocket(`ws://${location.host}/ws/events`);
  eventsWs.onmessage = (msg) => { logs.events.push(JSON.parse(msg.data)); if (logs.events.length > 500) logs.events.shift(); if (activeTab === 'events') renderLogs(); };

  const tradesWs = new WebSocket(`ws://${location.host}/ws/trades`);
  tradesWs.onmessage = (msg) => { logs.trades.push(JSON.parse(msg.data)); if (logs.trades.length > 500) logs.trades.shift(); if (activeTab === 'trades') renderLogs(); };
}

setInterval(refreshStatus, 2000);
refreshStatus();
loadConfig();
loadInitialLogs();
connectWs();
</script>
</body>
</html>
"""
