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
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from discord_blue.codex_bridge import config as bridge_config
from discord_blue.release_delivery import BRIDGE_LABEL, BRIDGES, HOOK_PATH

LABEL = "com.shinycomputers.discord-blue-delivery"


def git(checkout: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", str(checkout), *arguments], text=True, stderr=subprocess.DEVNULL).strip()


def job_pid() -> int:
    output = subprocess.check_output(["launchctl", "print", f"gui/{os.getuid()}/{BRIDGE_LABEL}"], text=True)
    match = re.search(r"^\s*pid = (\d+)\s*$", output, re.MULTILINE)
    if match is None:
        raise ValueError("Bridge startup is unverified")
    return int(match[1])


def verify_installed(checkout: Path, home: Path, target: dict[str, str], pid: int) -> None:
    for bridge in BRIDGES:
        output = subprocess.check_output([str(home / ".local/bin" / bridge), "--version"], text=True).split()
        if not output or output[-1] != target["revision"]:
            raise ValueError("Launcher source is unverified")
    if hashlib.sha256((checkout / HOOK_PATH).read_bytes()).hexdigest() != target["hooks_sha256"]:
        raise ValueError("In-place hook source is unverified")
    command = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True)
    if str(home / ".local/share/discord-blue-bridges" / target["revision"] / "venv") not in command:
        raise ValueError("Running bridge source is unverified")
    if pid != job_pid():
        raise ValueError("Bridge process changed during verification")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def save_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.chmod(0o600)
    temporary.replace(path)


def deliver_target(registration: dict[str, Any], target: dict[str, str]) -> dict[str, Any]:
    checkout, home = Path(registration["checkout"]), Path(registration["home"])
    if re.fullmatch(r"[0-9a-f]{40}", target["revision"]) is None or re.fullmatch(r"[0-9a-f]{64}", target["hooks_sha256"]) is None:
        raise ValueError("Invalid release target")
    if git(checkout, "status", "--porcelain", "--untracked-files=no") or git(checkout, "branch", "--show-current") != "main":
        raise ValueError("In-place source requires a clean main checkout")
    git(checkout, "fetch", "origin", "main")
    git(checkout, "merge-base", "--is-ancestor", target["revision"], "origin/main")
    hooks = subprocess.check_output(["git", "-C", str(checkout), "show", f"{target['revision']}:{HOOK_PATH}"])
    if hashlib.sha256(hooks).hexdigest() != target["hooks_sha256"]:
        raise ValueError("Released hook source disagrees with production")
    git(checkout, "merge-base", "--is-ancestor", "HEAD", target["revision"])
    git(checkout, "merge", "--ff-only", target["revision"])
    plist = plistlib.loads((home / "Library/LaunchAgents" / f"{BRIDGE_LABEL}.plist").read_bytes())
    log = Path(plist["StandardErrorPath"])
    offset = log.stat().st_size if log.exists() else 0
    subprocess.run(
        [
            "uv",
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
            registration["build_dir"],
            "--revision",
            target["revision"],
        ],
        check=True,
        timeout=900,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            pid = job_pid()
            verify_installed(checkout, home, target, pid)
            with log.open("rb") as stream:
                stream.seek(offset)
                lines = stream.read(65536).decode(errors="replace").splitlines()
            if any("Bridge source " in line and line.endswith(f" {target['revision']}") for line in lines):
                return {"status": "delivered", **target, "codex_revision": target["revision"], "codex_pid": pid}
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        time.sleep(1)
    raise ValueError("Bridge startup is unverified")


async def reconcile(registration: dict[str, Any], receipt_path: Path) -> dict[str, Any]:
    config = bridge_config.load_config(Path(registration["bridge_config"]))
    parsed = urlsplit(config.server_url)
    url = urlunsplit(("https" if parsed.scheme == "wss" else "http", parsed.netloc, "/agent-session/delivery", "", ""))
    timeout = aiohttp.ClientTimeout(total=30)
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
            await asyncio.to_thread(
                verify_installed, Path(registration["checkout"]), Path(registration["home"]), target, receipt["codex_pid"]
            )
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
                receipt = await asyncio.to_thread(deliver_target, registration, target)
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
        return receipt


def register(path: Path, checkout: Path, home: Path, build_dir: Path, bridge_config: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise ValueError("uv is unavailable")
    destination = {"checkout": str(checkout), "home": str(home), "build_dir": str(build_dir), "bridge_config": str(bridge_config)}
    registration = read_json(path) if path.exists() else {"host_id": str(uuid.uuid4()), **destination}
    if any(registration.get(key) != value for key, value in destination.items()):
        raise ValueError("Preserve the existing Mac registration")
    job = {
        "Label": LABEL,
        "ProgramArguments": [
            uv,
            "run",
            "--project",
            str(checkout),
            "python",
            str(checkout / "scripts/reconcile_bridge_delivery.py"),
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
    if plist.exists() and plistlib.loads(plist.read_bytes()) != job:
        raise ValueError("Preserve the existing delivery login item")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        save_json(path, registration)
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
            if args.checkout is None or args.build_dir is None:
                parser.error("Registration requires --checkout and --build-dir")
            register(
                args.registration.resolve(),
                Path(args.checkout).resolve(),
                args.home.resolve(),
                Path(args.build_dir).resolve(),
                args.bridge_config.resolve(),
            )
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
