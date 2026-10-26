from __future__ import annotations

from discord_blue.doodads.agent_session.formatting import CodeFence, advance_code_fence

DISCORD_MESSAGE_LIMIT = 2000
FENCE_FIT_ATTEMPTS = 8


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
