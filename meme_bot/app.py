from __future__ import annotations

import time
import traceback
from dataclasses import asdict
from datetime import datetime, timedelta
from threading import Event

from meme_bot.config import Config
from meme_bot.datafeed import IndexerProvider, MarketDataService, QuoteProvider
from meme_bot.execution import DummyExecutor, RealExecutor
from meme_bot.logger import JsonLineLogger
from meme_bot.models import BotMode, CandidateToken, Position, RuntimeState
from meme_bot.risk import RiskManager
from meme_bot.state_machine import StateMachine
from meme_bot.state_store import RuntimeStateStore
from meme_bot.strategy import MomentumScalpStrategy
from meme_bot.utils.timefmt import LOCAL_TZ, iso_utc, now_utc, parse_iso_datetime

EPSILON_FLAT = 0.005


def _token_meta(candidate: CandidateToken) -> dict:
    return {
        "token_address": candidate.address,
        "token_symbol": candidate.symbol,
        "token_name": candidate.name,
        "pair_address": candidate.pair_address,
        "dex_id": candidate.dex_id,
        "chain_id": candidate.chain_id,
        "liquidity_usd": candidate.liquidity_usd,
        "volume_m5_usd": candidate.volume_5m_usd,
    }


def _roll_pnl_buckets(state: RuntimeState, now: datetime) -> None:
    local_now = now.astimezone(LOCAL_TZ)
    day_bucket = local_now.strftime("%Y-%m-%d")
    hour_bucket = local_now.strftime("%Y-%m-%d %H")

    if state.pnl_day_bucket_local != day_bucket:
        state.pnl_day_bucket_local = day_bucket
        state.pnl_day_realized_usd = 0.0
    if state.pnl_hour_bucket_local != hour_bucket:
        state.pnl_hour_bucket_local = hour_bucket
        state.pnl_hour_realized_usd = 0.0


def _result_label(pnl_usd: float) -> str:
    if pnl_usd > EPSILON_FLAT:
        return "WIN"
    if pnl_usd < -EPSILON_FLAT:
        return "LOSS"
    return "FLAT"


def _apply_realized_pnl(state: RuntimeState, pnl_usd: float, now: datetime, logger: JsonLineLogger, reason: str, token_meta: dict) -> None:
    _roll_pnl_buckets(state, now)
    state.pnl_day_realized_usd += pnl_usd
    state.pnl_hour_realized_usd += pnl_usd
    state.pnl_total_realized_usd += pnl_usd

    # Backward compatibility fields
    state.daily_pnl_usd = state.pnl_day_realized_usd
    state.hourly_pnl_usd = state.pnl_hour_realized_usd

    logger.event(
        "pnl_updated",
        {
            "reason": reason,
            "pnl_delta_usd": pnl_usd,
            "pnl_day_realized_usd": state.pnl_day_realized_usd,
            "pnl_hour_realized_usd": state.pnl_hour_realized_usd,
            "pnl_total_realized_usd": state.pnl_total_realized_usd,
            "pnl_day_bucket_local": state.pnl_day_bucket_local,
            "pnl_hour_bucket_local": state.pnl_hour_bucket_local,
            **token_meta,
        },
    )


def _load_position(raw: dict) -> Position:
    parsed = dict(raw)
    for key in ("opened_at", "last_scale_in_at"):
        dt = parse_iso_datetime(parsed.get(key))
        if dt:
            parsed[key] = dt
    return Position(**parsed)


