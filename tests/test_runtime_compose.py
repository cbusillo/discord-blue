"""Opt-in artifact qualification: DISCORD_BLUE_COMPOSE_TEST_IMAGE selects a disposable built image."""

import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path


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
            DISCORD_BLUE_RUNTIME_CONFIG=(
                '[discord]\nguild_id=123\nbot_channel_id=456\nloaded_doodads=["agent_session_doodad"]\n'
                '[agent_session]\nenabled=true\nlisten_host="127.0.0.1"\nlisten_port=18787\n'
            ),
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
        self.assertEqual(created["Image"], self.env["LAUNCHPLANE_IMAGE_REFERENCE"])
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
            {"DISCORD_BLUE_RUNTIME_CONFIG": '[discord]\ntoken="fictional-do-not-persist"'},
            {"DISCORD_BLUE_STATE_DIR": "/fictional-wrong-state"},
        )
        for overrides in cases:
            with self.subTest(invalid_keys=list(overrides)):
                original = dict(self.env)
                self.env.update(overrides)
                result = self.compose("run", "--rm", "--no-deps", "-T", "discord-blue")
                self.env = original
                self.assertEqual(result.returncode, int(bool(overrides)), "Unexpected entrypoint outcome")
                for value in ("fictional-bot-value", "fictional-bridge-value", "fictional-do-not-persist"):
                    self.assertNotIn(value, result.stdout + result.stderr)
                if "DISCORD_BLUE_STATE_DIR" in overrides:
                    self.assertNotIn("probe-started", result.stdout)
                else:
                    self.assertIn("probe-started", result.stdout)
                if "DISCORD_BLUE_RUNTIME_CONFIG" in overrides:
                    self.assertIn("runtime-config-refused", result.stdout)
                if not overrides:
                    self.assertIn("runtime-inputs-qualified", result.stdout)


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

class OfflineBot:
    def __init__(self, config):
        assert os.getuid() != 0
        assert Path.home() == Path(os.environ["DISCORD_BLUE_STATE_DIR"])
        assert config.discord.token == os.environ["DISCORD_TOKEN"]
        assert config.agent_session.token == os.environ["AGENT_SESSION_TOKEN"]
        assert config.discord.guild_id > 0 and config.discord.bot_channel_id > 0
        assert "agent_session_doodad" in config.discord.loaded_doodads
        assert config.agent_session.enabled
        supplied = tomllib.loads(os.environ["DISCORD_BLUE_RUNTIME_CONFIG"])["agent_session"]
        assert config.agent_session.listen_host == supplied["listen_host"]
        assert config.agent_session.listen_port == supplied["listen_port"]
        assert runtime_identity() == json.loads(os.environ["LAUNCHPLANE_RUNTIME_IDENTITY_JSON"])
        config.update_and_save(debug=True)
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
