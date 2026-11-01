import argparse
import hashlib
import json
import os
import stat
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

import tomli_w


MAX_CONFIG_BYTES = 1024 * 1024
STATE_FORMAT = "discord-blue-testing-state-v1"


@dataclass(frozen=True)
class StateCopyReceipt:
    sha256: str
    byte_count: int
    format: str = STATE_FORMAT


@contextmanager
def open_directory(path: Path) -> Iterator[int]:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("An absolute state directory without parent traversal is required.")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    except OSError as error:
        raise ValueError("State directories must be directly accessible without symbolic links.") from error
    finally:
        os.close(descriptor)


@contextmanager
def config_directory(root: int, *, create: bool = False) -> Iterator[int]:
    descriptors = []
    parent = root
    try:
        for name in (".config", "discord-blue"):
            if create:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            parent = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            descriptors.append(parent)
        yield parent
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def read_config(root: int, *, optional: bool) -> bytes:
    try:
        with config_directory(root) as directory:
            descriptor = os.open("config.toml", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_CONFIG_BYTES:
                    raise ValueError("State configuration must be a bounded regular file.")
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    content = stream.read(MAX_CONFIG_BYTES + 1)
                after = os.fstat(descriptor)
                if len(content) > MAX_CONFIG_BYTES or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError("State configuration changed during inspection.")
                return content
            finally:
                os.close(descriptor)
    except FileNotFoundError:
        if optional:
            return b""
        raise ValueError("The sanitized state configuration is missing.") from None
    except OSError as error:
        raise ValueError("State configuration must be directly accessible without symbolic links.") from error


def safe_preferences(content: bytes) -> dict[str, object]:
    try:
        data = tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise ValueError("State configuration is not valid UTF-8 TOML.") from None
    preferences: dict[str, object] = {}
    if "debug" in data:
        if type(data["debug"]) is not bool:
            raise ValueError("The debug preference must be boolean.")
        preferences["debug"] = data["debug"]
    agent = data.get("agent_session", {})
    if not isinstance(agent, dict):
        raise ValueError("Agent preferences must be a table.")
    timing: dict[str, int] = {}
    for key in ("heartbeat_timeout_seconds", "heartbeat_check_interval_seconds"):
        if key not in agent:
            continue
        value = agent[key]
        if type(value) is not int or value <= 0:
            raise ValueError("Heartbeat preferences must be positive integers.")
        timing[key] = value
    if timing:
        preferences["agent_session"] = timing
    return preferences


def receipt(content: bytes) -> StateCopyReceipt:
    return StateCopyReceipt(hashlib.sha256(content).hexdigest(), len(content))


def plan_testing_state(source: Path) -> StateCopyReceipt:
    with open_directory(source) as directory:
        return receipt(tomli_w.dumps(safe_preferences(read_config(directory, optional=True))).encode())


def require_empty_destination(root: int) -> None:
    if not os.listdir(root):
        return
    if os.listdir(root) != [".config"]:
        raise ValueError("The testing state destination contains existing data.")
    with config_directory(root) as directory:
        if os.listdir(directory):
            raise ValueError("The testing state destination contains existing data.")
    descriptor = os.open(".config", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
    try:
        if os.listdir(descriptor) != ["discord-blue"]:
            raise ValueError("The testing state destination contains existing data.")
    finally:
        os.close(descriptor)


def inspect_testing_state(destination: Path) -> StateCopyReceipt:
    with open_directory(destination) as root:
        if os.listdir(root) != [".config"]:
            raise ValueError("Sanitized state contains unexpected files.")
        with config_directory(root) as directory:
            if os.listdir(directory) != ["config.toml"]:
                raise ValueError("Sanitized state contains unexpected files.")
        descriptor = os.open(".config", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            if os.listdir(descriptor) != ["discord-blue"]:
                raise ValueError("Sanitized state contains unexpected files.")
        finally:
            os.close(descriptor)
        content = read_config(root, optional=False)
        safe = safe_preferences(content)
        if tomllib.loads(content.decode()) != safe:
            raise ValueError("Sanitized state contains unsafe configuration.")
        return receipt(content)


def copy_testing_state(source: Path, destination: Path, expected_sha256: str) -> StateCopyReceipt:
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("Source and destination must be separate state directories.")
    with open_directory(source) as original, open_directory(destination) as target:
        source_stat, target_stat = os.fstat(original), os.fstat(target)
        if (source_stat.st_dev, source_stat.st_ino) == (target_stat.st_dev, target_stat.st_ino):
            raise ValueError("Source and destination must be separate state directories.")
        content = tomli_w.dumps(safe_preferences(read_config(original, optional=True))).encode()
        if receipt(content).sha256 != expected_sha256:
            raise ValueError("The source preferences changed after planning.")
        require_empty_destination(target)
        with config_directory(target, create=True) as directory:
            descriptor = os.open("config.toml", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory)
        os.fsync(target)
    verified = inspect_testing_state(destination)
    if verified.sha256 != expected_sha256:
        raise ValueError("The copied testing state could not be independently verified.")
    return verified


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--source", type=Path, required=True)
    copy = commands.add_parser("copy")
    copy.add_argument("--source", type=Path, required=True)
    copy.add_argument("--destination", type=Path, required=True)
    copy.add_argument("--expected-sha256", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--destination", type=Path, required=True)
    verify.add_argument("--expected-sha256", required=True)
    arguments = parser.parse_args()
    try:
        if arguments.command == "plan":
            result = plan_testing_state(arguments.source)
        elif arguments.command == "copy":
            result = copy_testing_state(arguments.source, arguments.destination, arguments.expected_sha256)
        else:
            result = inspect_testing_state(arguments.destination)
            if result.sha256 != arguments.expected_sha256:
                raise ValueError("The sanitized state digest does not match.")
    except (ValueError, OSError):
        print(json.dumps({"status": "refused", "format": STATE_FORMAT}))
        raise SystemExit(1) from None
    print(json.dumps({"status": "pass", **asdict(result)}))


if __name__ == "__main__":
    main()
