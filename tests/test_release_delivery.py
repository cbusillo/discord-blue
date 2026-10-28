from __future__ import annotations

import json
import hashlib
import os
import fcntl
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from discord_blue.doodads.agent_session.bridge import AgentSessionBridge
from discord_blue.health import RUNTIME_IDENTITY_ENV
from discord_blue.plugs.discord_plug import BlueBot
from discord_blue.release_delivery import (
    BRIDGES,
    HOOK_PATH,
    STARTUP_RECEIPT,
    DeliveryReceipts,
    production_delivery,
    save_json,
)
from scripts import reconcile_bridge_delivery as consumer
from tests.fakes_agent_session import FakeBot


class ReleaseDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.target = {"revision": "a" * 40, "hooks_sha256": "b" * 64}
        self.receipt = {"status": "delivered", **self.target, "codex_revision": self.target["revision"], "codex_pid": 123}
        config = SimpleNamespace(agent_session=SimpleNamespace(enabled=True, token="fixture-token"))
        self.bridge = AgentSessionBridge(
            cast(BlueBot, cast(object, FakeBot(cast(Any, config)))), store_path=self.base / "sessions.json"
        )
        app = web.Application()
        self.bridge.register_routes(app)
        self.server = TestServer(app)
        self.client = TestClient(self.server)
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.token = self.bridge.bot.config.agent_session.token
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.target_patch = patch("discord_blue.doodads.agent_session.bridge.production_delivery", side_effect=lambda: self.target)
        self.target_patch.start()
        self.addCleanup(self.target_patch.stop)

    async def test_registered_mac_stays_pending_across_releases_failures_and_server_restarts(self) -> None:
        host = "12345678-1234-1234-1234-123456789abc"
        response = await self.client.get("/agent-session/delivery")
        self.assertEqual(response.status, 401)
        async with self.client.get("/agent-session/delivery", headers=self.headers) as response:
            self.assertEqual((await response.json())["status"], "pending")
        for receipt, status in ((self.receipt, "delivered"), ({"status": "pending", **self.target}, "pending")):
            async with self.client.post(
                "/agent-session/delivery", headers=self.headers, json={"host_id": host, "receipt": receipt}
            ) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["clients"][host]["status"], status)
        self.bridge.deliveries.save(host, self.receipt)
        self.target = {**self.target, "revision": "c" * 40}
        recovered = DeliveryReceipts(self.bridge.deliveries.path)
        self.assertEqual(recovered.status(self.target)["clients"][host]["status"], "pending")
        stale = {**self.receipt, "revision": self.target["revision"]}
        recovered.save(host, stale)
        self.assertEqual(recovered.status(self.target)["status"], "pending")
        current = {**stale, "codex_revision": self.target["revision"]}
        recovered.save(host, current)
        self.assertEqual(recovered.status(self.target)["status"], "delivered")
        recovered.path.write_text("{")
        async with self.client.get("/agent-session/delivery", headers=self.headers) as response:
            self.assertEqual(response.status, 503)
            self.assertEqual((await response.json())["status"], "pending")

    async def test_consumer_reconciles_an_offline_mac_and_reposts_a_lost_ack_without_reloading(self) -> None:
        registration = {
            "host_id": "12345678-1234-1234-1234-123456789abc",
            "bridge_config": str(self.base / "config.toml"),
            "checkout": str(self.base / "checkout"),
            "home": str(self.base / "home"),
        }
        config = SimpleNamespace(
            server_url=str(self.server.make_url("/agent-session/connect")).replace("http:", "ws:"), token=self.token
        )
        receipt_path = self.base / "receipt.json"
        with (
            patch.object(consumer.bridge_config, "load_config", return_value=config),
            patch.object(consumer, "deliver_target", return_value=(self.receipt, True)) as deliver,
            patch.object(consumer, "observed_receipt", return_value=self.receipt),
            patch.object(consumer, "advance_automation") as advance,
        ):
            receipt = await consumer.reconcile(registration, receipt_path)
            self.assertEqual(receipt, self.receipt)
            advance.assert_called_once()
            advance.reset_mock()
            self.bridge.deliveries.path.unlink()
            await consumer.reconcile(registration, receipt_path)
            deliver.assert_called_once()
            advance.assert_not_called()
            self.target = {"revision": "c" * 40, "hooks_sha256": "d" * 64}
            deliver.side_effect = OSError("Mac delivery unavailable")
            with self.assertRaises(ValueError):
                await consumer.reconcile(registration, receipt_path)
            self.assertEqual(json.loads(receipt_path.read_text())["status"], "pending")
            self.assertEqual(self.bridge.deliveries.status(self.target)["status"], "pending")
            deliver.side_effect = None
            deliver.return_value = ({**self.receipt, **self.target, "codex_revision": self.target["revision"]}, True)
            await consumer.reconcile(registration, receipt_path)
            self.assertEqual(self.bridge.deliveries.status(self.target)["status"], "delivered")

    async def test_unavailable_production_never_requests_installation(self) -> None:
        with patch("discord_blue.doodads.agent_session.bridge.production_delivery", side_effect=ValueError):
            async with self.client.get("/agent-session/delivery", headers=self.headers) as response:
                self.assertEqual(response.status, 503)
        malformed = {**self.receipt, "error": "private local path"}
        async with self.client.post(
            "/agent-session/delivery",
            headers=self.headers,
            json={
                "host_id": "12345678-1234-1234-1234-123456789abc",
                "receipt": malformed,
            },
        ) as response:
            self.assertEqual(response.status, 400)
        for body in (
            {"host_id": "invalid", "receipt": self.receipt},
            {"host_id": "12345678-1234-1234-1234-123456789abc", "receipt": {**self.receipt, "codex_pid": True}},
        ):
            async with self.client.post("/agent-session/delivery", headers=self.headers, json=body) as response:
                self.assertEqual(response.status, 400)
        async with self.client.post("/agent-session/delivery", headers=self.headers, data="{") as response:
            self.assertEqual(response.status, 400)
        host = "12345678-1234-1234-1234-123456789abc"
        self.bridge.deliveries.save(host, self.receipt)
        async with self.client.delete("/agent-session/delivery", headers=self.headers, json={"host_id": host}) as response:
            self.assertEqual(response.status, 200)
            self.assertFalse(self.bridge.deliveries.read())


