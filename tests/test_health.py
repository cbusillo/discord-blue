from __future__ import annotations

import json
import asyncio
import unittest
from types import SimpleNamespace
from typing import Any
from typing import cast
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import make_mocked_request
from discord.gateway import DiscordWebSocket, KeepAliveHandler

from discord_blue.doodads.agent_session.bridge import AgentSessionBridge
from discord_blue.health import RUNTIME_IDENTITY_ENV
from discord_blue.health import health_payload
from discord_blue.health import DiscordGatewayHealth
from discord_blue.plugs.discord_plug import BlueBot
from tests.fakes_agent_session import FakeBot


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
            payload = health_payload(discord_status="ok", agent_session_enabled=True, active_agent_sessions=2)

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["service"], "discord-blue")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["runtime_identity"], runtime_identity)
        self.assertEqual(payload["source_git_ref"], "abc123")
        self.assertEqual(payload["image_reference"], "ghcr.io/example/discord-blue:abc123")
        self.assertEqual(payload["components"]["discord"], {"status": "ok"})
        self.assertEqual(
            payload["components"]["agent_session"],
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
            agent_session=SimpleNamespace(
                enabled=True,
                token="shared-secret",
                listen_host="127.0.0.1",
                listen_port=8787,
            )
        )
        bridge = AgentSessionBridge(cast(BlueBot, cast(object, FakeBot(cast(Any, config)))))

        with patch.dict("os.environ", {}, clear=True):
            response = await bridge.handle_health(make_mocked_request("GET", "/health"))

        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "application/json")
        assert isinstance(response.body, bytes)
        body = json.loads(response.body)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["components"]["discord"]["status"], "ok")
        self.assertEqual(body["components"]["discord"]["gateway"]["reason"], "recent_ack")

    async def test_bridge_health_endpoint_reports_unhealthy_when_discord_is_not_ready(self) -> None:
        config = SimpleNamespace(
            agent_session=SimpleNamespace(
                enabled=True,
                token="shared-secret",
                listen_host="127.0.0.1",
                listen_port=8787,
            )
        )
        bot = FakeBot(cast(Any, config))
        bot.ready = False
        bridge = AgentSessionBridge(cast(BlueBot, cast(object, bot)))

        with patch.dict("os.environ", {}, clear=True):
            response = await bridge.handle_health(make_mocked_request("GET", "/health"))

        self.assertEqual(response.status, 503)
        assert isinstance(response.body, bytes)
        body = json.loads(response.body)
        self.assertEqual(body["status"], "unhealthy")
        self.assertEqual(body["components"]["discord"]["status"], "unhealthy")

    async def test_gateway_health_failures_and_recovery_keep_runtime_identity(self) -> None:
        config = SimpleNamespace(agent_session=SimpleNamespace(enabled=False))
        bot = FakeBot(cast(Any, config))
        bridge = AgentSessionBridge(cast(BlueBot, cast(object, bot)))
        identity = {"product": "discord-blue", "source_git_ref": "health-test"}
        with patch.dict("os.environ", {RUNTIME_IDENTITY_ENV: json.dumps(identity)}, clear=True):
            with patch("discord_blue.health.time.monotonic", return_value=100):
                bot.gateway_health = DiscordGatewayHealth()
                for ready, closed in ((False, False), (True, False), (True, True)):
                    bot.ready, bot.closed = ready, closed
                    self.assertEqual((await bridge.handle_health(make_mocked_request("GET", "/health"))).status, 503)
                bot.ready, bot.closed = True, False
                bot.gateway_health.receive('{"op":10,"d":{"heartbeat_interval":20000}}')
                bot.gateway_health.receive('{"op":11}')
            with patch("discord_blue.health.time.monotonic", return_value=139):
                response = await bridge.handle_health(make_mocked_request("GET", "/health"))
                self.assertEqual(response.status, 200)  # Quiet channel: ACKs suffice.
                assert isinstance(response.body, bytes)
                body = json.loads(response.body)
                self.assertEqual(body["runtime_identity"], identity)
            with patch("discord_blue.health.time.monotonic", return_value=141):
                response = await bridge.handle_health(make_mocked_request("GET", "/health"))
                self.assertEqual(response.status, 503)
                assert isinstance(response.body, bytes)
                body = json.loads(response.body)
                self.assertEqual(body["status"], "unhealthy")
                self.assertEqual(body["components"]["discord"]["gateway"]["reason"], "stale")
                # Business events cannot hide missing protocol acknowledgements.
                bot.gateway_health.receive('{"op":0,"t":"MESSAGE_CREATE","d":{"content":"private"}}')
                self.assertEqual((await bridge.handle_health(make_mocked_request("GET", "/health"))).status, 503)
                bot.gateway_health.receive('{"op":11}')
                self.assertEqual((await bridge.handle_health(make_mocked_request("GET", "/health"))).status, 200)
                bot.closed = True
                self.assertEqual((await bridge.handle_health(make_mocked_request("GET", "/health"))).status, 503)

    async def test_real_gateway_dispatch_tracks_ack_and_invalidates_reconnect_proof(self) -> None:
        bot = BlueBot(cast(Any, SimpleNamespace()))
        async with bot:
            # Exercise discord.py's receive path with a fake socket, never Discord.
            socket = DiscordWebSocket(cast(Any, SimpleNamespace(closed=False)), loop=asyncio.get_running_loop())
            socket._dispatch = bot.dispatch
            socket.shard_id = None
            socket._max_heartbeat_timeout = bot._connection.heartbeat_timeout
            self.assertTrue(bot._enable_debug_events)
            with (
                patch.object(socket, "log_receive", socket.debug_log_receive),
                patch.object(socket, "send_as_json", new_callable=AsyncMock),
                patch.object(KeepAliveHandler, "start"),
            ):
                await socket.received_message('{"op":10,"d":{"heartbeat_interval":30000}}')
                self.assertEqual(bot.gateway_health.evidence(ready=True)["status"], "unhealthy")
                await socket.received_message('{"op":11}')
                self.assertEqual(bot.gateway_health.evidence(ready=True)["status"], "ok")
                bot.dispatch("disconnect")
                self.assertEqual(bot.gateway_health.evidence(ready=True)["status"], "unhealthy")
                await socket.received_message('{"op":11}')
                self.assertEqual(bot.gateway_health.evidence(ready=True)["status"], "unhealthy")
                await socket.received_message('{"op":10,"d":{"heartbeat_interval":30000}}')
                await socket.received_message('{"op":11}')
                self.assertEqual(bot.gateway_health.evidence(ready=True)["status"], "ok")
                # The next HELLO cannot inherit the previous connection's ACK.
                await socket.received_message('{"op":10,"d":{"heartbeat_interval":30000}}')
                self.assertEqual(bot.gateway_health.evidence(ready=True)["status"], "unhealthy")


class GatewayHealthTests(unittest.TestCase):
    def test_invalid_hello_and_control_frames_never_retain_good_evidence(self) -> None:
        for reset in ('{"op":7}', '{"op":9,"d":true}', '{"op":10,"d":{}}'):
            with self.subTest(reset=reset):
                health = DiscordGatewayHealth()
                health.receive('{"op":10,"d":{"heartbeat_interval":45000}}')
                health.receive('{"op":11}')
                self.assertEqual(health.evidence(ready=True)["status"], "ok")
                health.receive(reset)
                health.receive('{"op":11}')
                self.assertEqual(health.evidence(ready=True)["status"], "unhealthy")

    def test_non_protocol_input_cannot_supply_connectivity_evidence(self) -> None:
        health = DiscordGatewayHealth()
        for raw in ("{", "[]", '{"op":10,"d":{"heartbeat_interval":-1}}', '{"op":10,"d":{"heartbeat_interval":true}}'):
            health.receive(raw)
            health.receive('{"op":11}')
            self.assertEqual(health.evidence(ready=True)["status"], "unhealthy")


if __name__ == "__main__":
    unittest.main()
