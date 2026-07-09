from __future__ import annotations

import asyncio
import os
import re
import shutil
import urllib.parse
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from miners import get_driver
from services.alerts import AlertEngine
from services.config_store import apply_in_place, load_config, make_id, public_config, save_config
from services.network_data import NetworkDataService
from services.odds import calculate_odds
from services.poller import MinerPoller
from services.pool_logs import PoolLogService, probe_public_pool

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("POCISYS_CONFIG_PATH", ROOT / "config.json")).resolve()
DEFAULT_CONFIG_PATH = ROOT / "config.default.json"
HOST_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")

if not CONFIG_PATH.exists():
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DEFAULT_CONFIG_PATH, CONFIG_PATH)


class MinerPayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    ip: str = Field(min_length=1, max_length=255)
    type: Literal["axeos", "bitaxe", "nerdaxe", "nerdqaxe", "luxos"]
    group: str = Field(default="Ungrouped", max_length=80)
    enabled: bool = True
    display_order: int = Field(default=0, ge=0, le=9999)
    min_hashrate_ths: float | None = Field(default=None, ge=0, le=10_000_000)
    temp_warning_c: float | None = Field(default=70, ge=0, le=150)
    temp_critical_c: float | None = Field(default=80, ge=0, le=150)

    @field_validator("name", "group")
    @classmethod
    def trim_text(cls, value):
        return value.strip()

    @field_validator("ip")
    @classmethod
    def validate_host(cls, value):
        host = value.strip()
        for prefix in ("http://", "https://"):
            if host.lower().startswith(prefix):
                host = host[len(prefix):]
        host = host.rstrip("/")
        if "/" in host or not HOST_PATTERN.fullmatch(host):
            raise ValueError("Use an IP address or hostname without a URL path")
        return host

    @field_validator("temp_critical_c")
    @classmethod
    def validate_critical(cls, value, info):
        warning = info.data.get("temp_warning_c")
        if value is not None and warning is not None and value < warning:
            raise ValueError("Critical temperature must be at least the warning temperature")
        return value


class MinerTestPayload(BaseModel):
    ip: str
    type: Literal["axeos", "bitaxe", "nerdaxe", "nerdqaxe", "luxos"]

    @field_validator("ip")
    @classmethod
    def validate_host(cls, value):
        return MinerPayload.validate_host(value)


class ReorderPayload(BaseModel):
    ids: list[str]


class PoolPayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    type: str = Field(default="public_pool", min_length=1, max_length=40)
    mode: Literal["local_log", "public_pool_api"] = "public_pool_api"
    enabled: bool = True
    log_path: str = Field(default="", max_length=1024)
    api_url: str = Field(default="", max_length=1024)
    bitcoin_address: str = Field(default="", max_length=160)

    @field_validator("name", "type", "log_path", "api_url", "bitcoin_address")
    @classmethod
    def trim_text(cls, value):
        return value.strip()

    @model_validator(mode="after")
    def validate_source(self):
        if self.mode == "local_log" and not self.log_path:
            raise ValueError("Add a local pool log path")
        if self.mode == "public_pool_api":
            if not self.api_url.startswith(("http://", "https://")):
                raise ValueError("Public Pool API URL must begin with http:// or https://")
            parsed = urllib.parse.urlparse(self.api_url)
            if not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Enter a valid Public Pool API URL")
            self.api_url = self.api_url.rstrip("/")
        return self


class PublicPoolDetectPayload(BaseModel):
    host: str | None = Field(default=None, max_length=255)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value):
        if not value:
            return None
        return MinerPayload.validate_host(value)


class SettingsPayload(BaseModel):
    poll_interval_seconds: int = Field(default=10, ge=2, le=3600)
    dashboard_port: int = Field(default=8765, ge=1024, le=65535)
    alert_cooldown_seconds: int = Field(default=600, ge=0, le=86400)
    request_timeout_seconds: float = Field(default=4, ge=0.5, le=30)
    dashboard_density: Literal["comfortable", "compact"] = "comfortable"
    dashboard_base_url: str | None = None
    lan_access_enabled: bool = False
    discord_enabled: bool = False
    webhook_url: str | None = None
    clear_webhook: bool = False
    send_offline_alerts: bool = True
    send_recovery_alerts: bool = True
    send_hashrate_alerts: bool = True
    send_temperature_alerts: bool = True
    send_best_diff_alerts: bool = True
    send_block_found_alerts: bool = True
    send_pool_alerts: bool = True
    send_pool_switch_alerts: bool = True
    send_share_alerts: bool = True
    verbose_pool_events: bool = False
    btc_enabled: bool = True
    bch_enabled: bool = True
    auto_network_data: bool = True
    manual_btc_network_hashrate_eh: float | None = Field(default=None, gt=0)
    manual_bch_network_hashrate_eh: float | None = Field(default=None, gt=0)

    @field_validator("webhook_url")
    @classmethod
    def validate_webhook(cls, value):
        if not value:
            return value
        allowed = (
            "https://discord.com/api/webhooks/",
            "https://discordapp.com/api/webhooks/",
            "https://canary.discord.com/api/webhooks/",
        )
        if not value.startswith(allowed):
            raise ValueError("Enter a Discord webhook URL")
        return value.strip()

    @field_validator("dashboard_base_url")
    @classmethod
    def validate_dashboard_url(cls, value):
        if not value:
            return value
        cleaned = value.strip().rstrip("/")
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError("Dashboard link must begin with http:// or https://")
        return cleaned


