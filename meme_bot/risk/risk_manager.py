from __future__ import annotations

from datetime import timedelta
from typing import Any

from meme_bot.models import CandidateToken, RuntimeState
from meme_bot.utils.timefmt import ensure_aware_utc, now_utc, parse_iso_datetime


class RiskManager:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._time_parse_warned = False

    def filter_candidates(self, candidates: list[CandidateToken]) -> list[CandidateToken]:
        return [c for c in candidates if self.evaluate_candidate(c)[0]]

    def evaluate_candidate(self, candidate: CandidateToken) -> tuple[bool, str]:
        exclude_symbols = set(s.upper() for s in self.cfg.get("exclude_symbols", []))
        exclude_addresses = {a.strip() for a in self.cfg.get("exclude_token_addresses", []) if a}

        if candidate.address in exclude_addresses:
            return False, "excluded_token_address"
        if candidate.symbol.upper() in exclude_symbols:
            return False, "excluded_symbol"

        min_liq = float(self.cfg["min_liquidity_usd"])
        min_vol = float(self.cfg["min_volume_5m_usd"])
        if candidate.liquidity_usd < min_liq:
            return False, "liquidity_too_low"
        if candidate.volume_5m_usd < min_vol:
            return False, "volume_too_low"
        return True, "ok"

    def can_open_trade(self, state: RuntimeState, now=None) -> tuple[bool, str]:
        current = ensure_aware_utc(now) if now else now_utc()

        if state.trades_today >= int(self.cfg["max_trades_per_day"]):
            return False, "max_trades_per_day"
        if state.daily_pnl_usd <= -float(self.cfg["max_loss_per_day_usd"]):
            return False, "max_loss_per_day"
        if state.hourly_pnl_usd <= -float(self.cfg["max_loss_per_hour_usd"]):
            return False, "max_loss_per_hour"

        last = parse_iso_datetime(state.last_trade_ts_iso)
        if last is None and state.last_trade_ts_iso and not self._time_parse_warned:
            self._time_parse_warned = True
            return True, "time_parse_warn"

        if last:
            cooldown = timedelta(seconds=int(self.cfg["trade_cooldown_sec"]))
            if current - last < cooldown:
                return False, "trade_cooldown"

        return True, "ok"

    def slippage_ok(self, quote_price: float, index_price: float) -> bool:
        if index_price <= 0:
            return False
        slippage = abs(quote_price - index_price) / index_price * 10_000
        return slippage <= float(self.cfg["slippage_limit_bps"])

    def best_candidate_with_reason(self, candidates: list[CandidateToken]) -> tuple[CandidateToken | None, str]:
        if not candidates:
            return None, "no_candidates"

        first_reason = "signal_false"
        for candidate in sorted(candidates, key=lambda c: c.momentum_score, reverse=True):
            allowed, reason = self.evaluate_candidate(candidate)
            if allowed:
                return candidate, "ok"
            first_reason = reason

        return None, first_reason
