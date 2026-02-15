from __future__ import annotations

import time
import traceback
from pathlib import Path
from collections import Counter, defaultdict, deque
from dataclasses import asdict
from datetime import datetime, timedelta
from threading import Event

from meme_bot.config import Config
from meme_bot.datafeed import IndexerProvider, JupiterProvider, MarketDataService, QuoteProvider
from meme_bot.execution import DummyExecutor, RealExecutor
from meme_bot.logger import JsonLineLogger
from meme_bot.models import BotMode, CandidateToken, Position, RuntimeState
from meme_bot.risk import RiskManager
from meme_bot.state_machine import StateMachine
from meme_bot.state_store import RuntimeStateStore
from meme_bot.strategy import MomentumScalpStrategy
import requests
from dotenv import load_dotenv

from meme_bot.utils.timefmt import LOCAL_TZ, iso_utc, now_utc, parse_iso_datetime

EPSILON_FLAT = 0.005


class QuoteTimeoutError(Exception):
    pass


def _load_local_dotenv(cfg_path: str = "config.yaml") -> None:
    repo_root = Path(__file__).resolve().parents[1]
    repo_env = repo_root / ".env"
    cfg_env = Path(cfg_path).resolve().parent / ".env"

    if repo_env.is_file():
        load_dotenv(repo_env, override=False)
    elif cfg_env.is_file():
        load_dotenv(cfg_env, override=False)


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
        "price_change_m5": candidate.price_change_m5,
        "price_change_m15": candidate.price_change_m15,
        "price_change_h1": candidate.price_change_h1,
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


def _record_trade_attempt(
    state: RuntimeState,
    token_last_trade_iso: dict[str, str],
    token_address: str,
    now: datetime,
) -> None:
    state.trades_today += 1
    state.last_trade_ts_iso = iso_utc(now)
    token_last_trade_iso[token_address] = iso_utc(now)


def _estimate_fee_usd(notional_usd: float, fee_bps: float) -> float:
    return notional_usd * (fee_bps / 10_000)


def _weighted_avg_price(current_price: float, current_size: float, added_price: float, added_size: float) -> float:
    total = current_size + added_size
    if total <= 0:
        return current_price
    return (current_price * current_size + added_price * added_size) / total


def _result_label_with_fees(net_pnl_usd: float, fees_usd: float, flat_good_ratio: float) -> str:
    if net_pnl_usd >= 0:
        return "WIN"
    if flat_good_ratio > 0 and net_pnl_usd >= -fees_usd * flat_good_ratio:
        return "FLAT_GOOD"
    return "LOSS"


def _quote_enrichment_defaults() -> dict:
    return {
        "quote_in_amount": None,
        "quote_out_amount": None,
        "quote_price_impact_pct": None,
        "quote_route_labels": None,
        "expected_total_cost_pct": None,
        "expected_total_cost_usd": None,
        "fees_est_usd_quote": None,
        "quote_error": None,
        "quote_warning": None,
    }


def _with_config_fee_fields(payload: dict, fee_usd: float) -> dict:
    payload["fees_est_usd"] = fee_usd
    payload["fees_est_usd_config"] = fee_usd
    return payload


def _load_position(raw: dict) -> Position:
    parsed = dict(raw)
    for key in ("opened_at", "last_scale_in_at", "post_add_confirm_until", "quick_partial_at", "range_window_start", "max_hold_extend_until"):
        dt = parse_iso_datetime(parsed.get(key))
        if dt:
            parsed[key] = dt
    if parsed.get("last_fill_price_usd", 0) <= 0 and parsed.get("entry_price_usd"):
        parsed["last_fill_price_usd"] = parsed["entry_price_usd"]
    if parsed.get("last_high_price_usd", 0) <= 0 and parsed.get("entry_price_usd"):
        parsed["last_high_price_usd"] = parsed["entry_price_usd"]
    if parsed.get("last_high_after_partial_usd", 0) <= 0 and parsed.get("last_high_price_usd"):
        parsed["last_high_after_partial_usd"] = parsed["last_high_price_usd"]
    if parsed.get("range_high_price_usd", 0) <= 0 and parsed.get("entry_price_usd"):
        parsed["range_high_price_usd"] = parsed["entry_price_usd"]
    if parsed.get("range_low_price_usd", 0) <= 0 and parsed.get("entry_price_usd"):
        parsed["range_low_price_usd"] = parsed["entry_price_usd"]
    return Position(**parsed)