config = load_config(CONFIG_PATH)
alerts = AlertEngine(config)
poller = MinerPoller(config, alerts)
pool_logs = PoolLogService(config.get("pools", []), alerts, poller.statuses)
network_data = NetworkDataService()
config_lock = asyncio.Lock()


def commit_config(updated: dict):
    save_config(CONFIG_PATH, updated)
    apply_in_place(config, updated)
    alerts.reconfigure()
    pool_logs.reconfigure(config["pools"])
    poller.reconfigure()


def summary():
    statuses = poller.statuses()
    online = [item for item in statuses if item.get("online") and item.get("api_ok")]
    temperatures = [
        temp
        for item in statuses
        for temp in item.get("temps", {}).values()
        if temp is not None
    ]
    pings = [item["ping_ms"] for item in online if item.get("ping_ms") is not None]
    enabled_count = sum(1 for item in config.get("miners", []) if item.get("enabled", True))
    return {
        "total_hashrate_ths": sum(float(item.get("hashrate_ths") or 0) for item in online),
        "online_miners": len(online),
        "total_miners": enabled_count,
        "configured_miners": len(config.get("miners", [])),
        "average_ping_ms": round(sum(pings) / len(pings), 1) if pings else None,
        "highest_temperature_c": max(temperatures, default=None),
        "total_valid_shares": sum(item.get("shares", {}).get("valid", 0) for item in statuses),
        "total_bad_shares": sum(
            item.get("shares", {}).get(key, 0)
            for item in statuses
            for key in ("invalid", "stale", "rejected")
        ),
        "last_poll": poller.last_poll,
    }


@asynccontextmanager
async def lifespan(_app):
    poller.start()
    pool_logs.start()
    network_data.start()
    yield
    await poller.stop()
    await pool_logs.stop()
    await network_data.stop()


