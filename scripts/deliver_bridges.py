from __future__ import annotations

import argparse
import fcntl
import json
import os
import plistlib
import subprocess
import tempfile
import zipfile
from pathlib import Path

LABEL = "com.shinycomputers.discord-blue-codex-bridge"
BRIDGES = ("discord-blue-codex-bridge", "discord-blue-claude-channel")


def source_revision(checkout: Path) -> str:
    dirty = subprocess.check_output(["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=no"], text=True)
    if dirty:
        raise ValueError("Bridge delivery requires a clean committed checkout")
    subprocess.run(["git", "-C", str(checkout), "fetch", "origin", "main"], check=True, capture_output=True)
    revision = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", str(checkout), "merge-base", "--is-ancestor", revision, "origin/main"], check=True)
    return revision


def verify_release(binaries: Path, revision: str) -> None:
    for name in BRIDGES:
        output = subprocess.check_output([str(binaries / name), "--version"], text=True).strip()
        if output.split()[-1] != revision:
            raise ValueError(f"{name} does not report the selected source revision")


def prepare_release(checkout: Path, home: Path, build_dir: Path) -> tuple[Path, str]:
    revision = source_revision(checkout)
    release = home / ".local/share/discord-blue-bridges" / revision
    if (release / "complete").is_file():
        verify_release(release / "venv/bin", revision)
        return release / "venv", revision
    build_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bridge-", dir=build_dir) as directory:
        build = Path(directory)
        archive = build / "source.zip"
        subprocess.run(["git", "-C", str(checkout), "archive", "--format=zip", f"--output={archive}", revision], check=True)
        snapshot = build / "source"
        with zipfile.ZipFile(archive) as source:
            source.extractall(snapshot)
        (snapshot / "discord_blue/bridge_release.json").write_text(json.dumps({"revision": revision}))
        subprocess.run(
            ["uv", "sync", "--frozen", "--no-dev", "--no-editable", "--project", str(snapshot)],
            env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(release / "venv")},
            check=True,
        )
    verify_release(release / "venv/bin", revision)
    (release / "complete").touch()
    return release / "venv", revision


def replace_bytes(path: Path, content: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as target:
        temporary = Path(target.name)
        try:
            target.write(content)
            target.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def replace_link(path: Path, target: Path) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    try:
        temporary.symlink_to(target)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def deliver(checkout: Path, home: Path, build_dir: Path) -> tuple[str, int]:
    plist = home / "Library/LaunchAgents" / f"{LABEL}.plist"
    job = plistlib.loads(plist.read_bytes())
    args = job.get("ProgramArguments", [])
    if job.get("Label") != LABEL or not args or Path(args[0]).name != BRIDGES[0] or "Program" in job:
        raise ValueError("Unrecognized Codex bridge login item; preserve it and review its launch configuration")
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{LABEL}"
    loaded = subprocess.run(["launchctl", "print", service], capture_output=True, text=True, timeout=30)
    if loaded.returncode and "Could not find service" not in loaded.stderr:
        loaded.check_returncode()
    release, revision = prepare_release(checkout, home, build_dir)
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
        loaded = subprocess.CompletedProcess([], 113)
    if loaded.returncode:
        subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True, capture_output=True, timeout=30)
    pid = subprocess.check_output(["launchctl", "kickstart", "-kp", service], text=True, timeout=30).strip()
    verify_release(binaries, revision)
    return revision, int(pid)


def main() -> None:
    parser = argparse.ArgumentParser(description="Deliver one committed release to both local DUI bridges.")
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--build-dir", type=Path, required=True, help="Disposable build directory on the host's artifact volume")
    args = parser.parse_args()
    state = args.home / ".local/share/discord-blue-bridges"
    state.mkdir(parents=True, exist_ok=True)
    try:
        with (state / "delivery.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            revision, pid = deliver(args.checkout.resolve(), args.home.resolve(), args.build_dir.resolve())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(
            1, f"Bridge delivery incomplete ({type(exc).__name__}); inspect the launch item and retained release before retrying.\n"
        )
    print(
        json.dumps(
            {"revision": revision, "claude": "next session launch", "codex_pid": pid, "codex": "reload requested; verify startup"}
        )
    )


if __name__ == "__main__":
    main()
