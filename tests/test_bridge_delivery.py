from __future__ import annotations

import json
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from typing import Any, cast

from scripts import deliver_bridges


class BridgeDeliveryTests(unittest.TestCase):
    def test_delivery_switches_both_launches_and_preserves_running_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home, source = base / "home", base / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "Fixture"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "fixture@example.invalid"], check=True)
            package = source / "discord_blue"
            package.mkdir()
            (package / "bridge.py").write_text("first release")
            for args in (
                ["add", "."],
                ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "first"],
            ):
                subprocess.run(["git", "-C", str(source), *args], check=True)
            plist = home / "Library/LaunchAgents" / f"{deliver_bridges.LABEL}.plist"
            plist.parent.mkdir(parents=True)
            job: dict[str, Any] = {
                "Label": deliver_bridges.LABEL,
                "ProgramArguments": ["/old/discord-blue-codex-bridge", "--config", "/private/config"],
                "EnvironmentVariables": {"PATH": "/tools"},
                "KeepAlive": True,
            }
            plist.write_bytes(plistlib.dumps(job))
            calls: list[list[str]] = []
            real_run = subprocess.run

            def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if args[0] == "launchctl":
                    calls.append(args)
                    return subprocess.CompletedProcess(args, 0, "", "")
                if args[:2] == ["uv", "sync"]:
                    snapshot = Path(args[args.index("--project") + 1])
                    revision = json.loads((snapshot / "discord_blue/bridge_release.json").read_text())["revision"]
                    venv = Path(cast(dict[str, str], kwargs["env"])["UV_PROJECT_ENVIRONMENT"])
                    for name in deliver_bridges.BRIDGES:
                        command = venv / "bin" / name
                        command.parent.mkdir(parents=True, exist_ok=True)
                        command.write_text(f"#!{sys.executable}\nprint({revision!r})\n")
                        command.chmod(0o700)
                    return subprocess.CompletedProcess(args, 0, "", "")
                return cast(subprocess.CompletedProcess[str], real_run(args, **cast(Any, kwargs)))

            with patch.object(deliver_bridges.subprocess, "run", side_effect=run):
                first = deliver_bridges.deliver(source, home, base / "builds")
                previous = (home / ".local/bin/discord-blue-claude-channel").resolve()
                (package / "bridge.py").write_text("label-carrying release")
                subprocess.run(["git", "-C", str(source), "commit", "-qam", "labels"], check=True)
                second = deliver_bridges.deliver(source, home, base / "builds")
                for name in deliver_bridges.BRIDGES:
                    command = home / ".local/bin" / name
                    self.assertEqual(subprocess.check_output([str(command), "--version"], text=True).strip(), second)
                self.assertEqual(subprocess.check_output([str(previous), "--version"], text=True).strip(), first)
                updated = plistlib.loads(plist.read_bytes())
                self.assertEqual(updated["ProgramArguments"][1:], job["ProgramArguments"][1:])
                self.assertEqual(updated["EnvironmentVariables"], job["EnvironmentVariables"])
                self.assertEqual(
                    Path(updated["ProgramArguments"][0]).resolve(), (home / ".local/bin/discord-blue-codex-bridge").resolve()
                )
                self.assertEqual(sum(call[1] == "bootstrap" for call in calls), 2)
                deliver_bridges.deliver(source, home, base / "builds")
                self.assertEqual(sum(call[1] == "bootstrap" for call in calls), 2)
                before = plist.read_bytes()
                with patch.object(deliver_bridges, "prepare_release", side_effect=RuntimeError("build failed")):
                    with self.assertRaisesRegex(RuntimeError, "build failed"):
                        deliver_bridges.deliver(source, home, base / "builds")
                self.assertEqual(plist.read_bytes(), before)
                self.assertEqual(subprocess.check_output([str(previous), "--version"], text=True).strip(), first)

    def test_dirty_source_and_unrecognized_login_item_refuse_before_installing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            (source / "tracked").write_text("committed")
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                check=True,
            )
            (source / "tracked").write_text("uncommitted")
            with self.assertRaisesRegex(ValueError, "clean"):
                deliver_bridges.source_revision(source)
            with patch.object(deliver_bridges, "prepare_release") as prepare:
                with self.assertRaises((ValueError, FileNotFoundError)):
                    deliver_bridges.deliver(source, base / "home", base / "builds")
                prepare.assert_not_called()
