"""Shared Components V2 presentation for Codex and Claude session controls."""

from __future__ import annotations

from datetime import UTC, datetime

import discord

from discord_blue.doodads.agent_session.chunks import split_discord_message
from discord_blue.doodads.agent_session.formatting import convert_markdown_tables, mark_assistant_message
from discord_blue.doodads.agent_session.formatting import strip_assistant_markers
from discord_blue.doodads.agent_session.sessions import AgentSession


STATUS_CARD_COMPONENT_ID = 213
ANSWER_PREVIEW_LIMIT = 1600
ANSWER_PREVIEW_LINE_LIMIT = 12
DASHBOARD_PAGE_SIZE = 8
STATUS_LABELS = {
    "working": ("🔄 Working", 0x758AFF),
    "waiting": ("⏸ Waiting on you", 0xF0B232),
    "done": ("✅ Done", 0x58BC8A),
    "failed": ("❌ Failed", 0xEF7279),
    "unknown": ("⏸ Check terminal", 0xF0B232),
}


def assistant_answer_card(text: str) -> tuple[discord.ui.LayoutView, bool]:
    source = convert_markdown_tables(strip_assistant_markers(text)).strip()
    preview_budget = min(ANSWER_PREVIEW_LIMIT, len("\n".join(source.splitlines()[:ANSWER_PREVIEW_LINE_LIMIT])))
    long_answer = len(source) > preview_budget
    preview = split_discord_message(source, max(1, preview_budget), max_chunks=1)
    children: list[discord.ui.Item[discord.ui.LayoutView]] = [
        discord.ui.TextDisplay("### Answer"),
        discord.ui.Separator(),
        discord.ui.TextDisplay(mark_assistant_message(preview[0] if preview else "")),
    ]
    if long_answer:
        children.extend(
            [
                discord.ui.TextDisplay("-# Preview above · Full answer attached below"),
                discord.ui.File("attachment://answer.md"),
            ]
        )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*children, accent_color=STATUS_LABELS["done"][1]))
    return view, long_answer


def active_session_cards(sessions: list[AgentSession], guild_id: int) -> list[discord.ui.LayoutView]:
    from discord_blue.doodads.agent_session.threads import session_thread_name

    priority = {state: position for position, state in enumerate(("waiting", "unknown", "failed", "working", "done"))}
    ordered = sorted(sessions, key=lambda entry: (priority[entry.display_state], session_thread_name(entry.hello)))
    pages = [ordered[index : index + DASHBOARD_PAGE_SIZE] for index in range(0, len(ordered), DASHBOARD_PAGE_SIZE)] or [[]]
    views = []
    for index, page in enumerate(pages):
        children: list[discord.ui.Item[discord.ui.LayoutView]] = [
            discord.ui.TextDisplay(f"### Live agent sessions · {len(ordered)}"),
            discord.ui.Separator(),
        ]
        if not page:
            children.append(discord.ui.TextDisplay("No live agent sessions."))
        for session in page:
            title = discord.utils.escape_markdown(strip_assistant_markers(session_thread_name(session.hello)))[:150]
            host = discord.utils.escape_markdown(strip_assistant_markers(session.hello.host_label))[:80]
            detail = discord.utils.escape_markdown(strip_assistant_markers(session.last_status_message or ""))[:160]
            label = STATUS_LABELS[session.display_state][0]
            text = f"**{label}** · {title}\n-# {host}"
            if detail:
                text += f"\n{detail}"
            if session.thread_id is not None and guild_id:
                children.append(
                    discord.ui.Section(
                        discord.ui.TextDisplay(text),
                        accessory=discord.ui.Button(
                            label="Open thread", url=f"https://discord.com/channels/{guild_id}/{session.thread_id}"
                        ),
                    )
                )
            else:
                children.append(discord.ui.TextDisplay(f"{text}\n-# Thread is attaching"))
        children.append(
            discord.ui.TextDisplay(f"-# Page {index + 1} of {len(pages)} · Snapshot <t:{int(datetime.now(UTC).timestamp())}:R>")
        )
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(*children, accent_color=0x758AFF))
        views.append(view)
    return views


def notice_card(content: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(discord.ui.TextDisplay(strip_assistant_markers(content)), accent_color=0x4D4E56))
    return view


def session_status_card(session: AgentSession, reactions: list[str]) -> discord.ui.LayoutView:
    from discord_blue.doodads.agent_session.threads import session_thread_name

    state = session.display_state
    label, color = STATUS_LABELS[state]
    identity = session_thread_name(session.hello)
    detail = (
        session.last_status_message
        or {
            "working": "The agent is working.",
            "waiting": "Reply in this thread when you are ready.",
            "done": "Reply to start the next turn.",
            "failed": "Check the native terminal for the error.",
            "unknown": "Approval status is unconfirmed; check the native terminal.",
        }[state]
    )
    if session.pending_control_confirmation:
        label, color = "⏹ End this session?", 0xF0B232
        detail = "Tap ✅ to confirm or ✖️ to keep the session open."
    hints = {
        "▶️": "continue",
        "\N{INFORMATION SOURCE}\N{VARIATION SELECTOR-16}": "status",
        "⏸️": "pause",
        "⏹️": "end",
    }
    actions = " · ".join(f"{emoji} {hints[emoji]}" for emoji in reactions if emoji in hints)
    children: list[discord.ui.Item[discord.ui.LayoutView]] = [
        discord.ui.TextDisplay(f"### {label}\n{discord.utils.escape_markdown(strip_assistant_markers(identity))[:200]}"),
        discord.ui.Separator(),
        discord.ui.TextDisplay(strip_assistant_markers(detail)[:1000]),
    ]
    if actions and not session.pending_control_confirmation:
        children.append(discord.ui.TextDisplay(f"-# Tap a reaction: {actions}"))
    children.append(discord.ui.TextDisplay(f"-# Updated <t:{int(datetime.now(UTC).timestamp())}:R>"))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*children, accent_color=color, id=STATUS_CARD_COMPONENT_ID))
    return view
