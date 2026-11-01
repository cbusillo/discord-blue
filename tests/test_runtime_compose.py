"""Opt-in artifact qualification: DISCORD_BLUE_COMPOSE_TEST_IMAGE selects a disposable built image."""

import json
import os
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from scripts.artifact_manifest import write_manifest


@unittest.skipUnless(os.environ.get("DISCORD_BLUE_COMPOSE_TEST_IMAGE"), "requires an explicit disposable container image")
class RuntimeComposeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.override = Path(temporary.name) / "probe.json"
        self.project = f"db-runtime-test-{uuid.uuid4().hex}"
        self.env = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"}
        }
        self.env.update(
            LAUNCHPLANE_IMAGE_REFERENCE=os.environ["DISCORD_BLUE_COMPOSE_TEST_IMAGE"],
            LAUNCHPLANE_SOURCE_GIT_REF="fictional-source",
            LAUNCHPLANE_RUNTIME_IDENTITY_JSON=json.dumps({"product": "fixture-bot", "environment": "testing"}),
            DISCORD_BLUE_GUILD_ID="123",
            DISCORD_BLUE_BOT_CHANNEL_ID="456",
            DISCORD_BLUE_SPAWNED_CHANNEL_ID="654",
            DISCORD_BLUE_AUTO_JOIN_USER_IDS="[789]",
            DISCORD_TOKEN="fictional-bot-value",
            AGENT_SESSION_TOKEN="fictional-bridge-value",
        )
        inspected_image = subprocess.run(
            ["docker", "image", "inspect", self.env["LAUNCHPLANE_IMAGE_REFERENCE"]],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        (image,) = json.loads(inspected_image.stdout)
        self.selected_image_id = image["Id"]
        image_env = dict(value.split("=", 1) for value in image["Config"]["Env"])
        self.env["DISCORD_BLUE_STATE_DIR"] = image_env["HOME"]
        self.command = [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "--project-directory",
            str(self.override.parent),
            "--project-name",
            self.project,
            "--file",
            str(Path(__file__).resolve().parents[1] / "compose.runtime.yml"),
        ]
        self.addCleanup(self.compose, "down", "--volumes", "--remove-orphans")

    def compose(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([*self.command, *arguments], env=self.env, text=True, capture_output=True, timeout=120, check=False)

    def test_required_inputs_refuse_before_container_creation(self) -> None:
        self.assertEqual(self.compose("config", "--quiet").returncode, 0)
        for key in tuple(self.env):
            if key in {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"}:
                continue
            with self.subTest(key=key):
                value = self.env[key]
                for missing in (None, ""):
                    if missing is None:
                        del self.env[key]
                    else:
                        self.env[key] = missing
                    result = self.compose("config", "--quiet")
                    self.assertNotEqual(result.returncode, 0, "Incomplete managed input must refuse")
                    self.assertIn(key, result.stderr)
                    self.assertNotIn(self.env["DISCORD_TOKEN"] if key != "DISCORD_TOKEN" else value, result.stderr)
                self.env[key] = value

    def test_created_runtime_uses_selected_image_and_project_local_resources(self) -> None:
        self.assertEqual(self.compose("create", "discord-blue").returncode, 0)
        container = self.compose("ps", "--all", "--quiet", "discord-blue").stdout.strip()
        self.assertTrue(container)
        result = subprocess.run(
            ["docker", "inspect", container], env=self.env, capture_output=True, text=True, check=True, timeout=30
        )
        (created,) = json.loads(result.stdout)
        self.assertEqual(created["Image"], self.selected_image_id)
        self.assertFalse(created["HostConfig"]["PortBindings"])
        container_env = dict(value.split("=", 1) for value in created["Config"]["Env"])
        (mount,) = created["Mounts"]
        self.assertEqual(mount["Type"], "volume")
        self.assertEqual(mount["Destination"], container_env["HOME"])
        for kind, names in (
            ("volume", [mount["Name"]]),
            ("network", list(created["NetworkSettings"]["Networks"])),
        ):
            self.assertEqual(len(names), 1)
            inspected = subprocess.run(
                ["docker", kind, "inspect", *names], env=self.env, capture_output=True, text=True, check=True, timeout=30
            )
            (resource,) = json.loads(inspected.stdout)
            self.assertEqual(resource["Labels"]["com.docker.compose.project"], self.project)

    def test_entrypoint_delivers_inputs_without_persisting_credentials(self) -> None:
        self.override.write_text(
            json.dumps(
                {
                    "services": {
                        "discord-blue": {
                            "command": ["/app/.venv/bin/python", "-c", ENTRYPOINT_PROBE],
                            "restart": "no",
                        }
                    }
                }
            )
        )
        self.command.extend(["--file", str(self.override)])
        cases: tuple[dict[str, str], ...] = (
            {},
            {"DISCORD_BLUE_GUILD_ID": "fictional-do-not-persist"},
            {"DISCORD_BLUE_STATE_DIR": "/fictional-wrong-state"},
        )
        for overrides in cases:
            with self.subTest(invalid_keys=list(overrides)):
                original = dict(self.env)
                self.env.update(overrides)
                result = self.compose("run", "--rm", "--no-deps", "-T", "discord-blue")
                self.env = original
                if overrides:
                    self.assertNotEqual(result.returncode, 0, "Invalid runtime input must refuse")
                else:
                    self.assertEqual(result.returncode, 0, "Valid runtime input must start")
                for value in ("fictional-bot-value", "fictional-bridge-value", "fictional-do-not-persist"):
                    self.assertNotIn(value, result.stdout + result.stderr)
                if "DISCORD_BLUE_STATE_DIR" in overrides:
                    self.assertNotIn("probe-started", result.stdout)
                else:
                    self.assertIn("probe-started", result.stdout)
                if "DISCORD_BLUE_GUILD_ID" in overrides:
                    self.assertIn("runtime-config-refused", result.stdout)
                if not overrides:
                    self.assertIn("runtime-inputs-qualified", result.stdout)

    def test_container_signal_reaches_python(self) -> None:
        self.override.write_text(
            json.dumps({"services": {"discord-blue": {"command": ["/app/.venv/bin/python", "-c", SHUTDOWN_PROBE], "restart": "no"}}})
        )
        self.command.extend(["--file", str(self.override)])
        self.assertEqual(self.compose("up", "--detach", "discord-blue").returncode, 0)
        container = self.compose("ps", "--quiet", "discord-blue").stdout.strip()
        deadline = time.monotonic() + 15
        logs = ""
        while "probe-ready" not in logs and time.monotonic() < deadline:
            logs = subprocess.run(
                ["docker", "logs", container], env=self.env, text=True, capture_output=True, check=True, timeout=10
            ).stdout
            time.sleep(0.05)
        self.assertIn("probe-ready", logs)
        self.assertEqual(self.compose("stop", "discord-blue").returncode, 0)
        logs = subprocess.run(
            ["docker", "logs", container], env=self.env, text=True, capture_output=True, check=True, timeout=10
        ).stdout
        self.assertIn("probe-finished", logs)
        result = subprocess.run(
            ["docker", "inspect", container], env=self.env, capture_output=True, text=True, check=True, timeout=10
        )
        (stopped,) = json.loads(result.stdout)
        self.assertEqual(stopped["State"]["ExitCode"], 0)

    def test_offline_copy_uses_image_runtime_and_independent_managed_inputs(self) -> None:
        source = self.override.parent / "source"
        config = source / ".config/discord-blue/config.toml"
        config.parent.mkdir(parents=True)
        for directory in (source, source / ".config", config.parent):
            directory.chmod(0o755)
        original = 'debug = true\n[discord]\ntoken = "synthetic-production"\nguild_id = 999\n'
        config.write_text(original)
        config.chmod(0o644)
        manifest = self.override.parent / "manifest.json"
        write_manifest(
            source_commit="a" * 40,
            repository="cbusillo/discord-blue",
            image_reference="ghcr.io/cbusillo/discord-blue@sha256:" + "b" * 64,
            destination=manifest,
        )
        command = json.loads(manifest.read_text())["testing_state_copy"]["command"]
        service = {
            "image": self.env["LAUNCHPLANE_IMAGE_REFERENCE"],
            "network_mode": "none",
            "volumes": [f"{source}:/source:ro", f"state:{self.env['DISCORD_BLUE_STATE_DIR']}"],
            "command": command,
        }
        self.command[-1] = str(self.override)

        def run_command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
            service["command"] = arguments
            self.override.write_text(json.dumps({"services": {"discord-blue": service}, "volumes": {"state": {}}}))
            result = self.compose("run", "--rm", "--no-deps", "-T", "discord-blue")
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertNotIn("synthetic-production", result.stdout + result.stderr)
            return result

        planned = json.loads(run_command([*command, "plan", "--source", "/source"]).stdout)
        copied = json.loads(
            run_command(
                [
                    *command,
                    "copy",
                    "--source",
                    "/source",
                    "--destination",
                    self.env["DISCORD_BLUE_STATE_DIR"],
                    "--expected-sha256",
                    planned["sha256"],
                ]
            ).stdout
        )
        verified = json.loads(
            run_command(
                [*command, "verify", "--destination", self.env["DISCORD_BLUE_STATE_DIR"], "--expected-sha256", planned["sha256"]]
            ).stdout
        )
        self.assertEqual(planned, copied)
        self.assertEqual(planned, verified)
        self.assertEqual(config.read_text(), original)
        service["environment"] = {key: self.env[key] for key in ("DISCORD_TOKEN", "DISCORD_BLUE_GUILD_ID")}
        result = run_command([command[0], "-c", STATE_COPY_RUNTIME_PROBE])
        self.assertIn("offline-state-copy-qualified", result.stdout)


STATE_COPY_RUNTIME_PROBE = """
import os
from pathlib import Path
import sys
from discord_blue.config import Config
home = Path.home()
assert os.geteuid() != 0
assert not Path(sys.executable).resolve().is_relative_to(home)
runtime = Config()
assert runtime.debug
assert runtime.discord.token == os.environ['DISCORD_TOKEN']
assert runtime.discord.guild_id == int(os.environ['DISCORD_BLUE_GUILD_ID'])
assert not runtime.conversation.enabled
print('offline-state-copy-qualified')
"""


ENTRYPOINT_PROBE = """
print("probe-started", flush=True)
import asyncio
import json
import os
import tomllib
from pathlib import Path
from unittest.mock import patch
from discord_blue import __main__ as server
from discord_blue.health import runtime_identity
assert os.getpid() == 1

class OfflineBot:
    def __init__(self, config):
        assert os.getuid() != 0
        assert Path.home() == Path(os.environ["DISCORD_BLUE_STATE_DIR"])
        assert config.discord.token == os.environ["DISCORD_TOKEN"]
        assert config.agent_session.token == os.environ["AGENT_SESSION_TOKEN"]
        assert config.discord.guild_id > 0 and config.discord.bot_channel_id > 0
        assert "agent_session_doodad" in config.discord.loaded_doodads
        assert config.agent_session.enabled
        assert config.agent_session.spawned_channel_id == int(os.environ["DISCORD_BLUE_SPAWNED_CHANNEL_ID"])
        assert config.agent_session.auto_join_user_ids == json.loads(os.environ["DISCORD_BLUE_AUTO_JOIN_USER_IDS"])
        assert runtime_identity() == json.loads(os.environ["LAUNCHPLANE_RUNTIME_IDENTITY_JSON"])
        config.debug = True
        config.save()
        saved = tomllib.loads(config.filepath.read_text())
        assert "token" not in saved["discord"] and "token" not in saved["agent_session"]
    async def start(self, token):
        assert token == os.environ["DISCORD_TOKEN"]
    async def on_signal(self, signal):
        pass

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
try:
    with patch.object(server, "BlueBot", OfflineBot), patch("builtins.input", side_effect=AssertionError("Unexpected prompt")):
        try:
            server.main()
        except ValueError:
            print("runtime-config-refused")
            raise SystemExit(1)
    print("runtime-inputs-qualified")
finally:
    loop.close()
"""


SHUTDOWN_PROBE = """
import os
import signal
assert os.getpid() == 1
assert os.getuid() != 0
def stop(_signal: int, _frame: object) -> None:
    print("probe-finished", flush=True)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
print("probe-ready", flush=True)
while True:
    signal.pause()
"""
