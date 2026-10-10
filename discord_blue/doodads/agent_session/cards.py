"""Shared Components V2 presentation for Codex and Claude session controls."""

from __future__ import annotations

from datetime import UTC, datetime

import discord

from discord_blue.doodads.agent_session.formatting import strip_assistant_markers
from discord_blue.doodads.agent_session.sessions import AgentSession
from discord_blue.doodads.agent_session.threads import session_thread_name


STATUS_CARD_COMPONENT_ID = 213


def session_status_card(session: AgentSession, reactions: list[str]) -> discord.ui.LayoutView:
    state = session.display_state
    labels = {
        "working": ("🔄 Working", 0x758AFF),
        "waiting": ("⏸ Waiting on you", 0xF0B232),
        "done": ("✅ Done", 0x58BC8A),
        "failed": ("❌ Failed", 0xEF7279),
    }
    label, color = labels[state]
    identity = session_thread_name(session.hello)
    detail = (
        session.last_status_message
        or {
            "working": "The agent is working.",
            "waiting": "Reply in this thread when you are ready.",
            "done": "Reply to start the next turn.",
            "failed": "Check the native terminal for the error.",
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
