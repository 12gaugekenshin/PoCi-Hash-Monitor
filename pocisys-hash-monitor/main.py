from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import shutil
import threading
import urllib.parse
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from miners import get_driver
from services.alerts import AlertEngine
from services.config_store import apply_in_place, load_config, make_id, public_config, save_config
from services.network_data import NetworkDataService
from services.odds import calculate_odds
from services.poller import MinerPoller
from services.pool_logs import PoolLogService, probe_public_pool


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
CONFIG_PATH = Path(os.environ.get("POCISYS_CONFIG_PATH", ROOT / "config.json")).resolve()
DEFAULT_CONFIG_PATH = ROOT / "config.default.json"
HOST_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")
MAX_REQUEST_BYTES = 1024 * 1024
MINER_TYPES = {"axeos", "bitaxe", "nerdaxe", "nerdqaxe", "luxos"}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def as_float(value, default=None):
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ApiError(400, "Enter a valid number")


def as_int(value, default=0):
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "Enter a valid whole number")


def clean_host(value):
    host = str(value or "").strip()
    for prefix in ("http://", "https://"):
        if host.lower().startswith(prefix):
            host = host[len(prefix):]
    host = host.rstrip("/")
    if not host or "/" in host or not HOST_PATTERN.fullmatch(host):
        raise ApiError(400, "Use an IP address or hostname without a URL path")
    return host


def clean_miner(data):
    name = str(data.get("name") or "").strip()
    miner_type = str(data.get("type") or "").lower()
    if not name or len(name) > 80:
        raise ApiError(400, "Miner name is required")
    if miner_type not in MINER_TYPES:
        raise ApiError(400, "Unsupported miner API type")
    warning = as_float(data.get("temp_warning_c"), 70)
    critical = as_float(data.get("temp_critical_c"), 80)
    if warning is not None and not 0 <= warning <= 150:
        raise ApiError(400, "Temperature warning must be between 0 and 150")
    if critical is not None and not 0 <= critical <= 150:
        raise ApiError(400, "Critical temperature must be between 0 and 150")
    if warning is not None and critical is not None and critical < warning:
        raise ApiError(400, "Critical temperature must be at least the warning temperature")
    minimum = as_float(data.get("min_hashrate_ths"), None)
    if minimum is not None and minimum < 0:
        raise ApiError(400, "Minimum hashrate cannot be negative")
    return {
        "name": name,
        "ip": clean_host(data.get("ip")),
        "type": miner_type,
        "group": str(data.get("group") or "Ungrouped").strip()[:80] or "Ungrouped",
        "enabled": bool(data.get("enabled", True)),
        "display_order": max(0, min(9999, as_int(data.get("display_order"), 0))),
        "min_hashrate_ths": minimum,
        "temp_warning_c": warning,
        "temp_critical_c": critical,
    }


def clean_pool(data):
    name = str(data.get("name") or "").strip()
    mode = str(data.get("mode") or "public_pool_api")
    if not name or len(name) > 80:
        raise ApiError(400, "Pool monitor name is required")
    if mode not in {"public_pool_api", "local_log"}:
        raise ApiError(400, "Unsupported pool monitor type")
    api_url = str(data.get("api_url") or "").strip().rstrip("/")
    log_path = str(data.get("log_path") or "").strip()
    if mode == "public_pool_api":
        parsed = urllib.parse.urlparse(api_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ApiError(400, "Enter a valid Public Pool API URL")
    elif not log_path:
        raise ApiError(400, "Add a local pool log path")
    return {
        "name": name,
        "type": str(data.get("type") or ("public_pool" if mode == "public_pool_api" else "ckpool"))[:40],
        "mode": mode,
        "enabled": bool(data.get("enabled", True)),
        "log_path": log_path[:1024],
        "api_url": api_url[:1024],
        "bitcoin_address": str(data.get("bitcoin_address") or "").strip()[:160],
    }


if not CONFIG_PATH.exists():
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DEFAULT_CONFIG_PATH, CONFIG_PATH)

config = load_config(CONFIG_PATH)
alerts = AlertEngine(config)
poller = MinerPoller(config, alerts)
pool_logs = PoolLogService(config.get("pools", []), alerts, poller.statuses)
network_data = NetworkDataService()
config_lock = asyncio.Lock()


def commit_config(updated):
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


def current_settings():
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


