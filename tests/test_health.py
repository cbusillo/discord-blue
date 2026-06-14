from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any
from typing import cast
from unittest.mock import patch

from aiohttp import web

from discord_blue.doodads.every_code.bridge import EveryCodeBridge
from discord_blue.health import RUNTIME_IDENTITY_ENV
from discord_blue.health import health_payload
from discord_blue.plugs.discord_plug import BlueBot
from tests.fakes_every_code import FakeBot


class HealthPayloadTests(unittest.TestCase):
    def test_payload_includes_parsed_runtime_identity(self) -> None:
        runtime_identity = {
            "schema_version": 1,
            "product": "discord-blue",
            "context": "stable",
            "instance": "discord-blue-1",
            "source_git_ref": "abc123",
        }

        with patch.dict(
            "os.environ",
            {
                RUNTIME_IDENTITY_ENV: json.dumps(runtime_identity),
                "LAUNCHPLANE_SOURCE_GIT_REF": "abc123",
                "LAUNCHPLANE_IMAGE_REFERENCE": "ghcr.io/example/discord-blue:abc123",
            },
            clear=True,
        ):
            payload = health_payload(discord_status="ok", every_code_enabled=True, active_every_code_sessions=2)

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["service"], "discord-blue")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["runtime_identity"], runtime_identity)
        self.assertEqual(payload["source_git_ref"], "abc123")
        self.assertEqual(payload["image_reference"], "ghcr.io/example/discord-blue:abc123")
        self.assertEqual(payload["components"]["discord"], {"status": "ok"})
        self.assertEqual(
            payload["components"]["every_code"],
            {"status": "ok", "enabled": True, "active_sessions": 2},
        )

    def test_payload_omits_runtime_identity_when_env_is_missing(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            payload = health_payload(discord_status="ok")

        self.assertNotIn("runtime_identity", payload)
        self.assertEqual(payload["status"], "ok")

    def test_malformed_runtime_identity_stays_parseable(self) -> None:
        with patch.dict("os.environ", {RUNTIME_IDENTITY_ENV: "{"}, clear=True):
            payload = health_payload(discord_status="ok")

        self.assertEqual(
            payload["runtime_identity"],
            {
                "schema_version": 1,
                "status": "invalid",
                "error": "malformed_json",
                "detail": "Expecting property name enclosed in double quotes",
            },
        )

    def test_non_object_runtime_identity_stays_parseable(self) -> None:
        with patch.dict("os.environ", {RUNTIME_IDENTITY_ENV: '"not-an-object"'}, clear=True):
            payload = health_payload(discord_status="ok")

        self.assertEqual(
            payload["runtime_identity"],
            {
                "schema_version": 1,
                "status": "invalid",
                "error": "expected_json_object",
            },
        )


class HealthEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_health_endpoint_returns_json_without_auth(self) -> None:
        config = SimpleNamespace(
            every_code=SimpleNamespace(
                enabled=True,
                token="shared-secret",
                listen_host="127.0.0.1",
                listen_port=8787,
            )
        )
        bridge = EveryCodeBridge(cast(BlueBot, FakeBot(cast(Any, config))))

        with patch.dict("os.environ", {}, clear=True):
            response = await bridge.handle_health(cast(web.Request, SimpleNamespace(headers={})))

        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "application/json")
        self.assertIsInstance(response.body, bytes)
        body = json.loads(cast(bytes, response.body).decode())
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["components"]["discord"], {"status": "ok"})

    async def test_bridge_health_endpoint_reports_unhealthy_when_discord_is_not_ready(self) -> None:
        config = SimpleNamespace(
            every_code=SimpleNamespace(
                enabled=True,
                token="shared-secret",
                listen_host="127.0.0.1",
                listen_port=8787,
            )
        )
        bot = FakeBot(cast(Any, config))
        bot.ready = False
        bridge = EveryCodeBridge(cast(BlueBot, bot))

        with patch.dict("os.environ", {}, clear=True):
            response = await bridge.handle_health(cast(web.Request, SimpleNamespace(headers={})))

        self.assertEqual(response.status, 503)
        self.assertIsInstance(response.body, bytes)
        body = json.loads(cast(bytes, response.body).decode())
        self.assertEqual(body["status"], "unhealthy")
        self.assertEqual(body["components"]["discord"], {"status": "unhealthy"})


if __name__ == "__main__":
    unittest.main()
