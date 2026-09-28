"""Whether Claude Code loaded this plugin as a channel for the session that started this server.

The plugin's MCP server and hooks load in every session, but Claude Code only
delivers channel messages and permission prompts when the session was launched
with the development-channels flag naming this plugin. Claude Code does not tell
the server, so the launching command line is read from the process table.
"""

from __future__ import annotations

import os
import subprocess

CHANNEL_FLAG = "--dangerously-load-development-channels"
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
        flagged = any(word == CHANNEL_FLAG or word.startswith(f"{CHANNEL_FLAG}=") for word in words)
        return flagged and PLUGIN_ENTRY in line
    return None
