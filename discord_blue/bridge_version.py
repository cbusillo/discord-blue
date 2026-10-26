from importlib.metadata import version
from pathlib import Path
import json


def bridge_version() -> str:
    source = Path(__file__).with_name("bridge_release.json")
    revision = json.loads(source.read_text())["revision"] if source.is_file() else "unreleased"
    return f"{version('discord-blue')} {revision}"
