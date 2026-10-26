import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from discord_blue.config import Config


class RuntimeConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.config_path = self.home / ".config/discord-blue/config.toml"
        self.config_path.parent.mkdir(parents=True)
        self.addCleanup(patch.stopall)
        patch("pathlib.Path.home", return_value=self.home).start()
        patch.dict(os.environ, {}, clear=True).start()

    def test_fresh_runtime_starts_without_prompt_and_does_not_persist_inputs(self) -> None:
        os.environ.update(
            DISCORD_TOKEN="testing-bot-value",
            AGENT_SESSION_TOKEN="testing-bridge-value",
            DISCORD_BLUE_RUNTIME_CONFIG="""
[discord]
guild_id = 123
bot_channel_id = 456
loaded_doodads = ["agent_session_doodad"]
[agent_session]
enabled = true
operator_role_name = "Agent Admin"
auto_join_user_ids = [789]
spawned_channel_id = 654
""",
        )
        with patch("builtins.input", side_effect=AssertionError("Runtime must not prompt")):
            config = Config()
        self.assertEqual(config.discord.token, os.environ["DISCORD_TOKEN"])
        self.assertEqual(config.agent_session.token, os.environ["AGENT_SESSION_TOKEN"])
        self.assertEqual((config.discord.guild_id, config.discord.bot_channel_id), (123, 456))
        self.assertTrue(config.agent_session.enabled)
        self.assertEqual(config.agent_session.auto_join_user_ids, [789])
        self.assertEqual(config.agent_session.spawned_channel_id, 654)
        config.debug = True
        config.save()
        stored = tomllib.loads(self.config_path.read_text())
        self.assertTrue(stored["debug"])
        self.assertNotIn("token", stored["discord"])
        self.assertNotIn("token", stored["agent_session"])
        self.assertNotIn("guild_id", stored["discord"])
        self.assertNotIn("enabled", stored["agent_session"])
        self.assertNotIn("spawned_channel_id", stored["agent_session"])

    def test_runtime_override_preserves_existing_local_config(self) -> None:
        self.config_path.write_text(
            '[discord]\ntoken="local-bot"\nguild_id=222\nbot_channel_id=333\n[agent_session]\ntoken="local-bridge"\n'
        )
        os.environ.update(
            DISCORD_TOKEN="runtime-bot",
            AGENT_SESSION_TOKEN="runtime-bridge",
            DISCORD_BLUE_RUNTIME_CONFIG="[agent_session]\nenabled=false",
        )
        config = Config()
        self.assertEqual(config.discord.token, "runtime-bot")
        config.debug = True
        config.save()
        os.environ.clear()
        local = Config()
        self.assertEqual(local.discord.token, "local-bot")
        self.assertEqual(local.agent_session.token, "local-bridge")
        self.assertEqual(local.discord.guild_id, 222)
        self.assertTrue(local.debug)

    def test_incomplete_fresh_runtime_refuses_all_interactive_setup(self) -> None:
        cases = (
            {"DISCORD_BLUE_RUNTIME_CONFIG": "[discord]\nguild_id=123\nbot_channel_id=456"},
            {"DISCORD_TOKEN": "synthetic-bot", "DISCORD_BLUE_RUNTIME_CONFIG": "[agent_session]\nenabled=false"},
            {
                "DISCORD_TOKEN": "synthetic-bot",
                "DISCORD_BLUE_RUNTIME_CONFIG": "[discord]\nguild_id=123\nbot_channel_id=456\n[agent_session]\nenabled=true",
            },
        )
        for env in cases:
            with self.subTest(keys=list(env)), patch.dict(os.environ, env, clear=True):
                self.config_path.write_text("")
                with patch("builtins.input", side_effect=AssertionError("Managed runtime must not prompt")):
                    with self.assertRaises(ValueError):
                        Config()
                self.assertEqual(self.config_path.read_text(), "")

    def test_runtime_credentials_match_client_whitespace_normalization(self) -> None:
        os.environ.update(
            DISCORD_TOKEN=" synthetic-bot \n",
            AGENT_SESSION_TOKEN="\n synthetic-bridge \n",
            DISCORD_BLUE_RUNTIME_CONFIG="[discord]\nguild_id=123\nbot_channel_id=456",
        )
        config = Config()
        self.assertEqual(config.discord.token, os.environ["DISCORD_TOKEN"].strip())
        self.assertEqual(config.agent_session.token, os.environ["AGENT_SESSION_TOKEN"].strip())

    def test_invalid_inputs_fail_before_config_is_written_or_prompted(self) -> None:
        cases = (
            {"DISCORD_TOKEN": ""},
            {"AGENT_SESSION_TOKEN": "  "},
            {"DISCORD_BLUE_RUNTIME_CONFIG": '[discord]\ntoken="secret-should-not-be-here"'},
            {"DISCORD_BLUE_RUNTIME_CONFIG": "[discord]\nguild_id=true"},
            {"DISCORD_BLUE_RUNTIME_CONFIG": '[agent_session]\nauto_join_user_ids=["wrong-type"]'},
            {"DISCORD_BLUE_RUNTIME_CONFIG": "[conversation]\nenabled=true"},
            {"DISCORD_BLUE_RUNTIME_CONFIG": "[discord]\nunknown_field=5"},
            {"DISCORD_BLUE_RUNTIME_CONFIG": 'invalid = "secret-should-not-be-here'},
            {"DISCORD_BLUE_RUNTIME_CONFIG": ""},
        )
        initial = '[discord]\ntoken="local-bot"\n'
        for env in cases:
            with (
                self.subTest(keys=list(env)),
                patch.dict(os.environ, {"DISCORD_BLUE_RUNTIME_CONFIG": "[agent_session]\nenabled=false"} | env, clear=True),
            ):
                self.config_path.write_text(initial)
                with patch("builtins.input", side_effect=AssertionError("Invalid runtime must not prompt")):
                    with self.assertRaises(ValueError) as caught:
                        Config()
                self.assertNotIn("secret-should-not-be-here", str(caught.exception))
                self.assertEqual(self.config_path.read_text(), initial)

    def test_local_interactive_setup_still_works_without_runtime_inputs(self) -> None:
        os.environ.update(DISCORD_TOKEN="unrelated-runtime-value", AGENT_SESSION_TOKEN="local-client-value")
        with patch("builtins.input", return_value="local-input") as prompt:
            config = Config()
        prompt.assert_called_once()
        self.assertEqual(config.discord.token, "local-input")
        self.assertEqual(tomllib.loads(self.config_path.read_text())["discord"]["token"], "local-input")
