from __future__ import annotations

import contextlib
import copy
import io
import json
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from discord_blue import bridge_version
from discord_blue.claude_channel.__main__ import main as claude_main
from discord_blue.codex_bridge.__main__ import main as codex_main
from scripts import deliver_bridges


class BridgeDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.home, self.source = self.base / "home", self.base / "source"
        self.source.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("remote", "add", "origin", str(self.source))
        self.package = self.source / "discord_blue"
        self.package.mkdir()
        self.tracked = self.package / "bridge.py"
        self.tracked.write_text("first release")
        self.git("add", ".")
        self.git("commit", "-qm", "first")
        self.plist = self.home / "Library/LaunchAgents" / f"{deliver_bridges.LABEL}.plist"
        self.plist.parent.mkdir(parents=True)
        self.job: dict[str, Any] = {
            "Label": deliver_bridges.LABEL,
            "ProgramArguments": [str(self.base / "old" / deliver_bridges.BRIDGES[0]), "--config", "/private/config"],
            "EnvironmentVariables": {"PATH": "/tools"},
            "KeepAlive": True,
        }
        self.plist.write_bytes(plistlib.dumps(self.job))
        old = Path(self.job["ProgramArguments"][0])
        old.parent.mkdir()
        old.write_text(f"#!{sys.executable}\nprint('old-runtime')\n")
        old.chmod(0o700)
        self.loaded_definition: dict[str, Any] | None = copy.deepcopy(self.job)
        self.current_pid = 100
        self.started: list[str] = []
        self.real_run = subprocess.run

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.source), *args], check=True, capture_output=True)

    def run_command(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "launchctl":
            if args[1] == "print":
                return subprocess.CompletedProcess(args, 0 if self.loaded_definition else 113, "", "")
            if args[1] == "bootout":
                self.loaded_definition = None
                self.current_pid = 0
            if args[1] == "bootstrap":
                if self.loaded_definition is not None:
                    raise subprocess.CalledProcessError(5, args)
                self.loaded_definition = copy.deepcopy(plistlib.loads(self.plist.read_bytes()))
                self.start_loaded_job()
            if args[1] == "kickstart":
                if self.loaded_definition is None:
                    raise subprocess.CalledProcessError(113, args)
                if "k" in args[2] or not self.current_pid:
                    self.start_loaded_job()
            return subprocess.CompletedProcess(args, 0, str(self.current_pid), "")
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
        return cast(subprocess.CompletedProcess[str], self.real_run(args, **cast(Any, kwargs)))

    def start_loaded_job(self) -> None:
        assert self.loaded_definition is not None
        launch = Path(self.loaded_definition["ProgramArguments"][0])
        self.started.append(subprocess.check_output([str(launch), "--version"], text=True).strip())
        self.current_pid += 1

    def test_interrupted_publication_reloads_cached_definition_and_later_argument_environment_edits(self) -> None:
        replace = deliver_bridges.replace_bytes

        def interrupt_after_publication(path: Path, content: bytes) -> None:
            replace(path, content)
            raise InterruptedError

        with patch.object(deliver_bridges.subprocess, "run", side_effect=self.run_command):
            with patch.object(deliver_bridges, "replace_bytes", side_effect=interrupt_after_publication):
                with self.assertRaises(InterruptedError):
                    deliver_bridges.deliver(self.source, self.home, self.base / "builds")
            deliver_bridges.deliver(self.source, self.home, self.base / "builds")
            self.assertEqual(self.loaded_definition, plistlib.loads(self.plist.read_bytes()))
            edited = plistlib.loads(self.plist.read_bytes())
            edited["EnvironmentVariables"]["PATH"] = "/updated/tools"
            edited["ProgramArguments"].extend(["--codex-home", "/updated/home"])
            self.plist.write_bytes(plistlib.dumps(edited))
            deliver_bridges.deliver(self.source, self.home, self.base / "builds")
            self.assertEqual(self.loaded_definition, edited)

    def test_delivery_advances_both_launches_and_recovers_an_unloaded_job_without_modifying_previous_runtime(self) -> None:
        with patch.object(deliver_bridges.subprocess, "run", side_effect=self.run_command):
            first = deliver_bridges.deliver(self.source, self.home, self.base / "builds")
            previous = (self.home / ".local/bin/discord-blue-claude-channel").resolve()
            self.tracked.write_text("label-carrying release")
            self.git("commit", "-qam", "labels")
            second = deliver_bridges.deliver(self.source, self.home, self.base / "builds")
            for name in deliver_bridges.BRIDGES:
                command = self.home / ".local/bin" / name
                self.assertEqual(subprocess.check_output([str(command), "--version"], text=True).strip(), second)
            self.assertEqual(subprocess.check_output([str(previous), "--version"], text=True).strip(), first)
            updated = plistlib.loads(self.plist.read_bytes())
            self.assertEqual(updated["ProgramArguments"][1:], self.job["ProgramArguments"][1:])
            self.assertEqual(updated["EnvironmentVariables"], self.job["EnvironmentVariables"])
            self.assertEqual(self.started, [first, second])
            self.loaded_definition = None
            self.current_pid = 0
            deliver_bridges.deliver(self.source, self.home, self.base / "builds")
            self.assertIsNotNone(self.loaded_definition)
            self.assertEqual(self.started[-1], second)
            before = self.plist.read_bytes()
            with patch.object(deliver_bridges, "prepare_release", side_effect=RuntimeError):
                with self.assertRaises(RuntimeError):
                    deliver_bridges.deliver(self.source, self.home, self.base / "builds")
            self.assertEqual(self.plist.read_bytes(), before)
            self.assertEqual(subprocess.check_output([str(previous), "--version"], text=True).strip(), first)

    def test_unrecognized_login_item_refuses_before_install_or_launch_changes(self) -> None:
        self.job["Label"] += "-other"
        self.plist.write_bytes(plistlib.dumps(self.job))
        with patch.object(deliver_bridges, "prepare_release") as prepare, patch.object(deliver_bridges.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                deliver_bridges.deliver(self.source, self.home, self.base / "builds")
            prepare.assert_not_called()
            run.assert_not_called()
        self.assertFalse((self.home / ".local/bin").exists())

    def test_dirty_and_unmerged_source_refuse(self) -> None:
        self.tracked.write_text("uncommitted")
        with self.assertRaises(ValueError):
            deliver_bridges.source_revision(self.source)
        self.git("checkout", "-qb", "work/unreleased")
        self.git("commit", "-qam", "unreleased")
        with self.assertRaises(subprocess.CalledProcessError):
            deliver_bridges.source_revision(self.source)

    def test_requested_revision_is_selected_for_archive(self) -> None:
        selected = subprocess.check_output(["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True).strip()
        self.tracked.write_text("later release")
        self.git("commit", "-qam", "later")
        with patch.object(deliver_bridges.subprocess, "run", side_effect=self.run_command):
            delivered = deliver_bridges.deliver(self.source, self.home, self.base / "builds", selected)
        self.assertEqual(delivered, selected)
        self.assertEqual(self.started, [selected])

    def test_invalid_and_non_main_requested_revisions_refuse(self) -> None:
        with patch.object(deliver_bridges.subprocess, "run", side_effect=self.run_command):
            with self.assertRaises(ValueError):
                deliver_bridges.source_revision(self.source, "HEAD")
        self.git("checkout", "-qb", "work/unreleased")
        self.tracked.write_text("unreleased")
        self.git("commit", "-qam", "unreleased")
        selected = subprocess.check_output(["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True).strip()
        with patch.object(deliver_bridges.subprocess, "run", side_effect=self.run_command):
            with self.assertRaises(subprocess.CalledProcessError):
                deliver_bridges.source_revision(self.source, selected)

    def test_both_real_entrypoints_report_packaged_revision_before_loading_config(self) -> None:
        revision = subprocess.check_output(["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True).strip()
        with patch.object(bridge_version, "__file__", str(self.package / "bridge_version.py")):
            untagged = bridge_version.bridge_version()
            (self.package / "bridge_release.json").write_text(json.dumps({"revision": revision}))
            tagged = bridge_version.bridge_version()
            self.assertNotEqual(untagged, tagged)
            self.assertEqual(tagged.split()[-1], revision)
            for main in (codex_main, claude_main):
                output = io.StringIO()
                with contextlib.redirect_stdout(output), patch.object(sys, "argv", ["bridge", "--version"]):
                    with self.assertRaises(SystemExit) as exit_result:
                        main()
                self.assertEqual(exit_result.exception.code, 0)
                self.assertEqual(output.getvalue().strip(), tagged)
