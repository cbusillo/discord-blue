from __future__ import annotations

import argparse
import fcntl
import json
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from discord_blue.release_delivery import BRIDGES
from discord_blue.release_delivery import BRIDGE_LABEL as LABEL
from discord_blue.release_delivery import replace_bytes, replace_link


def source_revision(checkout: Path, requested_revision: str | None = None) -> str:
    dirty = subprocess.check_output(
        ["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=no"], text=True, timeout=30
    )
    if dirty:
        raise ValueError("Bridge delivery requires a clean committed checkout")
    subprocess.run(["git", "-C", str(checkout), "fetch", "origin", "main"], check=True, capture_output=True, timeout=60)
    if requested_revision is not None:
        if re.fullmatch(r"[0-9a-f]{40}", requested_revision) is None:
            raise ValueError("Bridge delivery revision must be an exact full commit ID")
        revision = requested_revision
        subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "--verify", f"{revision}^{{commit}}"],
            check=True,
            capture_output=True,
            timeout=30,
        )
    else:
        revision = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True, timeout=30).strip()
    subprocess.run(["git", "-C", str(checkout), "merge-base", "--is-ancestor", revision, "origin/main"], check=True, timeout=30)
    return revision


def verify_release(binaries: Path, revision: str) -> None:
    for name in BRIDGES:
        output = subprocess.check_output([str(binaries / name), "--version"], text=True, timeout=30).strip()
        if output.split()[-1] != revision:
            raise ValueError(f"{name} does not report the selected source revision")


def prepare_release(checkout: Path, home: Path, build_dir: Path, requested_revision: str | None = None) -> tuple[Path, str]:
    revision = source_revision(checkout, requested_revision)
    release = home / ".local/share/discord-blue-bridges" / revision
    cached = (release / "complete").is_file()
    if cached:
        verify_release(release / "venv/bin", revision)
        if (release / "source").is_dir():
            return release / "venv", revision
    build_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bridge-", dir=build_dir) as directory:
        build = Path(directory)
        archive = build / "source.zip"
        subprocess.run(
            ["git", "-C", str(checkout), "archive", "--format=zip", f"--output={archive}", revision], check=True, timeout=60
        )
        snapshot = build / "source"
        with zipfile.ZipFile(archive) as source:
            source.extractall(snapshot)
        (snapshot / "discord_blue/bridge_release.json").write_text(json.dumps({"revision": revision}))
        if not cached:
            subprocess.run(
                ["uv", "sync", "--frozen", "--no-dev", "--no-editable", "--project", str(snapshot)],
                env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(release / "venv")},
                check=True,
                timeout=600,
            )
        if not (release / "source").is_dir():
            with tempfile.TemporaryDirectory(prefix=".source-", dir=release) as publishing:
                prepared = Path(publishing) / "source"
                shutil.copytree(snapshot, prepared)
                prepared.rename(release / "source")
    verify_release(release / "venv/bin", revision)
    (release / "complete").touch()
    return release / "venv", revision


def deliver(checkout: Path, home: Path, build_dir: Path, requested_revision: str | None = None) -> str:
    plist = home / "Library/LaunchAgents" / f"{LABEL}.plist"
    job = plistlib.loads(plist.read_bytes())
    args = job.get("ProgramArguments", [])
    if job.get("Label") != LABEL or not args or Path(args[0]).name != BRIDGES[0] or "Program" in job:
        raise ValueError("Unrecognized Codex bridge login item; preserve it and review its launch configuration")
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{LABEL}"
    loaded = subprocess.run(["launchctl", "print", service], capture_output=True, text=True, timeout=30)
    if loaded.returncode not in (0, 113):
        loaded.check_returncode()
    release, revision = prepare_release(checkout, home, build_dir, requested_revision)
    binaries = home / ".local/bin"
    binaries.mkdir(parents=True, exist_ok=True)
    for name in BRIDGES:
        replace_link(binaries / name, release / "bin" / name)
    launcher = str(binaries / BRIDGES[0])
    if args[0] != launcher:
        job["ProgramArguments"] = [launcher, *args[1:]]
        replace_bytes(plist, plistlib.dumps(job))
    if loaded.returncode == 0:
        subprocess.run(["launchctl", "bootout", "--wait", service], check=True, capture_output=True, timeout=30)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True, capture_output=True, timeout=30)
    verify_release(binaries, revision)
    return revision


def main() -> None:
    parser = argparse.ArgumentParser(description="Deliver one committed release to both local DUI bridges.")
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--build-dir", type=Path, required=True, help="Disposable build directory on the host's artifact volume")
    parser.add_argument("--revision", help="Exact full commit ID to deliver from origin/main")
    parser.add_argument(
        "--prepare-only", action="store_true", help="Prepare the immutable runtime without changing launchers or jobs"
    )
    args = parser.parse_args()
    state = args.home / ".local/share/discord-blue-bridges"
    state.mkdir(parents=True, exist_ok=True)
    try:
        with (state / "delivery.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.prepare_only:
                runtime, revision = prepare_release(
                    args.checkout.resolve(), args.home.resolve(), args.build_dir.resolve(), args.revision
                )
                print(json.dumps({"revision": revision, "runtime": str(runtime), "source": str(runtime.parent / "source")}))
                return
            revision = deliver(args.checkout.resolve(), args.home.resolve(), args.build_dir.resolve(), args.revision)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(
            1,
            f"Bridge delivery incomplete ({type(exc).__name__}): {exc}. "
            "Inspect the launch item and retained release before retrying.\n",
        )
    print(json.dumps({"revision": revision, "claude": "next session launch", "codex": "reloaded from disk; startup unverified"}))


if __name__ == "__main__":
    main()
