from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from datetime import datetime, timezone


MAX_NETWORK_RESPONSE_BYTES = 512 * 1024
REFRESH_SECONDS = 600


def _read_json(url: str, timeout: float = 8.0):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "PoCiSys-Hash-Monitor/1.3"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_NETWORK_RESPONSE_BYTES + 1)
    if len(body) > MAX_NETWORK_RESPONSE_BYTES:
        raise ValueError("Network data response exceeded the 512 KB safety limit")
    return json.loads(body.decode("utf-8", errors="replace"))


class NetworkDataService:
    """Keeps one current BTC/BCH network snapshot; no history is retained."""

    def __init__(self):
        self.latest = {
            "btc": {"available": False},
            "bch": {"available": False},
            "updated_at": None,
        }
        self.running = False
        self.task = None

    def snapshot(self):
        return {
            "btc": dict(self.latest.get("btc", {})),
            "bch": dict(self.latest.get("bch", {})),
            "updated_at": self.latest.get("updated_at"),
        }

    def _fetch(self):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        previous = self.latest
        result = {"btc": {"available": False}, "bch": {"available": False}, "updated_at": now}

        try:
            mining = _read_json("https://mempool.space/api/v1/mining/hashrate/3d")
            prices = _read_json("https://mempool.space/api/v1/prices")
            hashrate_hs = float(mining.get("currentHashrate") or 0)
            result["btc"] = {
                "available": hashrate_hs > 0,
                "network_hashrate_eh": hashrate_hs / 1e18 if hashrate_hs else None,
                "difficulty": mining.get("currentDifficulty"),
                "price_usd": prices.get("USD"),
                "source": "mempool.space",
                "updated_at": now,
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            result["btc"] = {**previous.get("btc", {}), "error": str(exc)[:240]}

        try:
            payload = _read_json("https://api.blockchair.com/bitcoin-cash/stats")
            stats = payload.get("data", {}) if isinstance(payload, dict) else {}
            hashrate_hs = float(stats.get("hashrate_24h") or 0)
            result["bch"] = {
                "available": hashrate_hs > 0,
                "network_hashrate_eh": hashrate_hs / 1e18 if hashrate_hs else None,
                "difficulty": stats.get("difficulty"),
                "price_usd": stats.get("market_price_usd"),
                "source": "Blockchair",
                "updated_at": now,
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            result["bch"] = {**previous.get("bch", {}), "error": str(exc)[:240]}

        self.latest = result

    async def refresh(self):
        await asyncio.to_thread(self._fetch)
        return self.snapshot()

    async def run(self):
        self.running = True
        while self.running:
            started = time.monotonic()
            await self.refresh()
            delay = max(5, REFRESH_SECONDS - (time.monotonic() - started))
            await asyncio.sleep(delay)

    def start(self):
        if not self.task:
            self.task = asyncio.create_task(self.run())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
