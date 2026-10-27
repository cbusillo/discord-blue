from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

import discord

from discord_blue.doodads.agent_session.cards import DASHBOARD_PAGE_SIZE, active_session_rows
from discord_blue.doodads.agent_session.messages import agent_session_allowed_mentions
from discord_blue.doodads.agent_session.threads import session_thread_name

if TYPE_CHECKING:
    from discord_blue.doodads.agent_session.bridge import AgentSessionBridge


STATUS_FILTERS = {
    "all": "All statuses",
    "waiting": "Waiting on you",
    "working": "Working",
    "failed": "Failed",
    "done": "Done",
}
HARNESS_FILTERS = {"all": "All harnesses", "codex": "Codex", "claude": "Claude Code"}


class DashboardFilter(discord.ui.Select["ActiveSessionDashboard"]):
    def __init__(self, dashboard: ActiveSessionDashboard, kind: Literal["status", "harness"]) -> None:
        choices = STATUS_FILTERS if kind == "status" else HARNESS_FILTERS
        selected = dashboard.status if kind == "status" else dashboard.harness
        super().__init__(
            placeholder=f"{kind.title()}: {choices[selected]}",
            options=[discord.SelectOption(label=label, value=value, default=value == selected) for value, label in choices.items()],
        )
        self.dashboard = dashboard
        self.kind: Literal["status", "harness"] = kind

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.dashboard.update(interaction, self.kind, self.values[0])


class DashboardButton(discord.ui.Button["ActiveSessionDashboard"]):
    def __init__(
        self, dashboard: ActiveSessionDashboard, action: Literal["previous", "next", "refresh"], *, disabled: bool = False
    ) -> None:
        super().__init__(label=action.title(), disabled=disabled)
        self.dashboard = dashboard
        self.action: Literal["previous", "next", "refresh"] = action

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.dashboard.update(interaction, self.action)


class ActiveSessionDashboard(discord.ui.LayoutView):
    def __init__(self, bridge: AgentSessionBridge, owner_id: int, guild_id: int) -> None:
        super().__init__(timeout=600)
        self.bridge = bridge
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.status = "all"
        self.harness = "all"
        self.page = 0
        self.update_lock = asyncio.Lock()
        self.render()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (
            not self.bridge.bot.config.agent_session.enabled
            or interaction.user.id != self.owner_id
            or not self.bridge.is_operator(interaction.user)
            or (interaction.guild_id or 0) != self.guild_id
        ):
            await interaction.response.send_message(
                "Run /code active as an agent session operator to open your dashboard.", ephemeral=True
            )
            return False
        return True

    async def update(self, interaction: discord.Interaction, action: str, value: str = "") -> None:
        await interaction.response.defer()
        async with self.update_lock:
            if action == "status" and value in STATUS_FILTERS:
                self.status = value
                self.page = 0
            elif action == "harness" and value in HARNESS_FILTERS:
                self.harness = value
                self.page = 0
            elif action == "previous":
                self.page -= 1
            elif action == "next":
                self.page += 1
            self.render()
            await interaction.edit_original_response(view=self, allowed_mentions=agent_session_allowed_mentions())

    def render(self) -> None:
        sessions = self.bridge.sessions.live_sessions()
        needs_you = sum(session.display_state in {"waiting", "unknown"} for session in sessions)
        filtered = [
            session
            for session in sessions
            if (
                self.status == "all"
                or session.display_state == self.status
                or (self.status == "waiting" and session.display_state == "unknown")
            )
            and (self.harness == "all" or session.hello.harness == self.harness)
        ]
        priority = {state: position for position, state in enumerate(("waiting", "unknown", "failed", "working", "done"))}
        filtered.sort(key=lambda session: (priority[session.display_state], session_thread_name(session.hello), session.session_id))
        pages = max(1, (len(filtered) + DASHBOARD_PAGE_SIZE - 1) // DASHBOARD_PAGE_SIZE)
        self.page = min(max(0, self.page), pages - 1)
        start = self.page * DASHBOARD_PAGE_SIZE
        rows = active_session_rows(filtered[start : start + DASHBOARD_PAGE_SIZE], self.guild_id)
        self.clear_items()
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    f"### Live agent sessions · {len(sessions)}\n-# {needs_you} need you · {len(filtered)} match"
                ),
                discord.ui.ActionRow(DashboardFilter(self, "status")),
                discord.ui.ActionRow(DashboardFilter(self, "harness")),
                discord.ui.Separator(),
                *(rows or [discord.ui.TextDisplay("No sessions match these filters." if sessions else "No live agent sessions.")]),
                discord.ui.TextDisplay(
                    f"-# Page {self.page + 1} of {pages} · Snapshot <t:{int(datetime.now(UTC).timestamp())}:R>\n"
                    "-# Controls expire after 10 minutes idle. Run /code active again."
                ),
                discord.ui.ActionRow(
                    DashboardButton(self, "previous", disabled=self.page == 0),
                    DashboardButton(self, "next", disabled=self.page == pages - 1),
                    DashboardButton(self, "refresh"),
                ),
                accent_color=0x758AFF,
            )
        )
