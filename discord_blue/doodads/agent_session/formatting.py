"""Discord text for Agent session threads.

Discord renders only a subset of Markdown, and a thread mixes assistant answers with the bot's own notices.
This module owns how assistant text, user prompts and the waiting line look, and how an assistant message is
recognised again when a thread's history is read back after a restart.
"""

from __future__ import annotations

import re

MARKDOWN_CODE_FENCE_RE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})(?P<info>[^`~\n]*)$")

# Assistant messages end with this invisible pair (WORD JOINER + ZERO WIDTH SPACE). It survives in Discord's
# stored content, so a thread's history still shows which bot messages are assistant answers after the server
# restarts and has lost any in-memory record. The pair differs from the old lone zero-width waiting message.
ASSISTANT_MESSAGE_MARKER = "⁠​"
# Assistant messages posted before the marker existed started with this visible label.
LEGACY_ASSISTANT_LABEL = "**Assistant**"
USER_MESSAGE_PREFIX = "🧑 "
WAITING_FOR_DIRECTION = "⏸ Waiting for you"


def is_assistant_message(content: str) -> bool:
    return content.endswith(ASSISTANT_MESSAGE_MARKER) or content.startswith(LEGACY_ASSISTANT_LABEL)


def mark_assistant_message(chunk: str) -> str:
    return f"{chunk}{ASSISTANT_MESSAGE_MARKER}"


def format_user_message(message: str) -> str:
    # `>>>` quotes the rest of the message, so every line of a multi-line prompt stays inside one quote block.
    return f">>> {USER_MESSAGE_PREFIX}{message.strip()}"


def convert_markdown_tables(text: str) -> str:
    """Rewrite GitHub-style Markdown tables as bullet lists, which Discord can render.

    One pass over the lines: each line is looked at a bounded number of times, so the cost is linear in the
    input. Tables inside code fences are left exactly as written.
    """
    lines = text.split("\n")
    output: list[str] = []
    fence: tuple[str, int] | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        fence_match = MARKDOWN_CODE_FENCE_RE.match(line.rstrip())
        if fence_match is not None:
            marker = fence_match.group("fence")
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
            output.append(line)
            index += 1
            continue
        if fence is not None or index + 1 >= len(lines):
            output.append(line)
            index += 1
            continue
        headers = _table_cells(line)
        delimiter = _table_cells(lines[index + 1])
        if headers is None or delimiter is None or len(headers) != len(delimiter) or not _is_delimiter_row(delimiter):
            output.append(line)
            index += 1
            continue
        index += 2
        while index < len(lines):
            row_line = lines[index]
            if MARKDOWN_CODE_FENCE_RE.match(row_line.rstrip()) is not None:
                break
            row = _table_cells(row_line)
            if row is None:
                break
            output.append(_format_table_row(headers, row))
            index += 1
    return "\n".join(output)


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
