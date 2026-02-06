from __future__ import annotations

from datetime import datetime
from typing import Any

import requests

from meme_bot.models import CandidateToken


class IndexerProvider:
    """Universe source for candidate pairs/tokens."""

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.base_url = cfg["base_url"].rstrip("/")
        self.universe_path = cfg["universe_path"]
        self.top_n = int(cfg.get("top_n", 20))
        self.timeout_sec = int(cfg.get("timeout_sec", 10))
        self.api_key = cfg.get("api_key", "")
        self.chain_id = str(cfg.get("chain_id", "solana"))
        self.dex_allowlist = {d.lower() for d in cfg.get("dex_allowlist", [])}

    def fetch_universe(self) -> list[CandidateToken]:
        url = f"{self.base_url}{self.universe_path}"
        search_queries = self.cfg.get("search_queries") or [self.cfg.get("search_q", "pump"), "usd", "raydium"]
        pairs: list[dict[str, Any]] = []

        for query in search_queries:
            data = self._get_json(url, params={"q": query})
            pairs.extend(data.get("pairs", []))

        seen_addresses: set[str] = set()
        tokens: list[CandidateToken] = []

        for pair in pairs:
            base = pair.get("baseToken") or {}
            quote = pair.get("quoteToken") or {}

            token_address = str(base.get("address") or "").strip()
            quote_address = str(quote.get("address") or "").strip()
            pair_address = str(pair.get("pairAddress") or "").strip()
            token_symbol = str(base.get("symbol") or "UNKNOWN").strip()
            quote_symbol = str(quote.get("symbol") or "").strip()
            token_name = str(base.get("name") or token_symbol).strip()
            dex_id = str(pair.get("dexId") or "unknown").strip()
            chain_id = str(pair.get("chainId") or "").strip().lower()
            liquidity = float((pair.get("liquidity") or {}).get("usd") or 0.0)
            volume_5m = float((pair.get("volume") or {}).get("m5") or 0.0)
            price_usd = float(pair.get("priceUsd") or 0.0)
            momentum = float((pair.get("priceChange") or {}).get("m5") or 0.0)

            if not token_address or token_address == quote_address:
                continue
            if not pair_address:
                continue
            if token_symbol.upper() == "SOL" and quote_symbol.upper() == "SOL":
                continue
            if not liquidity or not volume_5m:
                continue
            if chain_id and chain_id != self.chain_id:
                continue
            if self.dex_allowlist and dex_id.lower() not in self.dex_allowlist:
                continue
            if token_address in seen_addresses:
                continue

            seen_addresses.add(token_address)
            tokens.append(
                CandidateToken(
                    symbol=token_symbol,
                    name=token_name,
                    address=token_address,
                    pair_address=pair_address,
                    dex_id=dex_id,
                    chain_id=chain_id or self.chain_id,
                    price_usd=price_usd,
                    liquidity_usd=liquidity,
                    volume_5m_usd=volume_5m,
                    momentum_score=momentum,
                    last_update_ts=datetime.utcnow(),
                )
            )

        return sorted(tokens, key=lambda t: t.momentum_score, reverse=True)[: self.top_n]

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
