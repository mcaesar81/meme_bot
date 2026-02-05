from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from meme_bot.config import Config
from meme_bot.models import RuntimeState
from meme_bot.state_machine import StateMachine
from meme_bot.state_store import RuntimeStateStore

app = FastAPI(title="meme_bot dashboard", version="1.0.0")


class ControlRequest(BaseModel):
    action: Literal["RUN", "PAUSE", "SAFE"]
    reason: Optional[str] = "manual_control"


def _load_ctx() -> tuple[Config, RuntimeStateStore, RuntimeState]:
    cfg = Config.load("config.yaml")
    store = RuntimeStateStore(cfg.get("state_store", "runtime_state_path"))
    state = store.load()
    return cfg, store, state


@app.get("/api/status")
def get_status() -> dict:
    _, _, state = _load_ctx()
    return {
        "mode": state.mode,
        "pause_until_iso": state.pause_until_iso,
        "pause_reason": state.pause_reason,
        "safe_reason": state.safe_reason,
        "trade_count_today": state.trades_today,
        "daily_pnl_usd": state.daily_pnl_usd,
        "last_trade_ts_iso": state.last_trade_ts_iso,
        "last_price_ts_iso": state.last_price_ts_iso,
    }


@app.post("/api/control")
def post_control(payload: ControlRequest) -> dict:
    cfg, store, state = _load_ctx()
    machine = StateMachine(state)
    reason = payload.reason or "manual_control"

    if payload.action == "RUN":
        machine.set_run()
    elif payload.action == "PAUSE":
        pause_sec = int(cfg.get("risk", "pause_duration_sec", default=120))
        machine.set_pause(datetime.utcnow() + timedelta(seconds=pause_sec), reason)
    elif payload.action == "SAFE":
        machine.set_safe(reason)
    else:
        raise HTTPException(status_code=400, detail="invalid_action")

    store.save(state)
    return {"ok": True, "mode": state.mode, "reason": reason}


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return """
<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>meme_bot dashboard</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; }
    .status { font-size: 20px; margin-bottom: 14px; }
    .RUN { color: #15803d; }
    .PAUSE { color: #ca8a04; }
    .SAFE { color: #b91c1c; }
    button { margin-right: 10px; padding: 8px 12px; }
    pre { background:#f4f4f5; padding:12px; }
  </style>
</head>
<body>
  <h1>meme_bot dashboard</h1>
  <div id=\"mode\" class=\"status\">Loading...</div>
  <div>
    <button onclick=\"sendControl('RUN')\">Run</button>
    <button onclick=\"sendControl('PAUSE')\">Pause</button>
    <button onclick=\"sendControl('SAFE')\">Safe</button>
  </div>
  <h3>Runtime</h3>
  <pre id=\"json\"></pre>

<script>
async function refresh() {
  const r = await fetch('/api/status');
  const j = await r.json();
  const mode = document.getElementById('mode');
  mode.className = 'status ' + j.mode;
  mode.textContent = `Mode: ${j.mode} | pause_reason=${j.pause_reason ?? '-'} | trades=${j.trade_count_today} | pnl=${j.daily_pnl_usd}`;
  document.getElementById('json').textContent = JSON.stringify(j, null, 2);
}
async function sendControl(action) {
  await fetch('/api/control', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action, reason: 'dashboard_manual'})
  });
  await refresh();
}
setInterval(refresh, 2000);
refresh();
</script>
</body>
</html>
"""
