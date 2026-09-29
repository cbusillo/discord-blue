"""How a local bridge labels its session's Discord thread.

Precedence: a name the user gave the session; otherwise the agent's own
auto-generated title (Claude Code's aiTitle); otherwise the first substantial
typed prompt; otherwise no label, and Discord Blue falls back to the git branch
and then to the repository alone.

Labels are short: tags such as ``<pasted_content ...>`` and system wrappers are
removed, as are URLs and quotes, and the rest is cut at a word boundary. A
prompt is substantial when what is left says what the session is about: short
prompts, go-aheads ("continue", "yes, go ahead") and slash commands do not.
"""

from __future__ import annotations

import re

LABEL_LIMIT = 45
MIN_WORDS = 4
GO_AHEAD = re.compile(r"(?:(?:continue|go|go on|go ahead|yes|y|ok|okay|sure|proceed|next|keep going|do it)\W*)+", re.IGNORECASE)
TAG_NAME = r"[a-zA-Z][\w-]*"
# A wrapper element and everything inside it, such as <system-reminder>...</system-reminder>.
TAG_BLOCK = re.compile(rf"<({TAG_NAME})\b[^<>]*>.*?</\1\s*>", re.DOTALL)
# What is left: an opening, closing or self-closing tag such as <pasted_content id="fc27">.
TAG = re.compile(rf"</?{TAG_NAME}\b[^<>]*/?>")
URL = re.compile(r"\b(?:https?|ftp)://\S+|\bwww\.\S+", re.IGNORECASE)
QUOTES = str.maketrans("", "", "\"'`" + "\u2018\u2019\u201c\u201d")
TRAILING = ",;:-([{" + "\u2013\u2014"


def clean(text: str) -> str:
    """One line of plain words: tags, URLs and quotes removed."""
    text = TAG.sub(" ", TAG_BLOCK.sub(" ", text))
    return " ".join(URL.sub(" ", text).translate(QUOTES).split())


def shorten(text: str) -> str:
    """Cut at a word boundary within LABEL_LIMIT; no ellipsis, and never half a word unless one word is too long."""
    if len(text) <= LABEL_LIMIT:
        return text
    words, kept = text.split(), ""
    for word in words:
        candidate = f"{kept} {word}" if kept else word
        if len(candidate) > LABEL_LIMIT:
            break
        kept = candidate
    return (kept or words[0][:LABEL_LIMIT]).rstrip(TRAILING + " ")


def substantial(prompt: str) -> str | None:
    """The prompt as a short label, or None when it says too little to name the session."""
    line = clean(prompt)
    if line.startswith("/") or len(line.split()) < MIN_WORDS or GO_AHEAD.fullmatch(line):
        return None
    return shorten(line)


def name_label(name: str | None) -> str | None:
    """A given or generated name as a label; a go-ahead such as Codex naming a thread "Continue" is not one."""
    line = clean(name or "")
    return shorten(line) if line and not GO_AHEAD.fullmatch(line) else None


class SessionLabel:
    """Tracks the label; `update` returns the label to send when it changed, else None."""

    def __init__(self, name: str | None = None, auto: str | None = None, prompt: str | None = None) -> None:
        self.name = name_label(name)
        self.auto = name_label(auto)
        self.prompt = substantial(prompt) if prompt else None
        self.current = self.name or self.auto or self.prompt

    def update(
        self, *, name: str | None = None, auto: str | None = None, prompt: str | None = None, clear_name: bool = False
    ) -> str | None:
        if clear_name:
            self.name = None
        self.name = name_label(name) or self.name
        self.auto = name_label(auto) or self.auto
        # The first substantial prompt says what the session is for; later ones are usually steps.
        if self.prompt is None and prompt:
            self.prompt = substantial(prompt)
        wanted = self.name or self.auto or self.prompt
        if wanted is None or wanted == self.current:
            return None
        self.current = wanted
        return wanted
