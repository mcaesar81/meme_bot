from __future__ import annotations

from datetime import datetime
from typing import Any

import requests

from meme_bot.models import CandidateToken


class IndexerProvider:
    """HTTP stub for indexer/universe source."""

    def __init__(self, cfg: dict[str, Any]):
        self.base_url = cfg["base_url"].rstrip("/")
        self.universe_path = cfg["universe_path"]
        self.top_n = int(cfg.get("top_n", 20))
        self.timeout_sec = int(cfg.get("timeout_sec", 10))
        self.api_key = cfg.get("api_key", "")

    def fetch_universe(self) -> list[CandidateToken]:
        url = f"{self.base_url}{self.universe_path}"
        params = {"top_n": self.top_n, "window": "5m"}

        data = self._get_json(url, params=params)
        # TODO(user): map provider-specific fields to normalized CandidateToken fields.
        # Expected provider payload example (placeholder):
        # {"tokens": [{"symbol": "MEME", "address": "...", "price": 0.01,
        #               "liquidity_usd": 12345, "volume_5m_usd": 7000,
        #               "momentum": 0.42, "updated_at": "2026-01-01T00:00:00Z"}]}
        tokens = []
        for item in data.get("tokens", []):
            tokens.append(
                CandidateToken(
                    symbol=item.get("symbol", "UNKNOWN"),
                    address=item.get("address", ""),
                    price_usd=float(item.get("price", 0.0)),
                    liquidity_usd=float(item.get("liquidity_usd", 0.0)),
                    volume_5m_usd=float(item.get("volume_5m_usd", 0.0)),
                    momentum_score=float(item.get("momentum", 0.0)),
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
