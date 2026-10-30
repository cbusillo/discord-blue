import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import tomli_w

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
        self.runtime = {
            "DISCORD_BLUE_GUILD_ID": "123",
            "DISCORD_BLUE_BOT_CHANNEL_ID": "456",
            "DISCORD_BLUE_SPAWNED_CHANNEL_ID": "654",
            "DISCORD_BLUE_AUTO_JOIN_USER_IDS": "[789, 987]",
            "DISCORD_TOKEN": "testing-bot-value",
            "AGENT_SESSION_TOKEN": "testing-bridge-value",
        }

    def test_fresh_runtime_starts_without_prompt_and_does_not_persist_inputs(self) -> None:
        os.environ.update(self.runtime)
        with patch("builtins.input", side_effect=AssertionError("Runtime must not prompt")):
            config = Config()
        self.assertEqual(config.discord.token, os.environ["DISCORD_TOKEN"])
        self.assertEqual(config.agent_session.token, os.environ["AGENT_SESSION_TOKEN"])
        self.assertEqual((config.discord.guild_id, config.discord.bot_channel_id), (123, 456))
        self.assertTrue(config.agent_session.enabled)
        self.assertIn("agent_session_doodad", config.discord.loaded_doodads)
        self.assertEqual(config.agent_session.auto_join_user_ids, [789, 987])
        self.assertEqual(config.agent_session.spawned_channel_id, 654)
        config.debug = True
        config.save()
        stored = tomllib.loads(self.config_path.read_text())
        self.assertTrue(stored["debug"])
        for section in ("discord", "agent_session"):
            self.assertNotIn("token", stored[section])
        self.assertNotIn("guild_id", stored["discord"])
        self.assertNotIn("enabled", stored["agent_session"])
        self.assertNotIn("spawned_channel_id", stored["agent_session"])

    def test_runtime_defaults_override_stale_local_values_without_persisting(self) -> None:
        initial = {
            "discord": {"token": "local-bot", "guild_id": 222, "bot_channel_id": 333, "loaded_doodads": []},
            "agent_session": {
                "token": "local-bridge",
                "enabled": False,
                "channel_id": 999,
                "listen_host": "127.0.0.1",
                "listen_port": 9999,
                "operator_role_name": "Local Admin",
            },
        }
        self.config_path.write_text(tomli_w.dumps(initial))
        os.environ.update(self.runtime)
        config = Config()
        self.assertTrue(config.agent_session.enabled)
        self.assertNotEqual(config.agent_session.listen_host, initial["agent_session"]["listen_host"])
        self.assertNotEqual(config.agent_session.listen_port, initial["agent_session"]["listen_port"])
        self.assertNotEqual(config.agent_session.operator_role_name, initial["agent_session"]["operator_role_name"])
        self.assertEqual(config.agent_session.channel_id, config.discord.bot_channel_id)
        config.debug = True
        config.save()
        os.environ.clear()
        local = Config()
        for section, values in initial.items():
            for field, value in values.items():
                self.assertEqual(getattr(getattr(local, section), field), value)
        self.assertTrue(local.debug)

    def test_missing_required_inputs_refuse_before_prompt_or_save(self) -> None:
        for key in self.runtime:
            for missing in (None, ""):
                env = dict(self.runtime)
                if missing is None:
                    del env[key]
                else:
                    env[key] = missing
                with self.subTest(key=key, missing=missing), patch.dict(os.environ, env, clear=True):
                    self.config_path.write_text("")
                    with patch("builtins.input", side_effect=AssertionError("Managed runtime must not prompt")):
                        with self.assertRaises(ValueError):
                            Config()
                    self.assertEqual(self.config_path.read_text(), "")

    def test_managed_identity_requires_settings_even_with_complete_local_config(self) -> None:
        self.config_path.write_text('[discord]\ntoken="local-bot"\nguild_id=123\nbot_channel_id=456\n')
        os.environ["LAUNCHPLANE_RUNTIME_IDENTITY_JSON"] = '{"product":"fixture"}'
        with self.assertRaises(ValueError):
            Config()

    def test_runtime_credentials_match_client_whitespace_normalization(self) -> None:
        os.environ.update(self.runtime | {"DISCORD_TOKEN": " synthetic-bot \n", "AGENT_SESSION_TOKEN": "\n synthetic-bridge \n"})
        config = Config()
        self.assertEqual(config.discord.token, os.environ["DISCORD_TOKEN"].strip())
        self.assertEqual(config.agent_session.token, os.environ["AGENT_SESSION_TOKEN"].strip())

    def test_invalid_inputs_fail_before_config_is_written_or_prompted(self) -> None:
        cases = (
            {"DISCORD_TOKEN": "from_terminal"},
            {"AGENT_SESSION_TOKEN": "  "},
            {"DISCORD_BLUE_GUILD_ID": "secret-should-not-be-here"},
            {"DISCORD_BLUE_BOT_CHANNEL_ID": "0"},
            {"DISCORD_BLUE_SPAWNED_CHANNEL_ID": "-1"},
            {"DISCORD_BLUE_SPAWNED_CHANNEL_ID": self.runtime["DISCORD_BLUE_BOT_CHANNEL_ID"]},
            *({"DISCORD_BLUE_AUTO_JOIN_USER_IDS": value} for value in ('["wrong-type"]', "[true]", "[-1]", "0", "{}", "[", "[0]")),
        )
        initial = '[discord]\ntoken="local-bot"\n'
        for env in cases:
            with self.subTest(keys=list(env)), patch.dict(os.environ, self.runtime | env, clear=True):
                self.config_path.write_text(initial)
                with patch("builtins.input", side_effect=AssertionError("Invalid runtime must not prompt")):
                    with self.assertRaises(ValueError) as caught:
                        Config()
                self.assertNotIn("secret-should-not-be-here", str(caught.exception))
                self.assertEqual(self.config_path.read_text(), initial)

    def test_explicit_empty_auto_join_list_is_valid(self) -> None:
        os.environ.update(self.runtime | {"DISCORD_BLUE_AUTO_JOIN_USER_IDS": "[]"})
        self.assertEqual(Config().agent_session.auto_join_user_ids, [])

    def test_local_interactive_setup_still_works_without_runtime_settings(self) -> None:
        os.environ.update(DISCORD_TOKEN="unrelated-runtime-value", AGENT_SESSION_TOKEN="local-client-value")
        with patch("builtins.input", return_value="local-input") as prompt:
            config = Config()
        prompt.assert_called_once()
        self.assertEqual(config.discord.token, "local-input")
        self.assertEqual(tomllib.loads(self.config_path.read_text())["discord"]["token"], "local-input")
