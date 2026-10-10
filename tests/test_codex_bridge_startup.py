from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from discord_blue.codex_bridge.__main__ import main, run_bridges
from discord_blue.codex_bridge.accounts import supervisor_homes
from discord_blue.codex_bridge.config import BridgeConfig, socket_for_home
from discord_blue.codex_bridge.daemon import start_daemon


class SupervisorAccountTests(unittest.TestCase):
    def test_cli_uses_supervisor_homes_instead_of_the_configured_socket(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            source, bridge_config = base / "supervisor.toml", base / "bridge.toml"
            source.write_text(
                f'[[accounts.account]]\nprovider = "openai"\n[accounts.account.env]\nCODEX_HOME = "{base / "account"}"\n'
            )
            bridge_config.write_text('server_url = "wss://bridge.example/agent-session/connect"\nsocket_path = "/old.sock"\n')
            mirror = AsyncMock()
            with (
                patch.dict(os.environ, {"AGENT_SESSION_TOKEN": "synthetic"}),
                patch.object(sys, "argv", ["bridge", "--config", str(bridge_config), "--supervisor-config", str(source)]),
                patch("discord_blue.codex_bridge.__main__.run_bridges", mirror),
                patch("discord_blue.codex_bridge.__main__.logging.basicConfig"),
            ):
                main()
            assert mirror.await_args is not None
            self.assertEqual(mirror.await_args.args[1], [base / "account"])

    def test_source_changes_add_and_remove_watched_homes_and_aliases_are_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            alias = base / "alias"
            alias.symlink_to(base / "first", target_is_directory=True)
            source = base / "supervisor.toml"

            def write(*homes: Path) -> None:
                source.write_text(
                    '[[accounts.account]]\nprovider = "anthropic"\n'
                    + "".join(
                        f'[[accounts.account]]\nprovider = "openai"\n[accounts.account.env]\nCODEX_HOME = "{h}"\n' for h in homes
                    )
                )

            write(base / "first", alias, base / "new-account")
            self.assertEqual(supervisor_homes(source), [base / "first", base / "new-account"])
            write(base / "new-account")
            self.assertEqual(supervisor_homes(source), [base / "new-account"])

    def test_expands_home_without_opening_account_files(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"HOME": root}):
            source = Path(root) / "supervisor.toml"
            source.write_text('[[accounts.account]]\nprovider = "openai"\n[accounts.account.env]\nCODEX_HOME = "~/account"\n')
            self.assertEqual(supervisor_homes(source), [(Path(root) / "account").resolve()])
            self.assertFalse((Path(root) / "account").exists())

    def test_invalid_source_fails_instead_of_silently_watching_a_subset_or_default_home(self) -> None:
        for text in (
            "broken = [",
            "",
            '[[accounts.account]]\nprovider = "anthropic"',
            '[[accounts.account]]\nprovider = "openai"',
            '[[accounts.account]]\nprovider = "openai"\n[accounts.account.env]\nCODEX_HOME = " "',
            '[[accounts.account]]\nprovider = "openai"\n[accounts.account.env]\nCODEX_HOME = "relative"',
            '[[accounts.account]]\nprovider = "openai"\n[accounts.account.env]\nCODEX_HOME = 7',
            "accounts = {account = [7]}",
        ):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as root:
                source = Path(root) / "supervisor.toml"
                source.write_text(text)
                with self.assertRaises(ValueError):
                    supervisor_homes(source)


class NativeStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_command_targets_each_home_before_mirroring_and_failure_is_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            recorder = base / "codex"
            record = base / "starts.jsonl"
            recorder.write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys\n"
                f"with open({str(record)!r}, 'a') as f: f.write(json.dumps([os.environ['CODEX_HOME'], sys.argv[1:], "
                "{k: v for k, v in os.environ.items() if k.startswith(('OPENAI_', 'CODEX_', 'AZURE_OPENAI_', 'AGENT_SESSION_'))}])"
                " + '\\n')\n"
                "print('private-provider-data', file=sys.stderr)\n"
                "sys.exit(9 if os.environ['CODEX_HOME'].endswith('/broken') else 0)\n"
            )
            recorder.chmod(0o700)
            mirrored: set[Path] = set()

            async def mirror(config: BridgeConfig) -> None:
                starts = [json.loads(line)[0] for line in record.read_text().splitlines()]
                self.assertIn(str(config.socket_path.parent.parent), starts)
                mirrored.add(config.socket_path)

            homes = [base / "broken", base / "healthy"]
            config = BridgeConfig("ws://localhost/agent-session/connect", "fake", Path("/unused.sock"), "test")
            with (
                patch("discord_blue.codex_bridge.__main__.run_bridge", mirror),
                patch.dict(
                    os.environ,
                    {
                        "CODEX_SQLITE_HOME": str(base / "other-account"),
                        "OPENAI_API_KEY": "synthetic-other-account",
                        "CODEX_API_KEY": "synthetic-other-account",
                        "AZURE_OPENAI_API_KEY": "synthetic-other-account",
                        "OPENAI_BASE_URL": "https://unintended-provider.invalid",
                        "AGENT_SESSION_TOKEN": "synthetic-bridge-secret",
                    },
                ),
                self.assertLogs(level="INFO") as logs,
            ):
                await run_bridges(config, homes, str(recorder))
            self.assertEqual(mirrored, {socket_for_home(h) for h in homes})
            self.assertCountEqual(
                [json.loads(line) for line in record.read_text().splitlines()],
                [[str(h), ["app-server", "daemon", "start"], {"CODEX_HOME": str(h)}] for h in homes],
            )
            self.assertIn("exit 9", "\n".join(logs.output))
            self.assertNotIn("private-provider-data", "\n".join(logs.output))

    async def test_missing_executable_logs_failure_without_ending_reconnect(self) -> None:
        with tempfile.TemporaryDirectory() as root, self.assertLogs(level="ERROR") as logs:
            await start_daemon(Path(root), str(Path(root) / "missing-codex"))
        self.assertIn("FileNotFoundError", "\n".join(logs.output))

    async def test_timeout_reaps_only_the_start_child(self) -> None:
        child: asyncio.subprocess.Process | None = None

        # Capture the real function before patching its shared asyncio module.
        real_spawn = asyncio.create_subprocess_exec

        async def spawn_real(*_args: object, **_kwargs: object) -> asyncio.subprocess.Process:
            nonlocal child
            child = await real_spawn(sys.executable, "-c", "import time; time.sleep(60)")
            return child

        with (
            patch("discord_blue.codex_bridge.daemon.DAEMON_START_TIMEOUT_SECONDS", 0.1),
            patch("discord_blue.codex_bridge.daemon.asyncio.create_subprocess_exec", spawn_real),
            self.assertLogs(level="ERROR") as logs,
        ):
            await start_daemon(Path("/throwaway"), "fake")
        assert child is not None
        self.assertIsNotNone(child.returncode)
        self.assertIn("TimeoutError", "\n".join(logs.output))
