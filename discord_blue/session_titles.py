"""How a local bridge labels its session's Discord thread.

Precedence: a name the user gave the session; otherwise its latest substantial
typed prompt; otherwise no label, and Discord Blue falls back to the git branch
and then to the repository alone. A prompt is substantial when it says what
the session is about: short prompts and go-aheads ("continue", "yes, go
ahead") say nothing, and neither do slash commands or tagged system text.
"""

from __future__ import annotations

import re

LABEL_LIMIT = 80
MIN_WORDS = 4
GO_AHEAD = re.compile(r"(?:(?:continue|go|go on|go ahead|yes|y|ok|okay|sure|proceed|next|keep going|do it)\W*)+", re.IGNORECASE)


def one_line(text: str) -> str:
    return " ".join(text.split())


def substantial(prompt: str) -> str | None:
    """The prompt as a one-line label, or None when it says too little to name the session."""
    line = one_line(prompt)
    if line.startswith(("/", "<")) or len(line.split()) < MIN_WORDS or GO_AHEAD.fullmatch(line):
        return None
    return clip(line)


def clip(label: str) -> str:
    label = one_line(label)
    return label[: LABEL_LIMIT - 1] + "…" if len(label) > LABEL_LIMIT else label


class SessionLabel:
    """Tracks the label; `update` returns the label to send when it changed, else None."""

    def __init__(self, name: str | None = None, prompt: str | None = None) -> None:
        self.name = clip(name) if name and name.strip() else None
        self.prompt = substantial(prompt) if prompt else None
        self.current = self.name or self.prompt

    def update(self, *, name: str | None = None, prompt: str | None = None, clear_name: bool = False) -> str | None:
        if clear_name:
            self.name = None
        if name and name.strip():
            self.name = clip(name)
        if prompt and (label := substantial(prompt)):
            self.prompt = label
        wanted = self.name or self.prompt
        if wanted is None or wanted == self.current:
            return None
        self.current = wanted
        return wanted
