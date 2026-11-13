"""Whether Claude Code loaded this plugin as a channel for the session that started this server.

The plugin's MCP server and hooks load in every session, but Claude Code only
delivers channel messages and permission prompts when the session was launched
with `--channels` (the plugin approved in managed settings) or the
development-channels flag naming this plugin. Claude Code does not tell
the server, so the launching command line is read from the process table.
"""

from __future__ import annotations

import os
import subprocess

CHANNEL_FLAGS = ("--channels", "--dangerously-load-development-channels")
PLUGIN_ENTRY = "plugin:dui"
ANCESTORS = 4


def ancestry(pid: int) -> list[str]:
    """The command lines of this process's parent and its ancestors, nearest first."""
    lines: list[str] = []
    for _ in range(ANCESTORS):
        try:
            result = subprocess.run(
                ["ps", "-ww", "-o", "ppid=", "-o", "args=", "-p", str(pid)], capture_output=True, text=True, timeout=5, check=False
            )
        except (OSError, subprocess.SubprocessError):
            break
        parent, _, args = result.stdout.strip().partition(" ")
        if not parent.isdigit():
            break
        lines.append(args.strip())
        pid = int(parent)
        if pid <= 1:
            break
    return lines


def loaded_as_channel(command_lines: list[str]) -> bool | None:
    """True or False from the nearest Claude Code command line, or None when it cannot be found."""
    for line in command_lines:
        words = line.split()
        if not words or not os.path.basename(words[0]).startswith("claude"):
            continue
        if len(words) > 1 and words[1].startswith("bg-"):
            return None  # A background session the Claude Code daemon claimed; its launch flags are not in argv.
        return any(is_plugin_entry(entry) for entry in channel_entries(words[1:]))
    return None


def channel_entries(words: list[str]) -> list[str]:
    entries: list[str] = []
    collecting = False
    for word in words:
        if word == "--":
            break
        flag, has_value, value = word.partition("=")
        if flag in CHANNEL_FLAGS:
            collecting = not has_value
            entries.extend(value.split(",") if has_value else ())
        elif word.startswith("-"):
            collecting = False
        elif collecting:
            entries.extend(word.split(","))
    return entries


def is_plugin_entry(entry: str) -> bool:
    return entry == PLUGIN_ENTRY or entry.startswith(f"{PLUGIN_ENTRY}@")


def launch_prompt(command_lines: list[str]) -> str | None:
    for line in command_lines:
        options, separator, prompt = line.partition(" -- ")
        words = options.split()
        if not words or not os.path.basename(words[0]).startswith("claude"):
            continue
        if len(words) > 1 and words[1].startswith("bg-"):
            return None
        if any(
            word.split("=", 1)[0]
            in {"--resume", "--continue", "--fork-session", "--from-pr", "--no-session-persistence", "-r", "-c"}
            for word in words
        ):
            return None
        return prompt if separator and prompt.strip() else None
    return None


def matches_launch_prompt(rendered: str | None, prompt: str | None) -> bool:
    return rendered is not None and prompt is not None and rendered in {prompt, prompt.replace("\n", r"\012").replace("\t", r"\011")}
