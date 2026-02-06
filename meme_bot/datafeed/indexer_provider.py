from __future__ import annotations

from datetime import datetime
from typing import Any

import requests

from meme_bot.models import CandidateToken


class IndexerProvider:
    """HTTP stub for indexer/universe source."""

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg 
        self.base_url = cfg["base_url"].rstrip("/")
        self.universe_path = cfg["universe_path"]
        self.top_n = int(cfg.get("top_n", 20))
        self.timeout_sec = int(cfg.get("timeout_sec", 10))
        self.api_key = cfg.get("api_key", "")

    def fetch_universe(self) -> list[CandidateToken]:
        url = f"{self.base_url}{self.universe_path}"
        q = self.cfg.get("search_q", "solana")
        data = self._get_json(url, params={"q": q})

        tokens = []
        for p in data.get("pairs", [])[: self.top_n]:
            base = (p.get("baseToken") or {})
            tokens.append(
                CandidateToken(
                    symbol=base.get("symbol", "UNKNOWN"),
                    address=base.get("address", ""),
                    price_usd=float(p.get("priceUsd") or 0.0),
                    liquidity_usd=float((p.get("liquidity") or {}).get("usd") or 0.0),
                    volume_5m_usd=float((p.get("volume") or {}).get("h24") or 0.0),  # placeholder
                    momentum_score=float((p.get("priceChange") or {}).get("m5") or 0.0),
                    last_update_ts=datetime.utcnow(),
                )
            )
        return tokens


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
