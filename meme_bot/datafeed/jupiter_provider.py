from __future__ import annotations

import time
from typing import Any

import requests


class JupiterProvider:
    """Best-effort Jupiter quote client for paper-trade enrichment."""

    def __init__(self, cfg: dict[str, Any], *, quote_cache: dict[str, tuple[float, dict[str, Any]]] | None = None):
        self.base_url = str(cfg.get("base_url", "https://api.jup.ag")).rstrip("/")
        self.quote_path = str(cfg.get("quote_path", "/swap/v1/quote"))
        self.token_path_template = str(cfg.get("token_path_template", "/tokens/v1/token/{mint}"))
        self.timeout_sec = float(cfg.get("timeout_sec", 3))
        self.slippage_bps = int(cfg.get("slippage_bps", 150))
        self.cache_ttl_sec = max(0.0, float(cfg.get("cache_ttl_sec", 2)))
        self.usdc_mint = str(cfg.get("usdc_mint", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"))
        self.usdc_decimals = int(cfg.get("usdc_decimals", 6))
        self._quote_cache = quote_cache if quote_cache is not None else {}
        self._token_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def quote_for_swap(self, token_mint: str, side: str, notional_usd: float, mid_price_usd: float) -> dict[str, Any]:
        payload = {
            "quote_in_amount": None,
            "quote_out_amount": None,
            "quote_price_impact_pct": None,
            "quote_route_labels": None,
            "expected_total_cost_pct": None,
            "expected_total_cost_usd": None,
            "quote_error": None,
            "fees_est_usd_quote": None,
        }

        if not token_mint or notional_usd <= 0:
            payload["quote_error"] = "invalid_quote_input"
            return payload

        try:
            quote_data = self._fetch_quote(token_mint, side, notional_usd, mid_price_usd)

            out_decimals = self._mint_decimals(str(quote_data.get("outputMint") or ""))
            in_decimals = self._mint_decimals(str(quote_data.get("inputMint") or ""))

            out_amount_raw = self._to_float(quote_data.get("outAmount"))
            in_amount_raw = self._to_float(quote_data.get("inAmount"))
            out_amount = self._normalize_amount(out_amount_raw, out_decimals)
            in_amount = self._normalize_amount(in_amount_raw, in_decimals)
            impact_pct = self._to_float(quote_data.get("priceImpactPct"))

            payload["quote_in_amount"] = in_amount
            payload["quote_out_amount"] = out_amount
            payload["quote_price_impact_pct"] = impact_pct
            payload["quote_route_labels"] = self._route_labels(quote_data)

            cost_usd, cost_pct = self._expected_cost(
                side=side,
                mid_price_usd=mid_price_usd,
                notional_usd=notional_usd,
                quote_out_amount=out_amount,
                quote_price_impact_pct=impact_pct,
            )
            payload["expected_total_cost_usd"] = cost_usd
            payload["expected_total_cost_pct"] = cost_pct
            return payload
        except Exception as exc:  # noqa: BLE001
            payload["quote_error"] = str(exc)
            return payload

    def _fetch_quote(self, token_mint: str, side: str, notional_usd: float, mid_price_usd: float) -> dict[str, Any]:
        if side == "buy":
            amount_atoms = max(1, int(round(notional_usd * (10**self.usdc_decimals))))
            params = {
                "inputMint": self.usdc_mint,
                "outputMint": token_mint,
                "amount": str(amount_atoms),
                "slippageBps": str(self.slippage_bps),
                "swapMode": "ExactIn",
            }
        else:
            if mid_price_usd <= 0:
                raise ValueError("invalid_mid_price_for_sell_quote")
            token_decimals = self._mint_decimals(token_mint)
            token_amount = notional_usd / mid_price_usd
            amount_atoms = max(1, int(round(token_amount * (10**token_decimals))))
            params = {
                "inputMint": token_mint,
                "outputMint": self.usdc_mint,
                "amount": str(amount_atoms),
                "slippageBps": str(self.slippage_bps),
                "swapMode": "ExactIn",
            }

        url = f"{self.base_url}{self.quote_path}"
        cache_key = f"{url}|{sorted(params.items())}"
        cached = self._cache_get(self._quote_cache, cache_key)
        if cached is not None:
            return cached

        response = requests.get(url, params=params, timeout=self.timeout_sec)
        response.raise_for_status()
        data = response.json()
        self._cache_set(self._quote_cache, cache_key, data)
        return data

    def _mint_decimals(self, mint: str) -> int:
        if not mint:
            return self.usdc_decimals
        if mint == self.usdc_mint:
            return self.usdc_decimals

        cache_key = f"token:{mint}"
        cached = self._cache_get(self._token_cache, cache_key)
        if cached is not None:
            return int(cached.get("decimals", 0))

        path = self.token_path_template.replace("{mint}", mint)
        url = f"{self.base_url}{path}"
        response = requests.get(url, timeout=self.timeout_sec)
        response.raise_for_status()
        data = response.json() if response.text else {}
        self._cache_set(self._token_cache, cache_key, data)
        return int(data.get("decimals", 0))

    def _expected_cost(
        # expected_total_cost_pct is measured versus mid-price value:
        # - buy: (mid_tokens_out - quoted_tokens_out) / mid_tokens_out * 100
        # - sell: (notional_usd - quoted_usdc_out) / notional_usd * 100
        self,
        *,
        side: str,
        mid_price_usd: float,
        notional_usd: float,
        quote_out_amount: float,
        quote_price_impact_pct: float,
    ) -> tuple[float | None, float | None]:
        if mid_price_usd > 0 and notional_usd > 0 and quote_out_amount > 0:
            if side == "buy":
                mid_out_tokens = notional_usd / mid_price_usd
                if mid_out_tokens > 0:
                    cost_pct = max(0.0, (mid_out_tokens - quote_out_amount) / mid_out_tokens * 100)
                    return notional_usd * (cost_pct / 100), cost_pct
            else:
                mid_out_usd = notional_usd
                cost_pct = max(0.0, (mid_out_usd - quote_out_amount) / mid_out_usd * 100)
                return mid_out_usd * (cost_pct / 100), cost_pct

        if quote_price_impact_pct >= 0 and notional_usd > 0:
            return notional_usd * (quote_price_impact_pct / 100), quote_price_impact_pct

        return None, None

    def _route_labels(self, quote_data: dict[str, Any]) -> list[str] | None:
        labels: list[str] = []

        route_plan = quote_data.get("routePlan") or []
        for leg in route_plan:
            swap_info = leg.get("swapInfo") or {}
            label = swap_info.get("label")
            if label:
                labels.append(str(label))

        market_infos = quote_data.get("marketInfos") or []
        for info in market_infos:
            label = info.get("label") or info.get("ammLabel") or info.get("dexLabel")
            if label:
                labels.append(str(label))

        if not labels:
            return None
        return list(dict.fromkeys(labels))

    @staticmethod
    def _normalize_amount(amount: float, decimals: int) -> float:
        if decimals < 0:
            decimals = 0
        return amount / (10**decimals)

    @staticmethod
    def _to_float(value: Any) -> float:
        if value is None:
            return 0.0
        return float(value)

    def _cache_get(self, cache: dict[str, tuple[float, dict[str, Any]]], key: str) -> dict[str, Any] | None:
        if self.cache_ttl_sec <= 0:
            return None
        entry = cache.get(key)
        if not entry:
            return None
        expires_at, data = entry
        if time.time() >= expires_at:
            cache.pop(key, None)
            return None
        return data

    def _cache_set(self, cache: dict[str, tuple[float, dict[str, Any]]], key: str, data: dict[str, Any]) -> None:
        if self.cache_ttl_sec <= 0:
            return
        cache[key] = (time.time() + self.cache_ttl_sec, data)
