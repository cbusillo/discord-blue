"""Discord text for Agent session threads.

Discord renders only a subset of Markdown, and a thread mixes assistant answers with the bot's own notices.
This module owns how assistant text, user prompts and the waiting line look, and how an assistant message is
recognised again when a thread's history is read back after a restart.
"""

from __future__ import annotations

from dataclasses import dataclass

WORD_JOINER = "\N{WORD JOINER}"
# Assistant messages end with this invisible pair (WORD JOINER + ZERO WIDTH SPACE). It survives in Discord's
# stored content, so a thread's history still shows which bot messages are assistant answers after the server
# restarts and has lost any in-memory record. The pair differs from the old lone zero-width waiting message.
# Every other message the bot posts has its word joiners removed, so pasted text can never carry the marker.
ASSISTANT_MESSAGE_MARKER = f"{WORD_JOINER}\N{ZERO WIDTH SPACE}"
# Assistant messages posted before the marker existed started with this visible label.
LEGACY_ASSISTANT_LABEL = "**Assistant**"
USER_MESSAGE_PREFIX = "🧑 "
WAITING_FOR_DIRECTION = "⏸ Waiting for you"

# A converted table may grow to this multiple of its written size, plus a small allowance; past that it is
# left as written. Repeating long headers on every row would otherwise multiply the text without bound.
TABLE_EXPANSION_FACTOR = 2
TABLE_EXPANSION_ALLOWANCE = 200


def is_assistant_message(content: str) -> bool:
    """Say whether a bot-authored message is an assistant answer. Callers check authorship first."""
    return content.endswith(ASSISTANT_MESSAGE_MARKER) or content.startswith(LEGACY_ASSISTANT_LABEL)


def strip_assistant_markers(text: str) -> str:
    return text.replace(WORD_JOINER, "")


def mark_assistant_message(chunk: str) -> str:
    return f"{strip_assistant_markers(chunk)}{ASSISTANT_MESSAGE_MARKER}"


def format_user_message(message: str) -> str:
    # `>>>` quotes the rest of the message, so every line of a multi-line prompt stays inside one quote block.
    return f">>> {USER_MESSAGE_PREFIX}{message.strip()}"


@dataclass(frozen=True, slots=True)
class CodeFence:
    char: str
    length: int
    info: str


def _fence_run(line: str) -> tuple[str, int, str] | None:
    indent = len(line) - len(line.lstrip(" "))
    if indent > 3:
        return None
    body = line[indent:]
    if not body or body[0] not in "`~":
        return None
    char = body[0]
    length = len(body) - len(body.lstrip(char))
    if length < 3:
        return None
    return char, length, body[length:]


def code_fence_opening(line: str) -> CodeFence | None:
    """Return the fence a line opens under GFM rules: 3+ backticks or tildes, with no backtick in a backtick
    fence's info string."""
    run = _fence_run(line)
    if run is None:
        return None
    char, length, rest = run
    if char == "`" and "`" in rest:
        return None
    return CodeFence(char, length, rest.strip())


def closes_code_fence(line: str, fence: CodeFence) -> bool:
    """A GFM closing fence uses the opening character, is at least as long, and has nothing after it."""
    run = _fence_run(line)
    if run is None:
        return False
    char, length, rest = run
    return char == fence.char and length >= fence.length and not rest.strip(" \t")


def advance_code_fence(line: str, fence: CodeFence | None) -> CodeFence | None:
    if fence is None:
        return code_fence_opening(line)
    return None if closes_code_fence(line, fence) else fence


def convert_markdown_tables(text: str) -> str:
    """Rewrite GitHub-style Markdown tables as bullet lists, which Discord can render.

    One pass over the lines: each line is looked at a bounded number of times, so the cost is linear in the
    input. Tables inside code fences, and tables that would grow too much, are left exactly as written.
    """
    lines = text.split("\n")
    output: list[str] = []
    fence: CodeFence | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if fence is not None or code_fence_opening(line) is not None or index + 1 >= len(lines):
            fence = advance_code_fence(line, fence)
            output.append(line)
            index += 1
            continue
        headers = _table_cells(line)
        delimiter = _table_cells(lines[index + 1])
        if headers is None or delimiter is None or len(headers) != len(delimiter) or not _is_delimiter_row(delimiter):
            output.append(line)
            index += 1
            continue
        end = index + 2
        while end < len(lines) and code_fence_opening(lines[end]) is None and "|" in lines[end]:
            end += 1
        output.extend(_convert_table(headers, lines[index:end]))
        index = end
    return "\n".join(output)


def _convert_table(headers: list[str], table_lines: list[str]) -> list[str]:
    written = sum(len(line) + 1 for line in table_lines)
    budget = written * TABLE_EXPANSION_FACTOR + TABLE_EXPANSION_ALLOWANCE
    converted: list[str] = []
    for row_line in table_lines[2:]:
        row = _table_cells(row_line)
        if row is None:
            return table_lines
        bullet = _format_table_row(headers, row)
        budget -= len(bullet) + 1
        if budget < 0:
            return table_lines
        converted.append(bullet)
    return converted


def _table_cells(line: str) -> list[str] | None:
    stripped = line.strip()
    if "|" not in stripped or len(line) - len(line.lstrip(" ")) > 3:
        return None
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in stripped:
        if escaped:
            current.append(char if char == "|" else f"\\{char}")
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    return cells


def _is_delimiter_row(cells: list[str]) -> bool:
    for cell in cells:
        dashes = cell.removeprefix(":").removesuffix(":")
        if not dashes or dashes.strip("-"):
            return False
    return True


def _format_table_row(headers: list[str], row: list[str]) -> str:
    name = _strip_bold(row[0]) if row else ""
    label = f"**{name}**" if name else ""
    values: list[str] = []
    for position, value in enumerate(row[1:], start=1):
        if not value:
            continue
        header = _strip_bold(headers[position]) if position < len(headers) else ""
        if len(headers) == 2 or not header:
            values.append(value)
        else:
            values.append(f"{header}: {value}")
    if not values:
        return f"- {label}".rstrip()
    joined = " · ".join(values)
    if not label:
        return f"- {joined}"
    separator = ": " if len(headers) == 2 else " — "
    return f"- {label}{separator}{joined}"


def _strip_bold(cell: str) -> str:
    if len(cell) > 4 and cell.startswith("**") and cell.endswith("**"):
        return cell[2:-2].strip()
    return cell
