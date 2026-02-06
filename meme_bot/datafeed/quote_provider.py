from __future__ import annotations

from typing import Any

import requests


class QuoteProvider:
    """HTTP stub for quote aggregation and effective buy/sell pricing."""

    def __init__(self, cfg: dict[str, Any]):
        self.base_url = cfg["base_url"].rstrip("/")
        self.quote_path = cfg["quote_path"]
        self.timeout_sec = int(cfg.get("timeout_sec", 10))
        self.api_key = cfg.get("api_key", "")

    def get_effective_price(self, token_address: str, side: str, size_usd: float) -> float:
        url = f"{self.base_url}{self.quote_path}/{token_address}"
        data = self._get_json(url, params={})

        # token-pairs returns a list; pick the highest liquidity pair
        pairs = data if isinstance(data, list) else data.get("pairs", [])
        if not pairs:
            return 0.0

        best = sorted(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0.0), reverse=True)[0]
        return float(best.get("priceUsd") or 0.0)


    def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = requests.get(url, params=params, timeout=self.timeout_sec)
            response.raise_for_status()
            return response.json()
        except Exception:
            if not self.api_key:
                raise

        headers = {"Authorization": f"Bearer {self.api_key}"}
        response = requests.get(url, params=params, headers=headers, timeout=self.timeout_sec)
        response.raise_for_status()
        return response.json()