async def discover_public_pool(host=None):
    hosts = []
    if host:
        hosts.append(clean_host(host))
    for status in poller.statuses():
        pool_url = str(status.get("pool", {}).get("url") or "")
        parsed = urllib.parse.urlparse(pool_url if "://" in pool_url else f"stratum://{pool_url}")
        if parsed.hostname and parsed.hostname not in hosts:
            hosts.append(parsed.hostname)
    attempts = []
    for candidate_host in hosts[:20]:
        for port in (2019, 3334, 40557):
            candidate = f"http://{candidate_host}:{port}"
            attempts.append(candidate)
            try:
                api_url, response = await asyncio.to_thread(probe_public_pool, candidate)
                return {
                    "ok": True,
                    "api_url": api_url,
                    "host": candidate_host,
                    "total_miners": response.get("totalMiners"),
                    "block_height": response.get("blockHeight"),
                }
            except (OSError, ValueError):
                continue
    return {"ok": False, "error": "Public Pool API not found", "attempted": attempts}


async def api_dispatch(method, path, data):
    parts = [urllib.parse.unquote(item) for item in path.strip("/").split("/") if item]
    statuses = poller.statuses()

    if method == "GET" and path == "/health":
        return {"ok": True, "version": "1.3.3"}
    if method == "GET" and path == "/api/status":
        return {
            "summary": summary(),
            "miners": statuses,
            "odds": calculate_odds(statuses, config, network_data.snapshot()),
            "discord": alerts.status(),
            "pools": pool_logs.status(),
            "pool_event_count": len(pool_logs.events),
            "ui": {"dashboard_density": config.get("app", {}).get("dashboard_density", "comfortable")},
        }
    if method == "GET" and path == "/api/miners":
        current = {item.get("id"): item for item in statuses}
        return {
            "miners": [
                {"config": deepcopy(miner), "status": current.get(miner.get("id"))}
                for miner in sorted(config.get("miners", []), key=lambda item: item.get("display_order", 0))
            ]
        }
    if method == "POST" and path == "/api/miners/test":
        candidate = clean_miner({
            "name": "Connection test",
            "ip": data.get("ip"),
            "type": data.get("type"),
            "group": "Test",
        })
        timeout = config.get("app", {}).get("request_timeout_seconds", 4)
        try:
            status = await asyncio.to_thread(get_driver(candidate, timeout).poll)
            status.pop("raw", None)
            return {"ok": bool(status.get("api_ok")), "status": status}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    if method == "POST" and path == "/api/miners":
        async with config_lock:
            updated = deepcopy(config)
            miner = clean_miner(data)
            miner["id"] = make_id("miner")
            if not miner["display_order"]:
                miner["display_order"] = len(updated["miners"]) + 1
            updated["miners"].append(miner)
            commit_config(updated)
        return {"ok": True, "miner": miner}
    if method == "POST" and path == "/api/miners/reorder":
        ids = data.get("ids") if isinstance(data.get("ids"), list) else []
        async with config_lock:
            updated = deepcopy(config)
            current_ids = {item["id"] for item in updated["miners"]}
            if len(ids) != len(current_ids) or set(ids) != current_ids:
                raise ApiError(400, "Reorder list must contain every configured miner exactly once")
            order = {miner_id: index + 1 for index, miner_id in enumerate(ids)}
            for miner in updated["miners"]:
                miner["display_order"] = order[miner["id"]]
            updated["miners"].sort(key=lambda item: item["display_order"])
            commit_config(updated)
        return {"ok": True}
    if len(parts) == 3 and parts[:2] == ["api", "miners"]:
        miner_id = parts[2]
        existing = next((item for item in config.get("miners", []) if item.get("id") == miner_id), None)
        if not existing:
            raise ApiError(404, "Miner not found")
        if method == "GET":
            status = next((item for item in statuses if item.get("id") == miner_id), None)
            return {"config": deepcopy(existing), "status": status}
        if method == "PUT":
            async with config_lock:
                updated = deepcopy(config)
                index = next(i for i, item in enumerate(updated["miners"]) if item.get("id") == miner_id)
                miner = clean_miner(data)
                miner["id"] = miner_id
                updated["miners"][index] = miner
                commit_config(updated)
            return {"ok": True, "miner": miner}
        if method == "DELETE":
            async with config_lock:
                updated = deepcopy(config)
                updated["miners"] = [item for item in updated["miners"] if item.get("id") != miner_id]
                for index, miner in enumerate(updated["miners"], 1):
                    miner["display_order"] = index
                commit_config(updated)
            return {"ok": True}

    if method == "GET" and path == "/api/pools":
        live = {item["name"]: item for item in pool_logs.status()}
        return {
            "pools": [
                {"config": deepcopy(pool), "status": live.get(pool.get("name"))}
                for pool in config.get("pools", [])
            ]
        }
    if method == "POST" and path == "/api/pools/discover":
        return await discover_public_pool(data.get("host"))
    if method == "POST" and path == "/api/pools":
        async with config_lock:
            updated = deepcopy(config)
            pool = clean_pool(data)
            pool["id"] = make_id("pool")
            updated["pools"].append(pool)
            commit_config(updated)
        return {"ok": True, "pool": pool}
    if len(parts) == 3 and parts[:2] == ["api", "pools"]:
        pool_id = parts[2]
        existing = next((item for item in config.get("pools", []) if item.get("id") == pool_id), None)
        if not existing:
            raise ApiError(404, "Pool not found")
        if method == "PUT":
            async with config_lock:
                updated = deepcopy(config)
                index = next(i for i, item in enumerate(updated["pools"]) if item.get("id") == pool_id)
                pool = clean_pool(data)
                pool["id"] = pool_id
                updated["pools"][index] = pool
                commit_config(updated)
            return {"ok": True, "pool": pool}
        if method == "DELETE":
            async with config_lock:
                updated = deepcopy(config)
                updated["pools"] = [item for item in updated["pools"] if item.get("id") != pool_id]
                commit_config(updated)
            return {"ok": True}
    if method == "GET" and path == "/api/pool-events":
        return {"events": list(pool_logs.events)}
    if method == "GET" and path == "/api/odds":
        return calculate_odds(statuses, config, network_data.snapshot())
    if method == "GET" and path == "/api/network-data":
        return network_data.snapshot()
    if method == "GET" and path == "/api/config":
        return public_config(config)
    if method == "GET" and path == "/api/settings":
        return current_settings()
    if method == "PUT" and path == "/api/settings":
        async with config_lock:
            updated = deepcopy(config)
            app_config = updated["app"]
            old_port = app_config.get("dashboard_port", 8765)
            old_lan = app_config.get("lan_access_enabled", False)
            density = str(data.get("dashboard_density") or "comfortable")
            if density not in {"comfortable", "compact"}:
                raise ApiError(400, "Unsupported dashboard density")
            dashboard_url = str(data.get("dashboard_base_url") or "").strip().rstrip("/")
            if dashboard_url and not dashboard_url.startswith(("http://", "https://")):
                raise ApiError(400, "Dashboard link must begin with http:// or https://")
            app_config.update(
                poll_interval_seconds=max(2, min(3600, as_int(data.get("poll_interval_seconds"), 10))),
                dashboard_port=max(1024, min(65535, as_int(data.get("dashboard_port"), 8765))),
                alert_cooldown_seconds=max(0, min(86400, as_int(data.get("alert_cooldown_seconds"), 600))),
                request_timeout_seconds=max(0.5, min(30, as_float(data.get("request_timeout_seconds"), 4))),
                dashboard_density=density,
                dashboard_base_url=dashboard_url,
                lan_access_enabled=bool(data.get("lan_access_enabled", False)),
            )
            discord = updated["discord"]
            webhook = str(data.get("webhook_url") or "").strip()
            allowed = (
                "https://discord.com/api/webhooks/",
                "https://discordapp.com/api/webhooks/",
                "https://canary.discord.com/api/webhooks/",
            )
            if webhook and not webhook.startswith(allowed):
                raise ApiError(400, "Enter a Discord webhook URL")
            discord.update(
                enabled=bool(data.get("discord_enabled", False)),
                send_offline_alerts=bool(data.get("send_offline_alerts", True)),
                send_recovery_alerts=bool(data.get("send_recovery_alerts", True)),
                send_hashrate_alerts=bool(data.get("send_hashrate_alerts", True)),
                send_temperature_alerts=bool(data.get("send_temperature_alerts", True)),
                send_best_diff_alerts=bool(data.get("send_best_diff_alerts", True)),
                send_block_found_alerts=bool(data.get("send_block_found_alerts", True)),
                send_pool_alerts=bool(data.get("send_pool_alerts", True)),
                send_pool_switch_alerts=bool(data.get("send_pool_switch_alerts", True)),
                send_share_alerts=bool(data.get("send_share_alerts", True)),
                verbose_pool_events=bool(data.get("verbose_pool_events", False)),
            )
            if data.get("clear_webhook"):
                discord["webhook_url"] = ""
            elif webhook:
                discord["webhook_url"] = webhook
            if discord["enabled"] and not discord.get("webhook_url"):
                raise ApiError(400, "Add a Discord webhook URL before enabling Discord")
            updated["odds"].update(
                btc_enabled=bool(data.get("btc_enabled", True)),
                bch_enabled=bool(data.get("bch_enabled", True)),
                auto_network_data=bool(data.get("auto_network_data", True)),
                manual_btc_network_hashrate_eh=as_float(data.get("manual_btc_network_hashrate_eh"), None),
                manual_bch_network_hashrate_eh=as_float(data.get("manual_bch_network_hashrate_eh"), None),
            )
            commit_config(updated)
        return {
            "ok": True,
            "restart_required": old_port != app_config["dashboard_port"] or old_lan != app_config["lan_access_enabled"],
        }
    if method == "GET" and path == "/api/alerts":
        return alerts.status()
    if method == "POST" and path == "/api/test-discord":
        return await alerts.test_discord()
    if method == "POST" and path == "/api/poll-now":
        await poller.poll_now()
        return {"ok": True, "summary": summary()}
    raise ApiError(404, "API route not found")


