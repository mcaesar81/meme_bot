from __future__ import annotations

import time
from dataclasses import asdict
from datetime import datetime, timedelta
from threading import Event

from meme_bot.config import Config
from meme_bot.datafeed import IndexerProvider, MarketDataService, QuoteProvider
from meme_bot.execution import DummyExecutor, RealExecutor
from meme_bot.logger import JsonLineLogger
from meme_bot.models import BotMode, Position
from meme_bot.risk import RiskManager
from meme_bot.state_machine import StateMachine
from meme_bot.state_store import RuntimeStateStore
from meme_bot.strategy import MomentumScalpStrategy


def run_loop(stop_event: Event, cfg_path: str = "config.yaml") -> None:
    last_mode: str | None = None

    while not stop_event.is_set():
        cfg = Config.load(cfg_path)
        logger = JsonLineLogger(cfg.get("logging", "events_path"), cfg.get("logging", "trades_path"))
        store = RuntimeStateStore(cfg.get("state_store", "runtime_state_path"))

        indexer = IndexerProvider(cfg.get("providers", "indexer"))
        quote = QuoteProvider(cfg.get("providers", "quote"))
        market = MarketDataService(indexer)
        risk = RiskManager(cfg.get("risk"))
        strategy = MomentumScalpStrategy(cfg.get("strategy"))

        mode = cfg.get("mode", default="paper")
        executor = DummyExecutor() if mode == "paper" else RealExecutor()

        state = store.load()
        if last_mode != mode:
            logger.event("bot_start", {"mode": mode, "state": asdict(state)})
            last_mode = mode

        now = datetime.utcnow()
        machine = StateMachine(state)

        try:
            if state.mode == BotMode.SAFE:
                logger.event("safe_mode", {"reason": state.safe_reason})
                store.save(state)
                stop_event.wait(float(cfg.get("loop_interval_sec", default=3)))
                continue

            if state.mode == BotMode.PAUSE and machine.is_pause_over(now):
                machine.set_run()
                logger.event("resume_run", {"at": now.isoformat()})

            candidates = market.get_candidates()
            state.last_price_ts_iso = now.isoformat()

            stale_limit = int(cfg.get("state_stale_price_sec", default=20))
            if market.last_update_ts and (now - market.last_update_ts).total_seconds() > stale_limit:
                machine.set_pause(now + timedelta(seconds=int(cfg.get("risk", "pause_duration_sec"))), "stale_price_data")
                logger.event("pause", {"reason": "stale_price_data"})

            overfund_limit = float(cfg.get("trade_wallet_overfund_pause_usd", default=150))
            daily_allowance = float(cfg.get("wallets", "trade_wallet_daily_allowance_usd"))
            if daily_allowance > overfund_limit:
                machine.set_pause(now + timedelta(seconds=int(cfg.get("risk", "pause_duration_sec"))), "trade_wallet_overfunded")
                logger.event("pause", {"reason": "trade_wallet_overfunded"})

            if state.mode != BotMode.RUN:
                store.save(state)
                stop_event.wait(float(cfg.get("loop_interval_sec", default=3)))
                continue

            filtered = risk.filter_candidates(candidates)

            if state.active_position is None:
                allowed, reason = risk.can_open_trade(state, now)
                if not allowed:
                    logger.event("entry_blocked", {"reason": reason})
                else:
                    pick = strategy.pick_entry(filtered)
                    if pick:
                        size = strategy.initial_entry_size()
                        q = quote.get_effective_price(pick.address, "buy", size)
                        if risk.slippage_ok(q, pick.price_usd):
                            result = executor.execute(pick.symbol, pick.address, "buy", size, q)
                            state.active_position = asdict(
                                Position(
                                    symbol=pick.symbol,
                                    token_address=pick.address,
                                    size_usd=result.filled_usd,
                                    entry_price_usd=result.avg_price_usd,
                                    opened_at=now,
                                    last_scale_in_at=now,
                                )
                            )
                            state.trades_today += 1
                            state.last_trade_ts_iso = now.isoformat()
                            logger.trade(result)
                        else:
                            logger.event("entry_rejected", {"reason": "slippage_limit", "symbol": pick.symbol})
            else:
                raw = dict(state.active_position or {})
                for key in ("opened_at", "last_scale_in_at"):
                    if isinstance(raw.get(key), str):
                        raw[key] = datetime.fromisoformat(raw[key])
                pos = Position(**raw)
                px = quote.get_effective_price(pos.token_address, "sell", pos.size_usd)
                unrealized = (px - pos.entry_price_usd) * (pos.size_usd / max(pos.entry_price_usd, 1e-9))

                partial = strategy.quick_profit_partial_size(pos, unrealized)
                if partial > 0:
                    result = executor.execute(pos.symbol, pos.token_address, "sell", partial, px)
                    pos.size_usd -= result.filled_usd
                    pos.quick_profit_taken = True
                    logger.trade(result)

                scale = strategy.maybe_scale_in_size(pos, now, unrealized)
                if scale > 0:
                    buy_px = quote.get_effective_price(pos.token_address, "buy", scale)
                    if risk.slippage_ok(buy_px, px):
                        result = executor.execute(pos.symbol, pos.token_address, "buy", scale, buy_px)
                        pos.size_usd += result.filled_usd
                        pos.last_scale_in_at = now
                        logger.trade(result)

                exit_now, reason = strategy.should_exit(pos, now, unrealized)
                if exit_now or pos.size_usd <= 0:
                    result = executor.execute(pos.symbol, pos.token_address, "sell", pos.size_usd, px)
                    pnl = result.filled_usd - pos.size_usd
                    state.daily_pnl_usd += pnl
                    state.hourly_pnl_usd += pnl
                    state.active_position = None
                    state.last_trade_ts_iso = now.isoformat()
                    logger.trade(result)
                    logger.event("position_closed", {"reason": reason, "pnl_usd": pnl})
                else:
                    state.active_position = asdict(pos)

            store.save(state)
            stop_event.wait(float(cfg.get("loop_interval_sec", default=3)))

        except TimeoutError:
            machine.set_safe("tx_timeout")
            logger.event("safe_mode", {"reason": "tx_timeout"})
            store.save(state)
            stop_event.wait(float(cfg.get("loop_interval_sec", default=3)))

        except Exception as exc:  # noqa: BLE001
            logger.event("loop_error", {"error": str(exc)})
            store.save(state)
            stop_event.wait(float(cfg.get("loop_interval_sec", default=3)))


def run() -> None:
    stop_event = Event()
    try:
        run_loop(stop_event)
    except KeyboardInterrupt:
        stop_event.set()
        time.sleep(0.05)
