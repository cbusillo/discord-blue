from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from discord_blue.codex_bridge import config as bridge_config
from discord_blue.release_delivery import BRIDGE_LABEL, BRIDGES, HOOK_PATH, STARTUP_RECEIPT, replace_link, save_json

LABEL = "com.shinycomputers.discord-blue-delivery"


class DeliveryFailure(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def git(checkout: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(checkout), *arguments], text=True, stderr=subprocess.DEVNULL, timeout=60
    ).strip()


def job_pid() -> int:
    output = subprocess.check_output(["launchctl", "print", f"gui/{os.getuid()}/{BRIDGE_LABEL}"], text=True, timeout=30)
    match = re.search(r"^\s*pid = (\d+)\s*$", output, re.MULTILINE)
    if match is None:
        raise ValueError("Bridge startup is unverified")
    return int(match[1])


def verify_launchers(home: Path, target: dict[str, str]) -> None:
    for bridge in BRIDGES:
        output = subprocess.check_output([str(home / ".local/bin" / bridge), "--version"], text=True, timeout=30).split()
        if not output or output[-1] != target["revision"]:
            raise ValueError("Launcher source is unverified")


def verify_installed(checkout: Path, home: Path, target: dict[str, str], pid: int) -> None:
    verify_hook_registration(checkout)
    verify_launchers(home, target)
    if hashlib.sha256((checkout / HOOK_PATH).read_bytes()).hexdigest() != target["hooks_sha256"]:
        raise ValueError("In-place hook source is unverified")
    command = subprocess.check_output(["ps", "-ww", "-p", str(pid), "-o", "command="], text=True, timeout=30)
    if str(home / ".local/share/discord-blue-bridges" / target["revision"] / "venv") not in command:
        raise ValueError("Running bridge source is unverified")
    if pid != job_pid():
        raise ValueError("Bridge process changed during verification")
    try:
        startup = read_json(home / STARTUP_RECEIPT)
    except (OSError, ValueError) as error:
        raise DeliveryFailure("startup_unverified") from error
    if startup.get("pid") != pid or startup.get("revision") != target["revision"]:
        raise DeliveryFailure("startup_unverified")


def verify_hook_registration(checkout: Path) -> None:
    plugins = json.loads(subprocess.check_output(["claude", "plugin", "list", "--json"], text=True, timeout=30))
    if not isinstance(plugins, list) or any(not isinstance(plugin, dict) for plugin in plugins):
        raise ValueError("DUI plugin registration is unavailable")
    expected = (checkout / HOOK_PATH).parent.parent.resolve()
    if not any(
        plugin.get("id") == "dui@discord-blue"
        and plugin.get("enabled") is True
        and Path(plugin.get("readFromFolder", "/missing")).resolve() == expected
        for plugin in plugins
    ):
        raise ValueError("DUI is not registered to this in-place hook source")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def advance_automation(home: Path, target: dict[str, str]) -> None:
    release = home / ".local/share/discord-blue-bridges" / target["revision"]
    automation = release.parent / "automation"
    if (release / "source/scripts/reconcile_bridge_delivery.py").is_file() and automation.is_symlink():
        replace_link(automation, release)


def observed_receipt(checkout: Path, home: Path, target: dict[str, str]) -> dict[str, Any]:
    pid = job_pid()
    verify_installed(checkout, home, target, pid)
    return {"status": "delivered", **target, "codex_revision": target["revision"], "codex_pid": pid}


def validate_hook_source(checkout: Path, target: dict[str, str]) -> None:
    if git(checkout, "status", "--porcelain", "--untracked-files=no") or git(checkout, "branch", "--show-current") not in {
        "main",
        "",
    }:
        raise ValueError("In-place source requires a clean main or managed release checkout")
    git(checkout, "fetch", "origin", "main")
    git(checkout, "merge-base", "--is-ancestor", target["revision"], "origin/main")
    git(checkout, "merge-base", "--is-ancestor", "HEAD", "origin/main")
    hooks = subprocess.check_output(["git", "-C", str(checkout), "show", f"{target['revision']}:{HOOK_PATH}"], timeout=30)
    if hashlib.sha256(hooks).hexdigest() != target["hooks_sha256"]:
        raise ValueError("Released hook source disagrees with production")


