from __future__ import annotations

import json
import unittest

from services.diagnostics import build_diagnostics


class DiagnosticsTests(unittest.TestCase):
    def test_report_is_sanitized_and_bounded(self):
        report = build_diagnostics(
            app_version="test",
            config={
                "app": {},
                "miners": [{"name": "Loki", "ip": "192.168.1.2"}],
                "pools": [{"api_url": "http://192.168.1.213:2020", "bitcoin_address": "bc1secret"}],
                "discord": {"webhook_url": "https://discord.com/api/webhooks/id/secret"},
                "odds": {},
                "hermes": {"token_hash": "digest", "token_hint": "123456"},
            },
            system={"hostname": "umbrel", "memory": {"used_bytes": 10}},
            summary={"online_miners": 1},
            miner_statuses=[{"ip": "192.168.1.2", "raw": {"large": "payload"}, "warning": "Pool http://10.0.0.2:3333 failed"}],
            pool_statuses=[],
            alert_status={"recent": [{"message": str(index)} for index in range(40)]},
            pool_events=[{"message": str(index)} for index in range(80)],
            health_status={"transitions": [{"state": index} for index in range(70)]},
            control_status={"recent_actions": [{"action": index} for index in range(40)]},
        )
        serialized = json.dumps(report)
        for secret in ("192.168.1.2", "192.168.1.213", "10.0.0.2", "bc1secret", "api/webhooks", "digest", "123456", "payload"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(len(report["recent"]["alerts"]["recent"]), 25)
        self.assertEqual(len(report["recent"]["pool_events"]), 50)
        self.assertEqual(len(report["recent"]["health"]["transitions"]), 50)
        self.assertEqual(len(report["recent"]["control"]["recent_actions"]), 25)


if __name__ == "__main__":
    unittest.main()
