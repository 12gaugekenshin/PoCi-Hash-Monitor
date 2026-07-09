from __future__ import annotations

import math


def _network_odds(symbol: str, enabled: bool, total_ths: float, network_eh, metadata=None):
    if not enabled:
        return {"symbol": symbol, "enabled": False}
    if not network_eh or network_eh <= 0:
        return {
            "symbol": symbol,
            "enabled": True,
            "available": False,
            "message": "Live network data is unavailable. Add a manual fallback in Settings.",
        }
    network_ths = float(network_eh) * 1_000_000
    expected_per_day = total_ths / network_ths * 144

    def chance(days):
        return 1 - math.exp(-(expected_per_day * days))

    return {
        "symbol": symbol,
        "enabled": True,
        "available": True,
        "network_hashrate_eh": float(network_eh),
        "network_source": (metadata or {}).get("source", "Manual fallback"),
        "difficulty": (metadata or {}).get("difficulty"),
        "price_usd": (metadata or {}).get("price_usd"),
        "updated_at": (metadata or {}).get("updated_at"),
        "expected_blocks_per_day": expected_per_day,
        "daily_chance": chance(1),
        "weekly_chance": chance(7),
        "monthly_chance": chance(30),
        "estimated_days_to_block": 1 / expected_per_day if expected_per_day else None,
    }


def calculate_odds(statuses: list[dict], config: dict, network_snapshot=None):
    total_ths = sum(float(item.get("hashrate_ths") or 0) for item in statuses if item.get("online"))
    odds_config = config.get("odds", {})
    live = network_snapshot or {}

    def network_value(symbol, manual_key):
        item = live.get(symbol.lower(), {}) if odds_config.get("auto_network_data", True) else {}
        if item.get("available") and item.get("network_hashrate_eh"):
            return item.get("network_hashrate_eh"), item
        return odds_config.get(manual_key), None

    btc_hashrate, btc_meta = network_value("BTC", "manual_btc_network_hashrate_eh")
    bch_hashrate, bch_meta = network_value("BCH", "manual_bch_network_hashrate_eh")
    return {
        "total_hashrate_ths": total_ths,
        "btc": _network_odds(
            "BTC",
            odds_config.get("btc_enabled", True),
            total_ths,
            btc_hashrate,
            btc_meta,
        ),
        "bch": _network_odds(
            "BCH",
            odds_config.get("bch_enabled", True),
            total_ths,
            bch_hashrate,
            bch_meta,
        ),
    }