def run_loop(stop_event: Event, cfg_path: str = "config.yaml") -> None:
    last_mode: str | None = None
    loop_count = 0
    repeat_top_count = 0
    last_top_token: str | None = None
    token_last_trade_iso: dict[str, str] = {}
    logged_timezone = False

    while not stop_event.is_set():
        cfg = Config.load(cfg_path)
        logger = JsonLineLogger(
            cfg.resolve_path("logging", "events_path", default="events.log"),
            cfg.resolve_path("logging", "trades_path", default="trades.jsonl"),
        )
        store = RuntimeStateStore(cfg.resolve_path("state_store", "runtime_state_path", default="runtime_state.json"))

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

        if not logged_timezone:
            logger.event("time_config", {"local_tz": str(LOCAL_TZ)})
            logged_timezone = True

        now = now_utc()
        machine = StateMachine(state)
        loop_count += 1

        try:
            _roll_pnl_buckets(state, now)

            tick_every = max(1, int(cfg.get("logging", "tick_every_loops", default=1)))
            if loop_count % tick_every == 0:
                logger.event("tick", {"mode": state.mode, "loop": loop_count})

            if state.mode == BotMode.SAFE:
                logger.event("safe_mode", {"reason": state.safe_reason})
                store.save(state)
                stop_event.wait(float(cfg.get("loop_interval_sec", default=3)))
                continue

            if state.mode == BotMode.PAUSE and machine.is_pause_over(now):
                machine.set_run()
                logger.event("resume_run", {"at": now.isoformat()})

            candidates = market.get_candidates()
            state.last_price_ts_iso = iso_utc(now)

            min_unique = int(cfg.get("providers", "indexer", "min_unique_tokens", default=3))
            unique_addresses = {c.address for c in candidates if c.address}
            if len(unique_addresses) < min_unique:
                logger.event("universe_too_narrow", {"unique_tokens": len(unique_addresses), "required": min_unique})

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
            probe = strategy.pick_entry(candidates)
            probe_reason = "no_candidates" if probe is None else risk.evaluate_candidate(probe)[1]
            best = strategy.pick_entry(filtered)
            best_reason = "ok"

            if best:
                if last_top_token == best.address:
                    repeat_top_count += 1
                else:
                    last_top_token = best.address
                    repeat_top_count = 1

                top_repeat_limit = int(cfg.get("providers", "indexer", "top_repeat_limit", default=8))
                if repeat_top_count >= top_repeat_limit:
                    logger.event("stuck_candidate", {"repeat_count": repeat_top_count, **_token_meta(best)})

            if state.active_position is None:
                allowed, reason = risk.can_open_trade(state, now)
                if not allowed:
                    if reason == "time_parse_warn":
                        logger.event("time_parse_warn", {"field": "last_trade_ts_iso", "value": state.last_trade_ts_iso})
                    best_reason = reason if reason != "time_parse_warn" else "ok"
                elif not filtered:
                    best_reason = probe_reason
                elif not best:
                    best_reason = "signal_false"
                else:
                    cooldown_min = int(cfg.get("risk", "same_token_cooldown_min", default=10))
                    last_trade_for_token = token_last_trade_iso.get(best.address)
                    if last_trade_for_token:
                        last_trade_dt = parse_iso_datetime(last_trade_for_token)
                        if last_trade_dt and now - last_trade_dt < timedelta(minutes=cooldown_min):
                            best_reason = "same_token_cooldown"

                if best_reason == "ok" and best:
                    size = strategy.initial_entry_size()
                    reference_price = quote.get_effective_price(best.address, "buy", size)
                    if reference_price <= 0:
                        best_reason = "price_unavailable"
                    elif risk.slippage_ok(reference_price, best.price_usd):
                        result = executor.execute(best.symbol, best.address, "buy", size, reference_price)
                        result.metadata.update(_token_meta(best))
                        result.metadata["reference_price_usd"] = best.price_usd
                        state.active_position = asdict(
                            Position(
                                symbol=best.symbol,
                                token_name=best.name,
                                token_address=best.address,
                                pair_address=best.pair_address,
                                dex_id=best.dex_id,
                                chain_id=best.chain_id,
                                size_usd=result.filled_usd,
                                entry_price_usd=result.avg_price_usd,
                                opened_at=now,
                                last_scale_in_at=now,
                            )
                        )
                        state.trades_today += 1
                        state.last_trade_ts_iso = iso_utc(now)
                        token_last_trade_iso[best.address] = iso_utc(now)
                        logger.trade(result)
                    else:
                        best_reason = "slippage_too_high"

                if best_reason != "ok":
                    payload = {
                        "reason": best_reason,
                        "candidate_symbol": best.symbol if best else None,
                        "candidate_token_address": best.address if best else None,
                        "candidate_liquidity_usd": best.liquidity_usd if best else None,
                        "candidate_vol_5m_usd": best.volume_5m_usd if best else None,
                        "effective_price": quote.get_effective_price(best.address, "buy", 1.0) if best else None,
                        "candidates": len(candidates),
                        "passed_filters": len(filtered),
                    }
                    if best:
                        payload.update(_token_meta(best))
                    logger.event("no_entry", payload)

                logger.event(
                    "decision_summary",
                    {
                        "candidates": len(candidates),
                        "passed_filters": len(filtered),
                        "best": best.address if best else None,
                        "blocked_by": None if best_reason == "ok" else best_reason,
                    },
                )
            else:
                raw = dict(state.active_position or {})
                pos = _load_position(raw)
                px = quote.get_effective_price(pos.token_address, "sell", pos.size_usd)
                unrealized = (px - pos.entry_price_usd) * (pos.size_usd / max(pos.entry_price_usd, 1e-9))
                state.open_unrealized_usd = unrealized

                partial = strategy.quick_profit_partial_size(pos, unrealized)
                if partial > 0:
                    result = executor.execute(pos.symbol, pos.token_address, "sell", partial, px)
                    leg_qty = partial / max(pos.entry_price_usd, 1e-9)
                    leg_exit_value = leg_qty * px
                    leg_pnl = leg_exit_value - partial
                    result.metadata.update(
                        {
                            "token_address": pos.token_address,
                            "token_symbol": pos.symbol,
                            "token_name": pos.token_name,
                            "pair_address": pos.pair_address,
                            "dex_id": pos.dex_id,
                            "chain_id": pos.chain_id,
                            "entry_price": pos.entry_price_usd,
                            "exit_price": px,
                            "qty": leg_qty,
                            "entry_value_usd": partial,
                            "exit_value_usd": leg_exit_value,
                            "realized_pnl_usd": leg_pnl,
                            "result": _result_label(leg_pnl),
                            "exit_reason": "quick_profit_partial",
                            "fees_est_usd": 0.0,
                        }
                    )
                    _apply_realized_pnl(state, leg_pnl, now, logger, "quick_profit_partial", {"token_address": pos.token_address})
                    pos.size_usd -= result.filled_usd
                    pos.quick_profit_taken = True
                    logger.trade(result)

                scale = strategy.maybe_scale_in_size(pos, now, unrealized)
                if scale > 0:
                    buy_px = quote.get_effective_price(pos.token_address, "buy", scale)
                    if risk.slippage_ok(buy_px, px):
                        result = executor.execute(pos.symbol, pos.token_address, "buy", scale, buy_px)
                        result.metadata.update(
                            {
                                "token_address": pos.token_address,
                                "token_symbol": pos.symbol,
                                "token_name": pos.token_name,
                                "pair_address": pos.pair_address,
                                "dex_id": pos.dex_id,
                                "chain_id": pos.chain_id,
                                "effective_price": buy_px,
                            }
                        )
                        pos.size_usd += result.filled_usd
                        pos.last_scale_in_at = now
                        logger.trade(result)

                exit_now, reason = strategy.should_exit(pos, now, unrealized)
                if exit_now or pos.size_usd <= 0:
                    result = executor.execute(pos.symbol, pos.token_address, "sell", pos.size_usd, px)
                    qty = pos.size_usd / max(pos.entry_price_usd, 1e-9)
                    exit_value = qty * px
                    pnl = exit_value - pos.size_usd
                    result.metadata.update(
                        {
                            "token_address": pos.token_address,
                            "token_symbol": pos.symbol,
                            "token_name": pos.token_name,
                            "pair_address": pos.pair_address,
                            "dex_id": pos.dex_id,
                            "chain_id": pos.chain_id,
                            "entry_price": pos.entry_price_usd,
                            "exit_price": px,
                            "qty": qty,
                            "entry_value_usd": pos.size_usd,
                            "exit_value_usd": exit_value,
                            "realized_pnl_usd": pnl,
                            "result": _result_label(pnl),
                            "exit_reason": reason,
                            "fees_est_usd": 0.0,
                        }
                    )
                    _apply_realized_pnl(state, pnl, now, logger, reason, {"token_address": pos.token_address})
                    state.active_position = None
                    state.last_trade_ts_iso = iso_utc(now)
                    token_last_trade_iso[pos.token_address] = iso_utc(now)
                    logger.trade(result)
                    logger.event(
                        "position_closed",
                        {
                            "reason": reason,
                            "realized_pnl_usd": pnl,
                            "result": _result_label(pnl),
                            "exit_reason": reason,
                            "entry_price": pos.entry_price_usd,
                            "exit_price": px,
                            "qty": qty,
                            "entry_value_usd": pos.size_usd,
                            "exit_value_usd": exit_value,
                            "fees_est_usd": 0.0,
                            "token_address": pos.token_address,
                            "token_symbol": pos.symbol,
                            "token_name": pos.token_name,
                            "pair_address": pos.pair_address,
                            "dex_id": pos.dex_id,
                            "chain_id": pos.chain_id,
                        },
                    )
                    state.open_unrealized_usd = 0.0
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
            logger.event("loop_error", {"error": str(exc), "traceback": traceback.format_exc(), "loop": loop_count})
            store.save(state)
            stop_event.wait(float(cfg.get("loop_interval_sec", default=3)))


def run() -> None:
    stop_event = Event()
    try:
        run_loop(stop_event)
    except KeyboardInterrupt:
        stop_event.set()
        time.sleep(0.05)