class ProductionSourceTests(unittest.TestCase):
    def test_only_exact_gated_production_identity_selects_source(self) -> None:
        revision = "a" * 40
        identity: dict[str, Any] = {
            "product": "discord-blue",
            "instance": "prod",
            "source_git_ref": revision,
            "deployment_record_id": "deployment",
            "artifact_id": "artifact",
        }
        with patch.dict(
            "os.environ", {RUNTIME_IDENTITY_ENV: json.dumps(identity), "LAUNCHPLANE_SOURCE_GIT_REF": revision}, clear=True
        ):
            self.assertEqual(production_delivery()["revision"], revision)
        for change in ({"instance": "testing"}, {"product": "other"}, {"source_git_ref": "short"}, {"artifact_id": ""}):
            with patch.dict(
                "os.environ",
                {
                    RUNTIME_IDENTITY_ENV: json.dumps({**identity, **change}),
                    "LAUNCHPLANE_SOURCE_GIT_REF": revision,
                },
                clear=True,
            ):
                with self.assertRaises(ValueError):
                    production_delivery()
        with tempfile.TemporaryDirectory() as directory:
            receipts = DeliveryReceipts(Path(directory) / "receipts.json")
            for change in ({"instance": "testing"}, {"environment_kind": "preview", "instance": "pr-301"}):
                with patch.dict(
                    "os.environ",
                    {
                        RUNTIME_IDENTITY_ENV: json.dumps({**identity, **change}),
                        "LAUNCHPLANE_SOURCE_GIT_REF": revision,
                    },
                    clear=True,
                ):
                    self.assertEqual(receipts.health()["status"], "not_applicable")
            with patch.dict("os.environ", {RUNTIME_IDENTITY_ENV: "{"}, clear=True):
                self.assertEqual(receipts.health()["status"], "pending")


