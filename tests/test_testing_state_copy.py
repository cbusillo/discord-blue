import hashlib
import os
import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import tomli_w

from discord_blue.config import Config
from discord_blue.testing_state_copy import copy_testing_state, inspect_testing_state, plan_testing_state


class TestingStateCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.destination = self.root / "destination"
        self.source.mkdir()
        self.destination.mkdir()
        self.config = self.source / ".config/discord-blue/config.toml"
        self.config.parent.mkdir(parents=True)

    def seed(self) -> bytes:
        data = {
            "debug": True,
            "discord": {"token": "synthetic-live-token", "guild_id": 111, "loaded_doodads": ["external_plugin"]},
            "agent_session": {
                "token": "synthetic-live-bridge",
                "channel_id": 222,
                "auto_join_user_ids": [333],
                "heartbeat_timeout_seconds": 287,
                "heartbeat_check_interval_seconds": 41,
            },
            "conversation": {"enabled": True, "guild_id": 111, "channel_id": 222, "session_id": "old-session"},
            "unknown": {"token": "synthetic-unknown-secret"},
        }
        self.config.write_text(tomli_w.dumps(data))
        for name in ("agent-sessions.json", ".discord-blue-handover.json", ".discord-blue-owner.lock"):
            (self.source / name).write_text('{"live":"synthetic-recovery-route"}')
        deliveries = self.source / ".local/share/discord-blue-bridges"
        deliveries.mkdir(parents=True)
        (deliveries / "codex-startup.json").write_text('{"live":"synthetic-recovery-route"}')
        return self.config.read_bytes()

    def test_copy_preserves_preferences_without_live_routes_or_credentials(self) -> None:
        original = self.seed()
        planned = plan_testing_state(self.source)
        copied = copy_testing_state(self.source, self.destination, planned.sha256)
        self.assertEqual(inspect_testing_state(self.destination), copied)
        self.assertEqual(copied, planned)
        self.assertEqual(self.config.read_bytes(), original)
        output = self.destination / ".config/discord-blue/config.toml"
        self.assertEqual(copied.sha256, hashlib.sha256(output.read_bytes()).hexdigest())
        data = tomllib.loads(output.read_text())
        self.assertTrue(data["debug"])
        self.assertEqual(data["agent_session"]["heartbeat_timeout_seconds"], 287)
        for secret in ("synthetic-live", "synthetic-unknown", "old-session", "synthetic-recovery"):
            self.assertNotIn(secret, output.read_text())
        runtime = {
            "DISCORD_BLUE_GUILD_ID": "123",
            "DISCORD_BLUE_BOT_CHANNEL_ID": "456",
            "DISCORD_BLUE_SPAWNED_CHANNEL_ID": "654",
            "DISCORD_BLUE_AUTO_JOIN_USER_IDS": "[]",
            "DISCORD_TOKEN": "synthetic-testing-bot",
            "AGENT_SESSION_TOKEN": "synthetic-testing-bridge",
        }
        with patch.dict(os.environ, runtime, clear=True), patch("pathlib.Path.home", return_value=self.destination):
            config = Config()
        self.assertEqual(config.discord.token, runtime["DISCORD_TOKEN"])
        self.assertEqual(config.discord.guild_id, int(runtime["DISCORD_BLUE_GUILD_ID"]))
        self.assertFalse(config.conversation.enabled)
        self.assertEqual(config.agent_session.heartbeat_timeout_seconds, data["agent_session"]["heartbeat_timeout_seconds"])
        self.assertEqual({path.name for path in output.parent.iterdir()}, {output.name})

    def test_changed_source_and_nonempty_destination_refuse_before_writes(self) -> None:
        self.seed()
        planned = plan_testing_state(self.source)
        self.config.write_text("debug = false\n")
        with self.assertRaises(ValueError):
            copy_testing_state(self.source, self.destination, planned.sha256)
        self.assertEqual(list(self.destination.iterdir()), [])
        sentinel = self.destination / "needed"
        sentinel.write_text("keep")
        with self.assertRaises(ValueError):
            copy_testing_state(self.source, self.destination, plan_testing_state(self.source).sha256)
        self.assertEqual(sentinel.read_text(), "keep")

    def test_symlinked_source_or_destination_refuses_without_following(self) -> None:
        self.seed()
        planned = plan_testing_state(self.source)
        destination_link = self.root / "destination-link"
        destination_link.symlink_to(self.destination, target_is_directory=True)
        with self.assertRaises(ValueError):
            copy_testing_state(self.source, destination_link, planned.sha256)
        with self.assertRaises(ValueError):
            copy_testing_state(self.source, self.source, planned.sha256)
        link = self.root / "link"
        link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ValueError):
            plan_testing_state(link)
        original = self.config.read_bytes()
        self.config.unlink()
        private = self.root / "private"
        private.write_bytes(original)
        self.config.symlink_to(private)
        with self.assertRaises(ValueError):
            plan_testing_state(self.source)
        self.assertEqual(private.read_bytes(), original)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_unknown_fields_cannot_pass_independent_readback(self) -> None:
        self.seed()
        planned = plan_testing_state(self.source)
        copy_testing_state(self.source, self.destination, planned.sha256)
        copied = self.destination / ".config/discord-blue/config.toml"
        copied.write_text(copied.read_text() + '\n[discord]\ntoken = "synthetic-injected"\n')
        with self.assertRaises(ValueError):
            inspect_testing_state(self.destination)

    def test_empty_initialization_and_image_bootstrap_directories_are_supported(self) -> None:
        (self.destination / ".config/discord-blue").mkdir(parents=True)
        planned = plan_testing_state(self.source)
        copied = copy_testing_state(self.source, self.destination, planned.sha256)
        self.assertEqual(inspect_testing_state(self.destination), copied)
        self.assertEqual(tomllib.loads((self.destination / ".config/discord-blue/config.toml").read_text()), {})

    def test_malformed_preferences_refuse_without_creating_output(self) -> None:
        for value in (
            'debug = "yes"',
            "[agent_session]\nheartbeat_timeout_seconds = true",
            "[agent_session]\nheartbeat_check_interval_seconds = -1",
            "debug = [",
        ):
            with self.subTest(value=value):
                self.config.write_text(value)
                with self.assertRaises(ValueError):
                    plan_testing_state(self.source)
                self.assertEqual(list(self.destination.iterdir()), [])

    def test_source_cannot_overflow_the_runtime_heartbeat_consumer(self) -> None:
        self.config.write_text(f"[agent_session]\nheartbeat_timeout_seconds = {10**1000}\n")
        with self.assertRaises(ValueError):
            plan_testing_state(self.source)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_verification_requires_the_runtime_owner(self) -> None:
        self.seed()
        planned = plan_testing_state(self.source)
        copy_testing_state(self.source, self.destination, planned.sha256)
        with patch("os.geteuid", return_value=os.geteuid() + 1), self.assertRaises(ValueError):
            inspect_testing_state(self.destination)

    def test_copy_commits_each_parent_directory_before_returning(self) -> None:
        self.seed()
        flushed = set()
        fsync = os.fsync

        def flush(descriptor: int) -> None:
            metadata = os.fstat(descriptor)
            flushed.add((metadata.st_dev, metadata.st_ino))
            fsync(descriptor)

        with patch("os.fsync", side_effect=flush):
            copy_testing_state(self.source, self.destination, plan_testing_state(self.source).sha256)
        metadata = (self.destination / ".config").stat()
        self.assertIn((metadata.st_dev, metadata.st_ino), flushed)

    def test_interrupted_write_never_publishes_an_incomplete_config(self) -> None:
        self.seed()
        planned = plan_testing_state(self.source)
        with patch("os.fsync", side_effect=OSError("fixture interrupted flush")), self.assertRaises((OSError, ValueError)):
            copy_testing_state(self.source, self.destination, planned.sha256)
        self.assertFalse((self.destination / ".config/discord-blue/config.toml").exists())
        with self.assertRaises(ValueError):
            inspect_testing_state(self.destination)
        with self.assertRaises(ValueError):
            copy_testing_state(self.source, self.destination, planned.sha256)

    def test_deeply_nested_source_returns_a_bounded_refusal(self) -> None:
        self.config.write_text("ignored = " + "[" * 2000 + "0" + "]" * 2000)
        with self.assertRaises(ValueError):
            plan_testing_state(self.source)

    def test_offline_module_commands_bind_the_copy_and_keep_errors_value_free(self) -> None:
        self.seed()
        planned = plan_testing_state(self.source)
        for arguments in (
            ["plan", "--source", str(self.source)],
            ["copy", "--source", str(self.source), "--destination", str(self.destination), "--expected-sha256", planned.sha256],
            ["verify", "--destination", str(self.destination), "--expected-sha256", planned.sha256],
        ):
            result = subprocess.run(
                [sys.executable, "-m", "discord_blue.testing_state_copy", *arguments],
                capture_output=True,
                text=True,
                cwd=self.root,
                env={},
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["sha256"], planned.sha256)
            self.assertNotIn("synthetic-", result.stdout + result.stderr)
        self.config.write_text('debug = "synthetic-invalid-preference"')
        refused = subprocess.run(
            [sys.executable, "-m", "discord_blue.testing_state_copy", "plan", "--source", str(self.source)],
            capture_output=True,
            text=True,
            cwd=self.root,
            env={},
            check=False,
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertNotIn("synthetic-invalid-preference", refused.stdout + refused.stderr)
        self.assertNotIn(str(self.source), refused.stdout + refused.stderr)