def _safe_quote(quote: QuoteProvider, token_address: str, side: str, size_usd: float) -> float:
    try:
        return quote.get_effective_price(token_address, side, size_usd)
    except requests.exceptions.Timeout as exc:
        raise QuoteTimeoutError from exc


def run_loop(stop_event: Event, cfg_path: str = "config.yaml") -> None:
    _load_local_dotenv(cfg_path)

    last_mode: str | None = None
    loop_count = 0
    repeat_top_count = 0
    last_top_token: str | None = None
    token_last_trade_iso: dict[str, str] = {}
    token_bad_trades: dict[str, deque[datetime]] = defaultdict(deque)
    token_cooldowns: dict[str, datetime] = {}
    summary_counts = Counter()
    summary_hold_secs: list[float] = []
    summary_pnls: list[float] = []
    summary_token_counts: Counter[str] = Counter()
    logged_timezone = False
    last_quote_timeout_log: datetime | None = None
    jupiter_quote_cache: dict[str, tuple[float, dict]] = {}

    while not stop_event.is_set():
        cfg = Config.load(cfg_path)
        logger = JsonLineLogger(
            cfg.resolve_path("logging", "events_path", default="data/events.log"),
            cfg.resolve_path("logging", "trades_path", default="data/trades.jsonl"),
        )
        store = RuntimeStateStore(cfg.resolve_path("state_store", "runtime_state_path", default="data/runtime_state.json"))

        indexer = IndexerProvider(cfg.get("providers", "indexer"))
        quote = QuoteProvider(cfg.get("providers", "quote"))
        jupiter = JupiterProvider(cfg.get("providers", "jupiter", default={}), quote_cache=jupiter_quote_cache)
        market = MarketDataService(indexer)
        risk = RiskManager(cfg.get("risk"))
        strategy = MomentumScalpStrategy(cfg.get("strategy"))

        mode = cfg.get("mode", default="paper")
        executor = DummyExecutor() if mode == "paper" else RealExecutor()
        fee_bps = float(cfg.get("fees", "fee_bps", default=0))
        min_net_win_usd = float(cfg.get("strategy", "min_net_win_usd", default=EPSILON_FLAT))
        min_net_loss_usd = float(cfg.get("strategy", "min_net_loss_usd", default=EPSILON_FLAT))
        scale_offset_bps = float(cfg.get("strategy", "scale_offset_bps", default=0))
        post_add_confirm_sec = float(cfg.get("strategy", "post_add_confirm_sec", default=0))
        post_add_stall_exit_sec = float(cfg.get("strategy", "post_add_stall_exit_sec", default=0))
        max_flat_adds = int(cfg.get("strategy", "max_flat_adds", default=0))
        scale_offset_bps_after_partial = float(cfg.get("strategy", "scale_offset_bps_after_partial", default=scale_offset_bps))
        post_partial_scale_cooldown_sec = float(cfg.get("strategy", "post_partial_scale_cooldown_sec", default=0))
        quick_profit_usd = float(cfg.get("strategy", "quick_profit_usd", default=0))
        quick_partial_exit_net_usd = float(cfg.get("strategy", "quick_partial_exit_net_usd", default=0))
        salvaged_loss_usd = float(cfg.get("strategy", "salvaged_loss_usd", default=min_net_loss_usd))
        max_adds_per_position = int(cfg.get("strategy", "max_adds_per_position", default=0))
        scale_require_new_high_after_partial = bool(cfg.get("strategy", "scale_require_new_high_after_partial", default=False))
        scale_volatility_window_sec = float(cfg.get("strategy", "scale_volatility_window_sec", default=0))
        quick_profit_fee_multiplier = float(cfg.get("strategy", "quick_profit_fee_multiplier", default=1.0))
        flat_good_fee_ratio = float(cfg.get("strategy", "flat_good_fee_ratio", default=0.0))
        max_hold_fee_extend_sec = float(cfg.get("strategy", "max_hold_fee_extend_sec", default=0))
        max_hold_fee_max_ext = int(cfg.get("strategy", "max_hold_fee_max_ext", default=0))
        per_token_cooldown_sec = float(cfg.get("strategy", "per_token_cooldown_sec", default=0))
        token_bad_limit = int(cfg.get("risk", "token_cooldown_bad_trades", default=0))
        token_bad_window_min = float(cfg.get("risk", "token_cooldown_window_min", default=0))
        token_cooldown_min = float(cfg.get("risk", "token_cooldown_min", default=0))
        max_loss_per_position = float(cfg.get("risk", "max_loss_per_position_usd", default=0))
        fee_block_max_cycles = int(cfg.get("risk", "fee_block_max_cycles", default=0))
        fee_block_deterioration_usd = float(cfg.get("risk", "fee_block_deterioration_usd", default=0))
        min_entry_vol_m5 = float(cfg.get("risk", "min_entry_volatility_m5_pct", default=0))
        min_entry_vol_fee_mult = float(cfg.get("risk", "min_entry_volatility_fee_mult", default=0))
        max_quote_price_impact_pct = float(cfg.get("risk", "max_quote_price_impact_pct", default=0))
        max_expected_total_cost_pct = float(cfg.get("risk", "max_expected_total_cost_pct", default=0))
        min_expected_edge_to_cost_mult = float(cfg.get("risk", "min_expected_edge_to_cost_mult", default=0))
        summary_every_loops = int(cfg.get("logging", "summary_every_loops", default=20))

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
        if token_cooldowns:
            for token, expires_at in list(token_cooldowns.items()):
                if now >= expires_at:
                    del token_cooldowns[token]

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
                    cooldown_until = token_cooldowns.get(best.address)
                    if cooldown_until and now < cooldown_until:
                        best_reason = "token_cooldown"
                        logger.event(
                            "token_cooldown_blocked",
                            {
                                "token_address": best.address,
                                "token_symbol": best.symbol,
                                "token_name": best.name,
                                "cooldown_until": cooldown_until.isoformat(),
                            },
                        )

                entry_quote_data = _quote_enrichment_defaults()
                if best_reason == "ok" and best:
                    size = strategy.initial_entry_size()
                    entry_fee_break_even_pct = fee_bps * 2 / 100
                    volatility_pct = abs(best.price_change_m5)
                    if volatility_pct < min_entry_vol_m5:
                        best_reason = "entry_volatility_too_low"
                    elif min_entry_vol_fee_mult > 0 and volatility_pct < entry_fee_break_even_pct * min_entry_vol_fee_mult:
                        best_reason = "entry_move_too_small_for_fees"
                    if best_reason == "ok":
                        reference_price = _safe_quote(quote, best.address, "buy", size)
                        if reference_price <= 0:
                            best_reason = "price_unavailable"
                        else:
                            slippage_limit = risk.slippage_limit_bps(best.liquidity_usd)
                            if not risk.slippage_ok(reference_price, best.price_usd, slippage_limit):
                                best_reason = "slippage_too_high"
                            else:
                                if mode == "paper":
                                    entry_quote_data = jupiter.quote_for_swap(best.address, "buy", size, best.price_usd)
                                    expected_cost_pct = entry_quote_data.get("expected_total_cost_pct")
                                    impact_pct = entry_quote_data.get("quote_price_impact_pct")
                                    if max_quote_price_impact_pct > 0 and impact_pct is not None and impact_pct > max_quote_price_impact_pct:
                                        best_reason = "quote_price_impact_too_high"
                                    elif max_expected_total_cost_pct > 0 and expected_cost_pct is not None and expected_cost_pct > max_expected_total_cost_pct:
                                        best_reason = "expected_total_cost_too_high"
                                    elif min_expected_edge_to_cost_mult > 0 and expected_cost_pct is not None:
                                        expected_edge_pct = max(0.0, best.price_change_m5)  # edge proxy = positive m5 trend percent
                                        if expected_edge_pct < expected_cost_pct * min_expected_edge_to_cost_mult:
                                            best_reason = "edge_below_expected_cost_multiple"
                                if best_reason == "ok":
                                    result = executor.execute(best.symbol, best.address, "buy", size, reference_price)
                                    _record_trade_attempt(state, token_last_trade_iso, best.address, now)
                                    entry_fee = _estimate_fee_usd(result.filled_usd, fee_bps)
                                    result.metadata.update(_token_meta(best))
                                    result.metadata["reference_price_usd"] = best.price_usd
                                    _with_config_fee_fields(result.metadata, entry_fee)
                                    result.metadata.update(entry_quote_data)
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
                                            last_fill_price_usd=result.avg_price_usd,
                                            fees_paid_usd=entry_fee,
                                            last_high_price_usd=result.avg_price_usd,
                                            last_high_after_partial_usd=result.avg_price_usd,
                                            range_high_price_usd=result.avg_price_usd,
                                            range_low_price_usd=result.avg_price_usd,
                                            range_window_start=now,
                                        )
                                    )
                                    logger.trade(result)

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
                        payload["expected_edge_pct"] = max(0.0, best.price_change_m5)
                    payload["min_expected_edge_to_cost_mult"] = min_expected_edge_to_cost_mult
                    payload["max_quote_price_impact_pct"] = max_quote_price_impact_pct
                    payload["max_expected_total_cost_pct"] = max_expected_total_cost_pct
                    payload.update(entry_quote_data)
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
                px = _safe_quote(quote, pos.token_address, "sell", pos.size_usd)
                unrealized = (px - pos.entry_price_usd) * (pos.size_usd / max(pos.entry_price_usd, 1e-9))
                state.open_unrealized_usd = unrealized
                prev_unrealized = pos.last_unrealized_usd
                pos.max_favorable_usd = max(pos.max_favorable_usd, unrealized)
                pos.max_adverse_usd = min(pos.max_adverse_usd, unrealized)
                pos.last_unrealized_usd = unrealized
                exit_fee_est = _estimate_fee_usd(pos.size_usd, fee_bps)
                net_unrealized = unrealized - (pos.fees_paid_usd + exit_fee_est)
                pos.last_high_price_usd = max(pos.last_high_price_usd, px)
                if scale_volatility_window_sec > 0:
                    if not pos.range_window_start or (now - pos.range_window_start).total_seconds() >= scale_volatility_window_sec:
                        pos.range_window_start = now
                        pos.range_high_price_usd = px
                        pos.range_low_price_usd = px
                    else:
                        pos.range_high_price_usd = max(pos.range_high_price_usd, px)
                        pos.range_low_price_usd = min(pos.range_low_price_usd, px)

                if (
                    pos.quick_profit_taken
                    and quick_partial_exit_net_usd > 0
                    and pos.realized_net_usd >= quick_partial_exit_net_usd
                    and unrealized <= 0
                ):
                    exit_now = True
                    reason = "quick_partial_exit"
                else:
                    exit_now = False
                    reason = "hold"

                if pos.awaiting_post_add_confirm and pos.post_add_confirm_until:
                    if px > pos.last_fill_price_usd:
                        pos.awaiting_post_add_confirm = False
                    elif now >= pos.post_add_confirm_until:
                        pos.awaiting_post_add_confirm = False
                        pos.adds_blocked = True
                        logger.event(
                            "post_add_not_confirmed",
                            {
                                "token_address": pos.token_address,
                                "token_symbol": pos.symbol,
                                "token_name": pos.token_name,
                                "last_fill_price": pos.last_fill_price_usd,
                                "confirm_until": pos.post_add_confirm_until.isoformat(),
                            },
                        )

                quick_profit_target = max(quick_profit_usd, pos.fees_paid_usd * quick_profit_fee_multiplier)
                partial = strategy.quick_profit_partial_size(pos, unrealized, min_profit_usd=quick_profit_target)
                if partial > 0:
                    partial_quote_data = _quote_enrichment_defaults()
                    if mode == "paper":
                        partial_quote_data = jupiter.quote_for_swap(pos.token_address, "sell", partial, px)
                    result = executor.execute(pos.symbol, pos.token_address, "sell", partial, px)
                    _record_trade_attempt(state, token_last_trade_iso, pos.token_address, now)
                    leg_qty = partial / max(pos.entry_price_usd, 1e-9)
                    leg_exit_value = leg_qty * px
                    entry_fee_portion = pos.fees_paid_usd * (partial / max(pos.size_usd, 1e-9))
                    exit_fee = _estimate_fee_usd(result.filled_usd, fee_bps)
                    leg_gross_pnl = leg_exit_value - partial
                    leg_fees = entry_fee_portion + exit_fee
                    leg_net_pnl = leg_gross_pnl - leg_fees
                    pos.fees_paid_usd -= entry_fee_portion
                    leg_result = _result_label_with_fees(leg_net_pnl, leg_fees, flat_good_fee_ratio)
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
                            "gross_pnl_usd": leg_gross_pnl,
                            "net_pnl_usd": leg_net_pnl,
                            "result": leg_result,
                            "exit_reason": "quick_profit_partial",
                        }
                    )
                    _with_config_fee_fields(result.metadata, leg_fees)
                    result.metadata.update(partial_quote_data)
                    _apply_realized_pnl(state, leg_net_pnl, now, logger, "quick_profit_partial", {"token_address": pos.token_address})
                    pos.size_usd -= result.filled_usd
                    pos.quick_profit_taken = True
                    pos.quick_partial_at = now
                    pos.last_high_after_partial_usd = pos.last_high_price_usd
                    pos.realized_net_usd += leg_net_pnl
                    logger.trade(result)

                scale = strategy.maybe_scale_in_size(pos, now, unrealized)
                if scale > 0:
                    buy_px = _safe_quote(quote, pos.token_address, "buy", scale)
                    scale_reason = None
                    if pos.adds_blocked:
                        scale_reason = "adds_blocked"
                    elif pos.awaiting_post_add_confirm:
                        scale_reason = "awaiting_post_add_confirm"
                    elif pos.quick_profit_taken and pos.quick_partial_at:
                        if (now - pos.quick_partial_at).total_seconds() < post_partial_scale_cooldown_sec:
                            scale_reason = "post_partial_cooldown"
                    elif max_adds_per_position > 0 and pos.add_count >= max_adds_per_position:
                        scale_reason = "max_adds_reached"
                    elif scale_volatility_window_sec > 0 and pos.range_window_start:
                        range_bps = 0.0
                        if pos.range_high_price_usd > 0:
                            range_bps = (pos.range_high_price_usd - pos.range_low_price_usd) / pos.range_high_price_usd * 10_000
                        if range_bps < fee_bps * 2:
                            scale_reason = "low_volatility"
                    elif buy_px < pos.entry_price_usd:
                        scale_reason = "scale_price_below_entry"
                    elif buy_px <= pos.last_fill_price_usd:
                        scale_reason = "scale_price_not_improved"
                    else:
                        offset_bps = scale_offset_bps_after_partial if pos.quick_profit_taken else scale_offset_bps
                        required_px = pos.last_fill_price_usd * (1 + offset_bps / 10_000)
                        if buy_px < required_px:
                            if max_flat_adds > pos.flat_adds_used and buy_px >= pos.entry_price_usd:
                                pos.flat_adds_used += 1
                            else:
                                scale_reason = "scale_offset_not_met"
                        if not scale_reason and scale_require_new_high_after_partial and pos.quick_profit_taken:
                            new_high_target = pos.last_high_after_partial_usd * (1 + offset_bps / 10_000)
                            if buy_px < new_high_target:
                                scale_reason = "partial_no_new_high"

                    if scale_reason:
                        logger.event(
                            "scale_blocked",
                            {
                                "reason": scale_reason,
                                "token_address": pos.token_address,
                                "token_symbol": pos.symbol,
                                "token_name": pos.token_name,
                                "buy_price": buy_px,
                                "last_fill_price": pos.last_fill_price_usd,
                            },
                        )
                    elif risk.slippage_ok(buy_px, px):
                        scale_quote_data = _quote_enrichment_defaults()
                        if mode == "paper":
                            scale_quote_data = jupiter.quote_for_swap(pos.token_address, "buy", scale, buy_px)
                        result = executor.execute(pos.symbol, pos.token_address, "buy", scale, buy_px)
                        _record_trade_attempt(state, token_last_trade_iso, pos.token_address, now)
                        add_fee = _estimate_fee_usd(result.filled_usd, fee_bps)
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
                        _with_config_fee_fields(result.metadata, add_fee)
                        result.metadata.update(scale_quote_data)
                        pos.entry_price_usd = _weighted_avg_price(
                            pos.entry_price_usd,
                            pos.size_usd,
                            result.avg_price_usd,
                            result.filled_usd,
                        )
                        pos.size_usd += result.filled_usd
                        pos.fees_paid_usd += add_fee
                        pos.last_scale_in_at = now
                        pos.last_fill_price_usd = result.avg_price_usd
                        pos.add_count += 1
                        if post_add_confirm_sec > 0:
                            pos.awaiting_post_add_confirm = True
                            pos.post_add_confirm_until = now + timedelta(seconds=post_add_confirm_sec)
                        logger.trade(result)

                if max_loss_per_position > 0 and unrealized <= -max_loss_per_position:
                    exit_now = True
                    reason = "hard_stop_exit"
                    logger.event(
                        "hard_stop_exit",
                        {
                            "token_address": pos.token_address,
                            "token_symbol": pos.symbol,
                            "token_name": pos.token_name,
                            "unrealized_usd": unrealized,
                            "max_loss_per_position_usd": max_loss_per_position,
                        },
                    )
                if not exit_now:
                    stall_override = post_add_stall_exit_sec if pos.adds_blocked and post_add_stall_exit_sec > 0 else None
                    exit_now, reason = strategy.should_exit(
                        pos,
                        now,
                        unrealized,
                        net_unrealized,
                        stall_override_sec=stall_override,
                    )
                    if exit_now and reason == "stall_exit" and fee_bps > 0:
                        fee_break_even = pos.size_usd * (fee_bps * 2 / 10_000)
                        if unrealized < fee_break_even:
                            exit_now = False
                            reason = "hold_fee_gate"
                            logger.event(
                                "stall_exit_blocked_fee",
                                {
                                    "token_address": pos.token_address,
                                    "token_symbol": pos.symbol,
                                    "token_name": pos.token_name,
                                    "unrealized_usd": unrealized,
                                    "fee_break_even_usd": fee_break_even,
                                },
                            )
                            pos.fee_blocked_count += 1
                            if fee_block_max_cycles > 0 and pos.fee_blocked_count >= fee_block_max_cycles:
                                if unrealized <= prev_unrealized - fee_block_deterioration_usd:
                                    exit_now = True
                                    reason = "fee_block_timeout_exit"
                                    logger.event(
                                        "fee_block_timeout_exit",
                                        {
                                            "token_address": pos.token_address,
                                            "token_symbol": pos.symbol,
                                            "token_name": pos.token_name,
                                            "fee_blocked_count": pos.fee_blocked_count,
                                            "unrealized_usd": unrealized,
                                        },
                                    )
                    if exit_now and reason == "max_hold" and fee_bps > 0:
                        fee_break_even = pos.size_usd * (fee_bps * 2 / 10_000)
                        if unrealized < fee_break_even:
                            if unrealized <= prev_unrealized - fee_block_deterioration_usd:
                                exit_now = True
                                reason = "max_hold_fee_exit"
                            else:
                                if max_hold_fee_extend_sec > 0 and (max_hold_fee_max_ext <= 0 or pos.max_hold_extend_count < max_hold_fee_max_ext):
                                    pos.max_hold_extend_until = now + timedelta(seconds=max_hold_fee_extend_sec)
                                    pos.max_hold_extend_count += 1
                                    exit_now = False
                                    reason = "hold_fee_gate"
                                    logger.event(
                                        "max_hold_extended",
                                        {
                                            "token_address": pos.token_address,
                                            "token_symbol": pos.symbol,
                                            "token_name": pos.token_name,
                                            "unrealized_usd": unrealized,
                                            "fee_break_even_usd": fee_break_even,
                                            "extend_until": pos.max_hold_extend_until.isoformat(),
                                        },
                                    )
                                else:
                                    exit_now = False
                                    reason = "hold_fee_gate"
                                    logger.event(
                                        "max_hold_blocked_fee",
                                        {
                                            "token_address": pos.token_address,
                                            "token_symbol": pos.symbol,
                                            "token_name": pos.token_name,
                                            "unrealized_usd": unrealized,
                                            "fee_break_even_usd": fee_break_even,
                                        },
                                    )
                                pos.fee_blocked_count += 1
                                if fee_block_max_cycles > 0 and pos.fee_blocked_count >= fee_block_max_cycles:
                                    if unrealized <= prev_unrealized - fee_block_deterioration_usd:
                                        exit_now = True
                                        reason = "fee_block_timeout_exit"
                                        logger.event(
                                            "fee_block_timeout_exit",
                                            {
                                                "token_address": pos.token_address,
                                                "token_symbol": pos.symbol,
                                                "token_name": pos.token_name,
                                                "fee_blocked_count": pos.fee_blocked_count,
                                                "unrealized_usd": unrealized,
                                            },
                                        )
                if exit_now or pos.size_usd <= 0:
                    exit_quote_data = _quote_enrichment_defaults()
                    if mode == "paper":
                        exit_quote_data = jupiter.quote_for_swap(pos.token_address, "sell", pos.size_usd, px)
                    result = executor.execute(pos.symbol, pos.token_address, "sell", pos.size_usd, px)
                    _record_trade_attempt(state, token_last_trade_iso, pos.token_address, now)
                    qty = pos.size_usd / max(pos.entry_price_usd, 1e-9)
                    exit_value = qty * px
                    gross_pnl = exit_value - pos.size_usd
                    exit_fee = _estimate_fee_usd(result.filled_usd, fee_bps)
                    total_fees = pos.fees_paid_usd + exit_fee
                    net_pnl = gross_pnl - total_fees
                    result_label = _result_label_with_fees(net_pnl, total_fees, flat_good_fee_ratio)
                    if pos.realized_net_usd > 0 and net_pnl <= 0 and net_pnl >= -salvaged_loss_usd:
                        result_label = "SALVAGED"
                    hold_sec = (now - pos.opened_at).total_seconds()
                    profit_missed = pos.max_favorable_usd - gross_pnl
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
                            "gross_pnl_usd": gross_pnl,
                            "net_pnl_usd": net_pnl,
                            "result": result_label,
                            "exit_reason": reason,
                            "hold_sec": hold_sec,
                            "mfe_usd": pos.max_favorable_usd,
                            "mae_usd": pos.max_adverse_usd,
                            "profit_missed_usd": profit_missed,
                        }
                    )
                    _with_config_fee_fields(result.metadata, total_fees)
                    result.metadata.update(exit_quote_data)
                    _apply_realized_pnl(state, net_pnl, now, logger, reason, {"token_address": pos.token_address})
                    state.active_position = None
                    pos.fees_paid_usd = 0.0
                    logger.trade(result)
                    logger.event(
                        "position_closed",
                        {
                            "reason": reason,
                            "gross_pnl_usd": gross_pnl,
                            "fees_est_usd": total_fees,
                            "fees_est_usd_config": total_fees,
                            "fees_est_usd_quote": exit_quote_data.get("fees_est_usd_quote"),
                            "quote_in_amount": exit_quote_data.get("quote_in_amount"),
                            "quote_out_amount": exit_quote_data.get("quote_out_amount"),
                            "quote_price_impact_pct": exit_quote_data.get("quote_price_impact_pct"),
                            "quote_route_labels": exit_quote_data.get("quote_route_labels"),
                            "expected_total_cost_pct": exit_quote_data.get("expected_total_cost_pct"),
                            "expected_total_cost_usd": exit_quote_data.get("expected_total_cost_usd"),
                            "quote_error": exit_quote_data.get("quote_error"),
                            "quote_warning": exit_quote_data.get("quote_warning"),
                            "net_pnl_usd": net_pnl,
                            "result": result_label,
                            "exit_reason": reason,
                            "entry_price": pos.entry_price_usd,
                            "exit_price": px,
                            "qty": qty,
                            "entry_value_usd": pos.size_usd,
                            "exit_value_usd": exit_value,
                            "hold_sec": hold_sec,
                            "mfe_usd": pos.max_favorable_usd,
                            "mae_usd": pos.max_adverse_usd,
                            "profit_missed_usd": profit_missed,
                            "token_address": pos.token_address,
                            "token_symbol": pos.symbol,
                            "token_name": pos.token_name,
                            "pair_address": pos.pair_address,
                            "dex_id": pos.dex_id,
                            "chain_id": pos.chain_id,
                        },
                    )
                    if per_token_cooldown_sec > 0:
                        cooldown_until = now + timedelta(seconds=per_token_cooldown_sec)
                        token_cooldowns[pos.token_address] = cooldown_until
                        logger.event(
                            "token_cooldown_exit",
                            {
                                "token_address": pos.token_address,
                                "token_symbol": pos.symbol,
                                "token_name": pos.token_name,
                                "cooldown_until": cooldown_until.isoformat(),
                            },
                        )
                    summary_counts.update([result_label.lower()])
                    summary_hold_secs.append(hold_sec)
                    summary_pnls.append(net_pnl)
                    summary_token_counts.update([pos.symbol])
                    if token_bad_limit > 0 and token_bad_window_min > 0 and token_cooldown_min > 0 and net_pnl <= 0:
                        bad_list = token_bad_trades[pos.token_address]
                        window_start = now - timedelta(minutes=token_bad_window_min)
                        while bad_list and bad_list[0] < window_start:
                            bad_list.popleft()
                        bad_list.append(now)
                        if len(bad_list) >= token_bad_limit:
                            cooldown_until = now + timedelta(minutes=token_cooldown_min)
                            token_cooldowns[pos.token_address] = cooldown_until
                            logger.event(
                                "token_cooldown_applied",
                                {
                                    "token_address": pos.token_address,
                                    "token_symbol": pos.symbol,
                                    "token_name": pos.token_name,
                                    "bad_trades": len(bad_list),
                                    "window_min": token_bad_window_min,
                                    "cooldown_min": token_cooldown_min,
                                    "cooldown_until": cooldown_until.isoformat(),
                                },
                            )
                    state.open_unrealized_usd = 0.0
                else:
                    state.active_position = asdict(pos)

            if summary_every_loops > 0 and loop_count % summary_every_loops == 0 and summary_counts:
                avg_hold = sum(summary_hold_secs) / max(len(summary_hold_secs), 1)
                avg_pnl = sum(summary_pnls) / max(len(summary_pnls), 1)
                top_tokens = [token for token, _count in summary_token_counts.most_common(5)]
                logger.event(
                    "session_summary",
                    {
                        "wins": summary_counts.get("win", 0),
                        "losses": summary_counts.get("loss", 0),
                        "flat": summary_counts.get("flat", 0),
                        "avg_hold_sec": avg_hold,
                        "avg_pnl_usd": avg_pnl,
                        "top_tokens": top_tokens,
                    },
                )

            store.save(state)
            stop_event.wait(float(cfg.get("loop_interval_sec", default=3)))

        except TimeoutError:
            machine.set_safe("tx_timeout")
            logger.event("safe_mode", {"reason": "tx_timeout"})
            store.save(state)
            stop_event.wait(float(cfg.get("loop_interval_sec", default=3)))

        except QuoteTimeoutError:
            now = now_utc()
            if not last_quote_timeout_log or (now - last_quote_timeout_log).total_seconds() >= 60:
                logger.event("quote_timeout", {"message": "quote request timed out"})
                last_quote_timeout_log = now
            store.save(state)
            stop_event.wait(float(cfg.get("loop_interval_sec", default=3)))

        except Exception as exc:  # noqa: BLE001
            logger.event("loop_error", {"error": str(exc), "traceback": traceback.format_exc(), "loop": loop_count})
            store.save(state)
            stop_event.wait(float(cfg.get("loop_interval_sec", default=3)))


def run() -> None:
    _load_local_dotenv()
    stop_event = Event()
    try:
        run_loop(stop_event)
    except KeyboardInterrupt:
        stop_event.set()
        time.sleep(0.05)
