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
# A 45-character label needs only the start of a prompt; bounding the input bounds the work on a huge paste.
INPUT_LIMIT = 4 * 1024
# Text Claude Code wraps a prompt in: system output, and pasted or attached content. A label never comes from
# inside one, and an unclosed one (a paste cut off by the bound above) hides the rest of the prompt.
WRAPPERS = frozenset(
    {
        "system-reminder",
        "local-command-stdout",
        "local-command-stderr",
        "command-name",
        "command-message",
        "command-args",
        "channel",
        "bash-input",
        "bash-stdout",
        "bash-stderr",
        "pasted_content",
        "pasted-content",
        "pasted_text",
        "paste",
        "attachment",
        "attachments",
        "file",
        "document",
        "image",
    }
)
URL = re.compile(r"\b(?:https?|ftp)://\S+|\bwww\.\S+", re.IGNORECASE)
QUOTES = str.maketrans("", "", "\"'`" + "\u2018\u2019\u201c\u201d")
TRAILING = ",;:-([{" + "\u2013\u2014"


def scan_tags(text: str) -> list[tuple[int, int, str, bool, bool]]:
    """Every tag in text as (start, end, name, closing, self_closing), in one pass with no backtracking.

    A tag is `<`, an optional `/`, a name (a letter, then letters, digits, `_` or `-`), anything but `<` or `>`,
    then `>`. Each character is looked at a bounded number of times, so hostile input cannot make this slow.
    """
    tags: list[tuple[int, int, str, bool, bool]] = []
    length, position = len(text), 0
    while (start := text.find("<", position)) != -1:
        index = start + 1
        closing = index < length and text[index] == "/"
        index += closing
        name_start = index
        if index < length and text[index].isascii() and text[index].isalpha():
            index += 1
            while index < length and (text[index].isascii() and (text[index].isalnum() or text[index] in "_-")):
                index += 1
        name = text[name_start:index].lower()
        if not name:
            position = start + 1
            continue
        # The rest of the tag: stop at the first `<` (a new tag starts there) or `>` (this one ends).
        end = index
        while end < length and text[end] not in "<>":
            end += 1
        if end >= length or text[end] == "<":
            position = end  # Not a tag; resume at the next `<` without rescanning.
            continue
        tags.append((start, end + 1, name, closing, text[end - 1] == "/"))
        position = end + 1
    return tags


def strip_tags(text: str) -> str:
    """Remove every tag and whatever a closed tag pair wraps, in one pass over the bounded text."""
    text = text[:INPUT_LIMIT]
    removed: list[tuple[int, int]] = []
    open_tags: list[tuple[str, int]] = []
    open_counts: dict[str, int] = {}
    for start, end, name, closing, self_closing in scan_tags(text):
        removed.append((start, end))
        if self_closing:
            continue
        if not closing:
            open_tags.append((name, start))
            open_counts[name] = open_counts.get(name, 0) + 1
        elif open_counts.get(name):
            # Close the nearest matching open tag; unclosed tags inside it (such as <br>) go with it.
            while open_tags:
                opened, opened_at = open_tags.pop()
                open_counts[opened] -= 1
                if opened == name:
                    removed.append((opened_at, end))
                    break
    for name, start in open_tags:
        if name in WRAPPERS:
            removed.append((start, len(text)))
            break
    kept, position = [], 0
    for start, end in sorted(removed):
        if start > position:
            kept.append(text[position:start])
        position = max(position, end)
    kept.append(text[position:])
    return " ".join(kept)


def clean(text: str) -> str:
    """One line of plain words: tags, URLs and quotes removed."""
    return " ".join(URL.sub(" ", strip_tags(text)).translate(QUOTES).split())


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