app = FastAPI(title="PoCiSys Hash Monitor", version="1.3.1", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")


@app.get("/api/status")
async def api_status():
    statuses = poller.statuses()
    return {
        "summary": summary(),
        "miners": statuses,
        "odds": calculate_odds(statuses, config, network_data.snapshot()),
        "discord": alerts.status(),
        "pools": pool_logs.status(),
        "pool_event_count": len(pool_logs.events),
        "ui": {"dashboard_density": config.get("app", {}).get("dashboard_density", "comfortable")},
    }


@app.get("/api/miners")
async def api_miners():
    current = {item.get("id"): item for item in poller.statuses()}
    return {
        "miners": [
            {"config": deepcopy(miner), "status": current.get(miner.get("id"))}
            for miner in sorted(config.get("miners", []), key=lambda item: item.get("display_order", 0))
        ]
    }


@app.post("/api/miners/test")
async def api_test_miner(payload: MinerTestPayload):
    miner = {"name": "Connection test", "ip": payload.ip, "type": payload.type, "group": "Test"}
    timeout = config.get("app", {}).get("request_timeout_seconds", 4)
    try:
        status = await asyncio.to_thread(get_driver(miner, timeout).poll)
        status.pop("raw", None)
        return {"ok": bool(status.get("api_ok")), "status": status}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/miners")
async def api_add_miner(payload: MinerPayload):
    async with config_lock:
        updated = deepcopy(config)
        miner = payload.model_dump()
        miner["id"] = make_id("miner")
        if not miner["display_order"]:
            miner["display_order"] = len(updated["miners"]) + 1
        updated["miners"].append(miner)
        commit_config(updated)
    return {"ok": True, "miner": miner}


@app.post("/api/miners/reorder")
async def api_reorder_miners(payload: ReorderPayload):
    async with config_lock:
        updated = deepcopy(config)
        current_ids = {item["id"] for item in updated["miners"]}
        if len(payload.ids) != len(current_ids) or set(payload.ids) != current_ids:
            raise HTTPException(400, "Reorder list must contain every configured miner exactly once")
        order = {miner_id: index + 1 for index, miner_id in enumerate(payload.ids)}
        for miner in updated["miners"]:
            miner["display_order"] = order[miner["id"]]
        updated["miners"].sort(key=lambda item: item["display_order"])
        commit_config(updated)
    return {"ok": True}


@app.get("/api/miners/{miner_id}")
async def api_get_miner(miner_id: str):
    miner = next((item for item in config.get("miners", []) if item.get("id") == miner_id), None)
    if not miner:
        raise HTTPException(404, "Miner not found")
    status = next((item for item in poller.statuses() if item.get("id") == miner_id), None)
    return {"config": deepcopy(miner), "status": status}


@app.put("/api/miners/{miner_id}")
async def api_update_miner(miner_id: str, payload: MinerPayload):
    async with config_lock:
        updated = deepcopy(config)
        index = next((i for i, item in enumerate(updated["miners"]) if item.get("id") == miner_id), None)
        if index is None:
            raise HTTPException(404, "Miner not found")
        miner = payload.model_dump()
        miner["id"] = miner_id
        updated["miners"][index] = miner
        commit_config(updated)
    return {"ok": True, "miner": miner}


@app.delete("/api/miners/{miner_id}")
async def api_delete_miner(miner_id: str):
    async with config_lock:
        updated = deepcopy(config)
        before = len(updated["miners"])
        updated["miners"] = [item for item in updated["miners"] if item.get("id") != miner_id]
        if len(updated["miners"]) == before:
            raise HTTPException(404, "Miner not found")
        for index, miner in enumerate(updated["miners"], 1):
            miner["display_order"] = index
        commit_config(updated)
    return {"ok": True}


@app.get("/api/pools")
async def api_pools():
    statuses = {item["name"]: item for item in pool_logs.status()}
    return {
        "pools": [
            {"config": deepcopy(pool), "status": statuses.get(pool.get("name"))}
            for pool in config.get("pools", [])
        ]
    }


@app.post("/api/pools")
async def api_add_pool(payload: PoolPayload):
    async with config_lock:
        updated = deepcopy(config)
        pool = payload.model_dump()
        pool["id"] = make_id("pool")
        updated["pools"].append(pool)
        commit_config(updated)
    return {"ok": True, "pool": pool}


@app.post("/api/pools/discover")
async def api_discover_public_pool(payload: PublicPoolDetectPayload):
    hosts = []
    if payload.host:
        hosts.append(payload.host)
    for status in poller.statuses():
        pool_url = str(status.get("pool", {}).get("url") or "")
        parsed = urllib.parse.urlparse(pool_url if "://" in pool_url else f"stratum://{pool_url}")
        if parsed.hostname and parsed.hostname not in hosts:
            hosts.append(parsed.hostname)
    attempts = []
    for host in hosts[:20]:
        for port in (2019, 3334, 40557):
            candidate = f"http://{host}:{port}"
            attempts.append(candidate)
            try:
                api_url, response = await asyncio.to_thread(probe_public_pool, candidate)
                return {
                    "ok": True,
                    "api_url": api_url,
                    "host": host,
                    "total_miners": response.get("totalMiners"),
                    "block_height": response.get("blockHeight"),
                }
            except (OSError, ValueError):
                continue
    return {"ok": False, "error": "Public Pool API not found", "attempted": attempts}


@app.put("/api/pools/{pool_id}")
async def api_update_pool(pool_id: str, payload: PoolPayload):
    async with config_lock:
        updated = deepcopy(config)
        index = next((i for i, item in enumerate(updated["pools"]) if item.get("id") == pool_id), None)
        if index is None:
            raise HTTPException(404, "Pool not found")
        pool = payload.model_dump()
        pool["id"] = pool_id
        updated["pools"][index] = pool
        commit_config(updated)
    return {"ok": True, "pool": pool}


@app.delete("/api/pools/{pool_id}")
async def api_delete_pool(pool_id: str):
    async with config_lock:
        updated = deepcopy(config)
        before = len(updated["pools"])
        updated["pools"] = [item for item in updated["pools"] if item.get("id") != pool_id]
        if len(updated["pools"]) == before:
            raise HTTPException(404, "Pool not found")
        commit_config(updated)
    return {"ok": True}


@app.get("/api/pool-events")
async def api_pool_events():
    return {"events": list(pool_logs.events)}


@app.get("/api/odds")
async def api_odds():
    return calculate_odds(poller.statuses(), config, network_data.snapshot())


@app.get("/api/network-data")
async def api_network_data():
    return network_data.snapshot()


@app.get("/api/config")
async def api_config():
    return public_config(config)


@app.get("/api/settings")
async def api_settings():
    app_config = config.get("app", {})
    discord = config.get("discord", {})
    odds = config.get("odds", {})
    return {
        "poll_interval_seconds": app_config.get("poll_interval_seconds", 10),
        "dashboard_port": app_config.get("dashboard_port", 8765),
        "alert_cooldown_seconds": app_config.get("alert_cooldown_seconds", 600),
        "request_timeout_seconds": app_config.get("request_timeout_seconds", 4),
        "dashboard_density": app_config.get("dashboard_density", "comfortable"),
        "dashboard_base_url": app_config.get("dashboard_base_url", ""),
        "lan_access_enabled": app_config.get("lan_access_enabled", False),
        "discord_enabled": discord.get("enabled", False),
        "webhook_configured": bool(discord.get("webhook_url")),
        "send_offline_alerts": discord.get("send_offline_alerts", True),
        "send_recovery_alerts": discord.get("send_recovery_alerts", True),
        "send_hashrate_alerts": discord.get("send_hashrate_alerts", True),
        "send_temperature_alerts": discord.get("send_temperature_alerts", True),
        "send_best_diff_alerts": discord.get("send_best_diff_alerts", True),
        "send_block_found_alerts": discord.get("send_block_found_alerts", True),
        "send_pool_alerts": discord.get("send_pool_alerts", True),
        "send_pool_switch_alerts": discord.get("send_pool_switch_alerts", True),
        "send_share_alerts": discord.get("send_share_alerts", True),
        "verbose_pool_events": discord.get("verbose_pool_events", False),
        "btc_enabled": odds.get("btc_enabled", True),
        "bch_enabled": odds.get("bch_enabled", True),
        "auto_network_data": odds.get("auto_network_data", True),
        "manual_btc_network_hashrate_eh": odds.get("manual_btc_network_hashrate_eh"),
        "manual_bch_network_hashrate_eh": odds.get("manual_bch_network_hashrate_eh"),
    }


@app.put("/api/settings")
async def api_update_settings(payload: SettingsPayload):
    async with config_lock:
        updated = deepcopy(config)
        old_port = updated["app"].get("dashboard_port", 8765)
        old_lan_access = updated["app"].get("lan_access_enabled", False)
        updated["app"].update(
            poll_interval_seconds=payload.poll_interval_seconds,
            dashboard_port=payload.dashboard_port,
            alert_cooldown_seconds=payload.alert_cooldown_seconds,
            request_timeout_seconds=payload.request_timeout_seconds,
            dashboard_density=payload.dashboard_density,
            dashboard_base_url=payload.dashboard_base_url or "",
            lan_access_enabled=payload.lan_access_enabled,
        )
        discord = updated["discord"]
        discord.update(
            enabled=payload.discord_enabled,
            send_offline_alerts=payload.send_offline_alerts,
            send_recovery_alerts=payload.send_recovery_alerts,
            send_hashrate_alerts=payload.send_hashrate_alerts,
            send_temperature_alerts=payload.send_temperature_alerts,
            send_best_diff_alerts=payload.send_best_diff_alerts,
            send_block_found_alerts=payload.send_block_found_alerts,
            send_pool_alerts=payload.send_pool_alerts,
            send_pool_switch_alerts=payload.send_pool_switch_alerts,
            send_share_alerts=payload.send_share_alerts,
            verbose_pool_events=payload.verbose_pool_events,
        )
        if payload.clear_webhook:
            discord["webhook_url"] = ""
        elif payload.webhook_url:
            discord["webhook_url"] = payload.webhook_url
        if payload.discord_enabled and not discord.get("webhook_url"):
            raise HTTPException(400, "Add a Discord webhook URL before enabling Discord")
        updated["odds"].update(
            btc_enabled=payload.btc_enabled,
            bch_enabled=payload.bch_enabled,
            auto_network_data=payload.auto_network_data,
            manual_btc_network_hashrate_eh=payload.manual_btc_network_hashrate_eh,
            manual_bch_network_hashrate_eh=payload.manual_bch_network_hashrate_eh,
        )
        commit_config(updated)
    return {
        "ok": True,
        "restart_required": old_port != payload.dashboard_port or old_lan_access != payload.lan_access_enabled,
    }


@app.get("/api/alerts")
async def api_alerts():
    return alerts.status()


@app.post("/api/test-discord")
async def api_test_discord():
    return await alerts.test_discord()


@app.post("/api/poll-now")
async def api_poll_now():
    await poller.poll_now()
    return {"ok": True, "summary": summary()}


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/{path:path}", response_class=FileResponse)
async def frontend(path: str):
    return FileResponse(ROOT / "web" / "index.html")


if __name__ == "__main__":
    import uvicorn

    configured_host = "0.0.0.0" if config["app"].get("lan_access_enabled", False) else "127.0.0.1"
    uvicorn.run(
        "main:app",
        host=os.environ.get("POCISYS_HOST", configured_host),
        port=int(os.environ.get("POCISYS_PORT", config["app"].get("dashboard_port", 8765))),
        access_log=False,
    )