class LocalDeliveryTests(unittest.TestCase):
    def test_exact_hook_refresh_existing_command_and_fresh_startup_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, home = base / "source", base / "home"
            source.mkdir()
            real_output = subprocess.check_output
            real_run = subprocess.run

            def git(*args: str) -> str:
                return real_output(["git", "-C", str(source), *args], text=True, stderr=subprocess.DEVNULL).strip()

            git("init", "-q", "-b", "main")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            git("remote", "add", "origin", str(source))
            hooks = source / HOOK_PATH
            hooks.parent.mkdir(parents=True)
            hooks.write_text('{"hooks":{}}')
            git("add", ".")
            git("commit", "-qm", "old")
            previous = git("rev-parse", "HEAD")
            hooks.write_text('{"hooks":{"Stop":[]}}')
            git("commit", "-qam", "released")
            target = {"revision": git("rev-parse", "HEAD"), "hooks_sha256": hashlib.sha256(hooks.read_bytes()).hexdigest()}
            remote = base / "remote"
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
            git("remote", "set-url", "origin", str(remote))
            git("push", "-q", "origin", "main")
            git("checkout", "--detach", previous)
            git("branch", "local-main", previous)
            git("checkout", "local-main")
            git("branch", "-D", "main")
            git("branch", "-m", "main")
            registration = {"checkout": str(source), "home": str(home), "build_dir": str(base / "build")}
            installed_sources: list[str] = []

            def output(args: list[str], **kwargs: object) -> str | bytes:
                if args[0] == "claude":
                    return json.dumps([{"id": "dui@discord-blue", "enabled": True, "readFromFolder": str(hooks.parent.parent)}])
                if args[0] == "launchctl":
                    return "state = running\n pid = 123\n"
                if args[0] == "ps":
                    return str(home / ".local/share/discord-blue-bridges" / target["revision"] / "venv/bin/python")
                return real_output(args, **cast(Any, kwargs))

            def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if "--revision" not in args:
                    return real_run(args, **cast(Any, kwargs))
                installed_sources.append(git("rev-parse", "HEAD"))
                revision = args[args.index("--revision") + 1]
                for name in BRIDGES:
                    launcher = home / ".local/bin" / name
                    launcher.parent.mkdir(parents=True, exist_ok=True)
                    launcher.write_text(f"#!{sys.executable}\nprint({revision!r})\n")
                    launcher.chmod(0o700)
                save_json(home / STARTUP_RECEIPT, {"pid": 123, "revision": revision})
                return subprocess.CompletedProcess(args, 0)

            with (
                patch.object(consumer.subprocess, "check_output", side_effect=output),
                patch.object(consumer.subprocess, "run", side_effect=run),
            ):
                receipt, installed = consumer.deliver_target(registration, target)
            self.assertTrue(installed)
            self.assertEqual(installed_sources, [previous])
            self.assertEqual(receipt["revision"], git("rev-parse", "HEAD"))
            self.assertEqual(receipt["hooks_sha256"], hashlib.sha256(hooks.read_bytes()).hexdigest())
            self.assertEqual(receipt["status"], "delivered")
            self.assertEqual(git("rev-parse", "main"), previous)
            with (
                patch.object(consumer.subprocess, "check_output", side_effect=output),
                patch.object(consumer.subprocess, "run", side_effect=run) as installer,
            ):
                save_json(home / STARTUP_RECEIPT, {"pid": 456, "revision": target["revision"]})
                with patch.object(consumer, "job_pid", return_value=456):
                    resumed, installed = consumer.deliver_target(registration, target)
                self.assertEqual(resumed["codex_pid"], 456)
                self.assertFalse(installed)
                self.assertFalse(any("--revision" in call.args[0] for call in installer.call_args_list))
                (home / STARTUP_RECEIPT).unlink()
                for _ in range(2):
                    with self.assertRaises(consumer.DeliveryFailure) as failed:
                        consumer.deliver_target(registration, target)
                    self.assertEqual(failed.exception.reason, "startup_unverified")
                self.assertFalse(any("--revision" in call.args[0] for call in installer.call_args_list))
                target = {"revision": previous, "hooks_sha256": hashlib.sha256(b'{"hooks":{}}').hexdigest()}
                rolled_back, installed = consumer.deliver_target(registration, target)
                self.assertTrue(installed)
                self.assertEqual(rolled_back["revision"], previous)
                self.assertEqual(hooks.read_bytes(), b'{"hooks":{}}')
            attempts = base / "attempt.json"

            def failing_install(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if "--revision" in args:
                    raise subprocess.CalledProcessError(1, args)
                return real_run(args, **cast(Any, kwargs))

            with (
                patch.object(consumer.subprocess, "check_output", side_effect=output),
                patch.object(consumer.subprocess, "run", side_effect=failing_install) as installer,
                patch.object(consumer, "verify_installed", side_effect=ValueError),
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    consumer.deliver_target(registration, target, attempts)
                with self.assertRaises(consumer.DeliveryFailure):
                    consumer.deliver_target(registration, target, attempts)
                self.assertEqual(sum("--revision" in call.args[0] for call in installer.call_args_list), 1)
            target = {
                "revision": git("rev-parse", "origin/main"),
                "hooks_sha256": hashlib.sha256(b'{"hooks":{"Stop":[]}}').hexdigest(),
            }
            interrupted = base / "interrupted-attempt.json"
            save_json(interrupted, {"revision": target["revision"], "started_at": 0, "outcome": "started"})
            lock_path = home / ".local/share/discord-blue-bridges/delivery.lock"
            with (
                patch.object(consumer.subprocess, "check_output", side_effect=output),
                patch.object(consumer.subprocess, "run", side_effect=run) as installer,
            ):
                with lock_path.open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with self.assertRaises(consumer.DeliveryFailure):
                        consumer.deliver_target(registration, target, interrupted)
                    self.assertEqual(git("rev-parse", "HEAD"), previous)
                    self.assertFalse(any("--revision" in call.args[0] for call in installer.call_args_list))
                recovered, installed = consumer.deliver_target(registration, target, interrupted)
                self.assertTrue(installed)
                self.assertEqual(recovered["revision"], target["revision"])
                self.assertEqual(sum("--revision" in call.args[0] for call in installer.call_args_list), 1)
            succeeded = base / "successful-attempt.json"
            with (
                patch.object(consumer.subprocess, "check_output", side_effect=output),
                patch.object(consumer.subprocess, "run", side_effect=run) as installer,
                patch.object(consumer, "verify_installed", side_effect=ValueError),
            ):
                with patch.object(consumer.time, "monotonic", side_effect=[0, 1, 31]), patch.object(consumer.time, "sleep"):
                    with self.assertRaises(consumer.DeliveryFailure):
                        consumer.deliver_target(registration, target, succeeded)
                with patch.object(consumer.time, "time", return_value=consumer.time.time() + 1800):
                    with self.assertRaises(consumer.DeliveryFailure):
                        consumer.deliver_target(registration, target, succeeded)
                self.assertEqual(sum("--revision" in call.args[0] for call in installer.call_args_list), 1)
            hooks.write_text("local work")
            with (
                self.assertRaises(ValueError),
                patch.object(consumer.subprocess, "run", side_effect=run) as install,
                patch.object(consumer.subprocess, "check_output", side_effect=output),
            ):
                consumer.deliver_target(registration, target)
            self.assertFalse(any("--revision" in call.args[0] for call in install.call_args_list))
            self.assertEqual(hooks.read_text(), "local work")

    def test_registration_recovers_bootstrap_failure_with_another_shell_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            (home / "Library/LaunchAgents").mkdir(parents=True)
            path = base / "registration.json"
            prepared = {"runtime": str(base / "release/venv"), "source": str(base / "release/source")}
            bootstraps = 0

            def run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                nonlocal bootstraps
                if args[1] == "print":
                    return subprocess.CompletedProcess(args, 113)
                bootstraps += 1
                if bootstraps == 1:
                    raise OSError("interrupted bootstrap")
                return subprocess.CompletedProcess(args, 0)

            with (
                patch.object(consumer, "verify_hook_registration"),
                patch.object(consumer.shutil, "which", return_value="/fixture/uv"),
                patch.object(consumer.subprocess, "check_output", return_value=json.dumps(prepared)) as prepare,
                patch.object(consumer.subprocess, "run", side_effect=run),
            ):
                with self.assertRaises(OSError):
                    consumer.register(path, base / "source", home, base / "build", base / "config")
                original = json.loads(path.read_text())
                with patch.dict(os.environ, {"PATH": "/another/shell"}):
                    consumer.register(path, base / "source", home, base / "build", base / "config")
                self.assertEqual(json.loads(path.read_text())["host_id"], original["host_id"])
                prepare.assert_called_once()
                self.assertEqual(bootstraps, 2)
            automation = home / ".local/share/discord-blue-bridges/automation"
            initial = automation.resolve()
            target = {"revision": "a" * 40}
            consumer.advance_automation(home, target)
            self.assertEqual(automation.resolve(), initial)
            release = automation.parent / target["revision"]
            script = release / "source/scripts/reconcile_bridge_delivery.py"
            script.parent.mkdir(parents=True)
            script.write_text("released reconciler")
            consumer.advance_automation(home, target)
            self.assertEqual(automation.resolve(), release.resolve())
            with (
                patch.object(consumer, "verify_hook_registration"),
                patch.object(consumer.shutil, "which", return_value="/fixture/uv"),
                patch.object(consumer.subprocess, "run", side_effect=run),
            ):
                consumer.register(path, base / "source", home, base / "build", base / "config")
            self.assertEqual(automation.resolve(), initial)