def select_hook_source(checkout: Path, target: dict[str, str]) -> None:
    try:
        validate_hook_source(checkout, target)
        if git(checkout, "rev-parse", "HEAD") != target["revision"]:
            git(checkout, "switch", "--detach", target["revision"])
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise DeliveryFailure("source_unavailable") from error


def verify_installer_idle(home: Path) -> None:
    try:
        with (home / ".local/share/discord-blue-bridges/delivery.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        raise DeliveryFailure("startup_unverified") from error


def deliver_target(
    registration: dict[str, Any], target: dict[str, str], attempt_path: Path | None = None
) -> tuple[dict[str, Any], bool]:
    checkout, home = Path(registration["checkout"]), Path(registration["home"])
    if re.fullmatch(r"[0-9a-f]{40}", target["revision"]) is None or re.fullmatch(r"[0-9a-f]{64}", target["hooks_sha256"]) is None:
        raise ValueError("Invalid release target")
    try:
        verify_hook_registration(checkout)
        validate_hook_source(checkout, target)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise DeliveryFailure("source_unavailable") from error
    try:
        verify_launchers(home, target)
        select_hook_source(checkout, target)
        return observed_receipt(checkout, home, target), False
    except DeliveryFailure:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    if attempt_path is not None:
        attempt = read_json(attempt_path) if attempt_path.exists() else {}
        if attempt.get("revision") == target["revision"]:
            if attempt.get("outcome") not in {"failed", "started"}:
                raise DeliveryFailure("startup_unverified")
            if time.time() - attempt.get("started_at", 0) < 900:
                raise DeliveryFailure("delivery_failed")
            if attempt["outcome"] == "started":
                verify_installer_idle(home)
        save_json(attempt_path, {"revision": target["revision"], "started_at": time.time(), "outcome": "started"})
    command = [
        sys.executable,
        str(Path(__file__).with_name("deliver_bridges.py")),
        "--checkout",
        str(checkout),
        "--home",
        str(home),
        "--build-dir",
        registration["build_dir"],
        "--revision",
        target["revision"],
    ]
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        if attempt_path is not None:
            save_json(attempt_path, {"revision": target["revision"], "started_at": time.time(), "outcome": "failed"})
        raise
    if attempt_path is not None:
        save_json(attempt_path, {"revision": target["revision"], "started_at": time.time(), "outcome": "succeeded"})
    select_hook_source(checkout, target)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            return observed_receipt(checkout, home, target), True
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        time.sleep(1)
    raise DeliveryFailure("startup_unverified")


async def reconcile(registration: dict[str, Any], receipt_path: Path) -> dict[str, Any]:
    config = bridge_config.load_config(Path(registration["bridge_config"]))
    parsed = urlsplit(config.server_url)
    url = urlunsplit(("https" if parsed.scheme == "wss" else "http", parsed.netloc, "/agent-session/delivery", "", ""))
    timeout = aiohttp.ClientTimeout(total=30)
    installed = False
    async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {config.token}"}, timeout=timeout) as client:
        async with client.get(url, allow_redirects=False) as response:
            response.raise_for_status()
            if response.status != 200:
                raise ValueError("Production delivery target is unavailable")
            target = (await response.json())["target"]
        try:
            receipt = read_json(receipt_path)
            if receipt.get("status") != "delivered" or any(receipt.get(key) != value for key, value in target.items()):
                raise ValueError("Delivery is pending")
            receipt = await asyncio.to_thread(observed_receipt, Path(registration["checkout"]), Path(registration["home"]), target)
            save_json(receipt_path, receipt)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            receipt = {"status": "pending", **target}
            save_json(receipt_path, receipt)
            async with client.post(
                url, json={"host_id": registration["host_id"], "receipt": receipt}, allow_redirects=False
            ) as response:
                response.raise_for_status()
                if response.status != 200:
                    raise ValueError("Delivery registration is unavailable") from None
            try:
                receipt, installed = await asyncio.to_thread(
                    deliver_target, registration, target, receipt_path.with_suffix(".attempt.json")
                )
            except DeliveryFailure as error:
                receipt["error"] = error.reason
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                receipt["error"] = "delivery_failed"
            save_json(receipt_path, receipt)
        async with client.post(
            url, json={"host_id": registration["host_id"], "receipt": receipt}, allow_redirects=False
        ) as response:
            response.raise_for_status()
            result = await response.json()
            registered = result["clients"][registration["host_id"]]
            if registered["status"] != "delivered":
                raise ValueError("Mac delivery remains pending")
        if installed:
            advance_automation(Path(registration["home"]), target)
        return receipt


def register(path: Path, checkout: Path, home: Path, build_dir: Path, bridge_config_path: Path) -> None:
    destination = {
        "checkout": str(checkout),
        "home": str(home),
        "build_dir": str(build_dir),
        "bridge_config": str(bridge_config_path),
    }
    registration = read_json(path) if path.exists() else {"host_id": str(uuid.uuid4()), **destination}
    if any(registration.get(key) != value for key, value in destination.items()):
        raise ValueError("Preserve the existing Mac registration")
    verify_hook_registration(checkout)
    if "runtime" not in registration:
        uv = shutil.which("uv")
        if uv is None:
            raise ValueError("uv is unavailable")
        prepared = json.loads(
            subprocess.check_output(
                [
                    uv,
                    "run",
                    "--project",
                    str(checkout),
                    "python",
                    str(checkout / "scripts/deliver_bridges.py"),
                    "--checkout",
                    str(checkout),
                    "--home",
                    str(home),
                    "--build-dir",
                    str(build_dir),
                    "--prepare-only",
                ],
                text=True,
            )
        )
        registration.update({"runtime": prepared["runtime"], "source": prepared["source"]})
    automation = home / ".local/share/discord-blue-bridges/automation"
    job = {
        "Label": LABEL,
        "ProgramArguments": [
            str(automation / "venv/bin/python"),
            str(automation / "source/scripts/reconcile_bridge_delivery.py"),
            "--registration",
            str(path),
        ],
        "RunAtLoad": True,
        "StartInterval": 60,
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        "StandardOutPath": str(path.parent / "reconciliation.log"),
        "StandardErrorPath": str(path.parent / "reconciliation.log"),
    }
    plist = home / "Library/LaunchAgents" / f"{LABEL}.plist"
    if plist.exists():
        existing = plistlib.loads(plist.read_bytes())
        arguments = existing.get("ProgramArguments", [])
        if existing.get("Label") != LABEL or arguments != job["ProgramArguments"]:
            raise ValueError("Preserve the existing delivery login item")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        save_json(path, registration)
    automation.parent.mkdir(parents=True, exist_ok=True)
    if automation.exists() and not automation.is_symlink():
        raise ValueError("Preserve the existing automation path")
    replace_link(automation, Path(registration["runtime"]).parent)
    if not plist.exists():
        plist.write_bytes(plistlib.dumps(job))
    loaded = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, timeout=30)
    if loaded.returncode == 113:
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], check=True, timeout=30)
    else:
        loaded.check_returncode()


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile the registered Mac with Discord Blue's gated production source.")
    parser.add_argument("--registration", type=Path, required=True)
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--checkout", type=Path)
    parser.add_argument("--build-dir", type=Path)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--bridge-config", type=Path, default=Path.home() / ".config/discord-blue/codex-bridge.toml")
    args = parser.parse_args()
    try:
        if args.register:
            checkout, build_dir = args.checkout, args.build_dir
            if isinstance(checkout, Path) and isinstance(build_dir, Path):
                register(
                    args.registration.resolve(),
                    checkout.resolve(),
                    args.home.resolve(),
                    build_dir.resolve(),
                    args.bridge_config.resolve(),
                )
            else:
                parser.error("Registration requires --checkout and --build-dir")
            print(json.dumps({"status": "pending", "reason": "registered; awaiting reconciliation"}))
            return
        with args.registration.with_suffix(".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            receipt = asyncio.run(reconcile(read_json(args.registration), args.registration.with_suffix(".receipt.json")))
        print(json.dumps(receipt))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, aiohttp.ClientError) as error:
        parser.exit(1, f"Mac delivery pending ({type(error).__name__}); retained for automatic reconciliation.\n")


if __name__ == "__main__":
    main()
