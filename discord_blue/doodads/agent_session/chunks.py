"""Split assistant answers into Discord messages.

Every message this module returns fits Discord's limit exactly, including the code fences it adds to keep a
split code block balanced and the assistant marker, so no send path ever has to truncate one.
"""

from __future__ import annotations

from discord_blue.doodads.agent_session.formatting import ASSISTANT_MESSAGE_MARKER
from discord_blue.doodads.agent_session.formatting import CodeFence
from discord_blue.doodads.agent_session.formatting import advance_code_fence
from discord_blue.doodads.agent_session.formatting import convert_markdown_tables
from discord_blue.doodads.agent_session.formatting import mark_assistant_message
from discord_blue.doodads.agent_session.formatting import strip_assistant_markers

DISCORD_MESSAGE_LIMIT = 2000
DISCORD_ASSISTANT_CHUNK_LIMIT = 1800
MAX_ASSISTANT_CHUNKS = 10
ASSISTANT_TRUNCATED_NOTICE = "\n\n*(truncated, see terminal)*"
# Tries at shrinking a chunk to make room for its fences before giving up on keeping that code block balanced.
FENCE_FIT_ATTEMPTS = 8


def format_assistant_messages(text: str) -> list[str]:
    """Turn one assistant answer into marked Discord messages, at most `MAX_ASSISTANT_CHUNKS` of them."""
    body_limit = DISCORD_ASSISTANT_CHUNK_LIMIT - len(ASSISTANT_MESSAGE_MARKER) - len(ASSISTANT_TRUNCATED_NOTICE)
    source = convert_markdown_tables(strip_assistant_markers(text))
    # The answer can only fill this many messages; reading further would be wasted work.
    source = source[: (MAX_ASSISTANT_CHUNKS + 1) * DISCORD_ASSISTANT_CHUNK_LIMIT]
    chunks = split_discord_message(source, body_limit, max_chunks=MAX_ASSISTANT_CHUNKS + 1)
    if len(chunks) > MAX_ASSISTANT_CHUNKS:
        chunks = chunks[:MAX_ASSISTANT_CHUNKS]
        chunks[-1] = f"{chunks[-1]}{ASSISTANT_TRUNCATED_NOTICE}"
    return [mark_assistant_message(chunk) for chunk in chunks]


def split_discord_message(text: str, limit: int, *, max_chunks: int | None = None) -> list[str]:
    """Split text into chunks of at most `limit` characters, reopening and closing a code block that a split
    lands inside so each chunk renders on its own."""
    normalized = text.strip()
    chunks: list[str] = []
    fence: CodeFence | None = None
    position = _skip_whitespace(normalized, 0)
    while position < len(normalized) and (max_chunks is None or len(chunks) < max_chunks):
        prefix = _opening(fence)
        if len(prefix) + len(_closing(fence)) > limit // 2:
            # An absurdly long fence or info string cannot be repeated on every chunk; stop balancing it.
            fence, prefix = None, ""
        budget = limit - len(prefix) - len(_closing(fence))
        chunk: str | None = None
        for _attempt in range(FENCE_FIT_ATTEMPTS):
            end, next_position = _split_point(normalized, position, max(1, budget))
            piece = normalized[position:end].rstrip()
            next_fence = _scan_fences(piece, fence)
            candidate = f"{prefix}{piece}{_closing(next_fence)}"
            if len(candidate) <= limit:
                chunk = candidate
                break
            budget -= len(candidate) - limit
        if chunk is None:
            end, next_position = _split_point(normalized, position, limit)
            chunk = normalized[position:end].rstrip()
            next_fence = None
        chunks.append(chunk)
        fence = next_fence
        position = _skip_whitespace(normalized, next_position)
    return chunks


def _split_point(text: str, start: int, limit: int) -> tuple[int, int]:
    """Choose where the chunk starting at `start` ends: a newline, else a space, else a hard cut."""
    if len(text) - start <= limit:
        return len(text), len(text)
    hard_end = start + limit
    minimum = start + limit // 2
    split_at = text.rfind("\n", start, hard_end)
    if split_at < minimum:
        split_at = text.rfind(" ", start, hard_end)
    if split_at < minimum:
        split_at = hard_end
    return split_at, split_at


def _skip_whitespace(text: str, position: int) -> int:
    while position < len(text) and text[position].isspace():
        position += 1
    return position


def _scan_fences(text: str, fence: CodeFence | None) -> CodeFence | None:
    for line in text.split("\n"):
        fence = advance_code_fence(line, fence)
    return fence


def _opening(fence: CodeFence | None) -> str:
    if fence is None:
        return ""
    return f"{fence.char * fence.length}{fence.info}\n"


def _closing(fence: CodeFence | None) -> str:
    if fence is None:
        return ""
    return f"\n{fence.char * fence.length}"