event_loop = asyncio.new_event_loop()
loop_started = threading.Event()


def run_event_loop():
    asyncio.set_event_loop(event_loop)

    async def boot():
        poller.start()
        pool_logs.start()
        network_data.start()

    event_loop.run_until_complete(boot())
    loop_started.set()
    event_loop.run_forever()


def run_api(method, path, data=None):
    future = asyncio.run_coroutine_threadsafe(api_dispatch(method, path, data or {}), event_loop)
    return future.result(timeout=90)


class PoCiSysHandler(BaseHTTPRequestHandler):
    server_version = "PoCiSys/1.3.3"

    def log_message(self, _format, *_args):
        return

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(400, "Invalid request length")
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ApiError(413, "Request body is too large")
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "Request body must be valid JSON")
        if not isinstance(value, dict):
            raise ApiError(400, "Request body must be a JSON object")
        return value

    def serve_frontend(self, path):
        if path.startswith("/static/"):
            relative = path[len("/static/"):]
            candidate = (WEB_ROOT / relative).resolve()
            if not str(candidate).startswith(str(WEB_ROOT.resolve())) or not candidate.is_file():
                self.send_error(404)
                return
        else:
            candidate = WEB_ROOT / "index.html"
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def dispatch(self, method):
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        try:
            if not path.startswith("/api/") and path != "/health":
                if method != "GET":
                    raise ApiError(405, "Method not allowed")
                self.serve_frontend(path)
                return
            data = self.read_json() if method in {"POST", "PUT"} else {}
            self.send_json(run_api(method, path, data))
        except ApiError as exc:
            self.send_json({"detail": exc.message}, exc.status)
        except TimeoutError:
            self.send_json({"detail": "Request timed out"}, 504)
        except Exception as exc:
            self.send_json({"detail": f"Internal error: {exc}"}, 500)

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")


class PoCiSysServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def shutdown_services():
    async def stop():
        await poller.stop()
        await pool_logs.stop()
        await network_data.stop()

    try:
        asyncio.run_coroutine_threadsafe(stop(), event_loop).result(timeout=10)
    finally:
        event_loop.call_soon_threadsafe(event_loop.stop)


if __name__ == "__main__":
    print("PoCiSys Hash Monitor 1.3.3 starting", flush=True)
    print(f"Config path: {CONFIG_PATH}", flush=True)
    thread = threading.Thread(target=run_event_loop, name="pocisys-services", daemon=True)
    thread.start()
    loop_started.wait(10)
    host = os.environ.get("POCISYS_HOST", "0.0.0.0")
    port = int(os.environ.get("POCISYS_PORT", "8765"))
    server = PoCiSysServer((host, port), PoCiSysHandler)
    print(f"PoCiSys Hash Monitor ready on {host}:{port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        shutdown_services()
