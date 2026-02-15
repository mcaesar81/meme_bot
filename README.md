# meme_bot

Config-first Python skeleton for a crypto meme-coin trading bot (Windows + Raspberry Pi compatible), now with:
- **Primary web dashboard** (FastAPI) in `ui_web/` that now also manages bot runtime
- **Windows system tray controller** in `ui_tray/` remains optional and talks to the web API

## Features
- `config.yaml` knobs for risk limits, trade sizing, slippage, cooldowns, pause durations, and filters.
- Crash-safe `runtime_state.json` with bot state machine: `RUN` / `PAUSE` / `SAFE`.
- JSONL logging:
  - `events.log` for bot events.
  - `trades.jsonl` for completed trade actions.
- Modular folders: `datafeed/`, `execution/`, `strategy/`, `risk/`, `logger/`, `state_store/`.
- Two-wallet model in config:
  - `vault_wallet_address` (storage only, never trades).
  - `trade_wallet_address` + daily allowance for controlled execution.
- Fail-safes:
  - Stale market data -> `PAUSE`.
  - Transaction timeout -> `SAFE`.
  - Overfunded trade wallet allowance -> `PAUSE`.
- Strategy: momentum scalp
  - test entry (`$2`), optional scale-in up to `$5`.
  - exits: stop loss (`-$1`), stall (`75s`), max hold (`8m`), quick profit (`+$0.50` partial + tighter stall).
- Shared file locking for config/state/log paths to reduce cross-process corruption.

## New UI architecture
- **Web UI (primary):**
  - `GET /api/status`
  - `POST /api/control` with `{action, reason}`
- **Tray UI (secondary):** no duplicated dashboard UI; only:
  - run/pause/safe actions
  - status icon + tooltip
  - open dashboard
  - exit tray
- Tray communicates only via:
  - `GET http://127.0.0.1:8000/api/status`
  - `POST http://127.0.0.1:8000/api/control`

## Folder structure

```text
meme_bot/
  main.py
  run_bot.py
  run_web_ui.py
  run_tray.py
  config.yaml
  requirements.txt
  README.md
  meme_bot/
    app.py
    config.py
    models.py
    state_machine.py
    utils/
      file_lock.py
    datafeed/
    execution/
    strategy/
    risk/
    logger/
    state_store/
  ui_web/
    app.py
  ui_tray/
    tray_app.py
```

## Setup

1. Create venv:
   - Windows (PowerShell):
     ```powershell
     py -m venv .venv
     .\.venv\Scripts\Activate.ps1
     ```
   - Raspberry Pi / Linux:
     ```bash
     python3 -m venv .venv
     source .venv/bin/activate
     ```
2. Install deps (includes FastAPI + WebSocket support via `uvicorn[standard]` and `websockets`):
   ```bash
   pip install -r requirements.txt
   ```
   This avoids runtime warnings like `Unsupported upgrade request` / `No supported WebSocket library detected`.
3. Edit `config.yaml` placeholders.
   - For Jupiter auth, keep `providers.jupiter.api_key_env: JUP_API_KEY` as the recommended path; `providers.jupiter.api_key` is available only as a quick local fallback.
4. Create a local `.env` (optional, recommended for secrets):
   ```env
   JUP_API_KEY=your_jupiter_api_key_here
   ```
   The app will auto-load `.env` at startup if the file exists. `.env` is gitignored and keys are never logged.

## Run (single command)

```bash
python run_app.py
```

Then open: `http://127.0.0.1:8000/`

### Optional components
- Legacy bot-only runner: `python run_bot.py`
- Legacy web-only runner: `python run_web_ui.py`
- Optional tray controller (Windows): `python run_tray.py`

## Notes
- `RealExecutor` is intentionally a stub (no live trading implementation).
- Data providers are HTTP stubs with placeholder URLs + parse blocks.
- Jupiter token metadata currently uses Tokens API v1 path (`/tokens/v1/token/{mint}`), which is deprecated; a future update should migrate to Tokens API v2 once the replacement endpoint is confirmed.
- Tray requires Windows with system tray support (`pystray`, `pillow`).
- Treat this repository as the source of truth for current behavior and configuration defaults.
