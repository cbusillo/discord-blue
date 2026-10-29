import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest


PROCESS = """
import json
import os
from pathlib import Path
import signal
import sys
from typing import NoReturn
from unittest.mock import patch
from discord_blue import __main__ as entry

folder, role, descriptor = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])

def config() -> NoReturn:
    if role == "old":
        (folder / "old-config").touch()
        while True:
            signal.pause()
    try:
        exited = os.read(descriptor, 1) == b""
    except BlockingIOError:
        exited = False
    (folder / "new-config").write_text(json.dumps({"predecessor_exited": exited}))
    raise SystemExit(0)

with patch("pathlib.Path.home", return_value=folder), patch.object(entry, "get_config", config):
    (folder / (role + "-entered")).touch()
    entry.start_bot()
"""


class ProcessHandoverTests(unittest.TestCase):
    def start_process(self, folder: Path, role: str, descriptor: int) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            [sys.executable, "-c", PROCESS, str(folder), role, str(descriptor)],
            pass_fds=(descriptor,),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self.stop, process)
        return process

    def wait_for(self, path: Path) -> None:
        deadline = time.monotonic() + 10
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), path.name)

    def test_successor_cannot_read_or_write_configuration_before_predecessor_exits(self) -> None:
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            reader, writer = os.pipe()
            self.addCleanup(os.close, reader)
            os.set_blocking(reader, False)
            old = self.start_process(folder, "old", writer)
            os.close(writer)
            self.wait_for(folder / "old-config")
            new = self.start_process(folder, "new", reader)
            self.wait_for(folder / "new-entered")
            time.sleep(0.1)
            self.assertIsNone(new.poll(), "Successor reached configuration while predecessor lived")
            self.stop(old)
            self.assertEqual(new.wait(timeout=10), 0)
            self.assertTrue(json.loads((folder / "new-config").read_text())["predecessor_exited"])

    def test_standby_terminates_without_waiting_for_predecessor_or_loading_config(self) -> None:
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            reader, writer = os.pipe()
            self.addCleanup(os.close, reader)
            old = self.start_process(folder, "old", writer)
            os.close(writer)
            self.wait_for(folder / "old-config")
            new = self.start_process(folder, "new", reader)
            self.wait_for(folder / "new-entered")
            new.terminate()
            new.communicate(timeout=2)
            self.assertFalse((folder / "new-config").exists())
            self.assertIsNone(old.poll())

    @staticmethod
    def stop(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=10)
