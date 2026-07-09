from __future__ import annotations

import asyncio
import json
import urllib.request


class DiscordWebhook:
    def __init__(self, config: dict):
        self.config = config

    @property
    def ready(self):
        return bool(self.config.get("enabled") and self.config.get("webhook_url"))

    def _post(self, title: str, message: str, color: int, url: str | None):
        embed = {"title": title, "description": message, "color": color}
        if url:
            embed["url"] = url
            embed["footer"] = {"text": "Open PoCiSys Hash Monitor"}
        payload = {
            "username": "PoCiSys Hash Monitor",
            "embeds": [embed],
        }
        request = urllib.request.Request(
            self.config["webhook_url"],
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "PoCiSys-Hash-Monitor/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status

    async def send(self, title: str, message: str, severity="warning", url=None):
        if not self.ready:
            return {"sent": False, "reason": "Discord is disabled or no webhook URL is configured."}
        colors = {"info": 0x3498DB, "warning": 0xF1C40F, "critical": 0xE74C3C, "success": 0x2ECC71}
        try:
            status = await asyncio.to_thread(
                self._post, title, message, colors.get(severity, 0xF1C40F), url
            )
            return {"sent": status in (200, 204), "status": status}
        except Exception as exc:
            return {"sent": False, "reason": str(exc)}
