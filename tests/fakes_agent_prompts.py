from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from discord_blue.config import AgentSessionConfig, Config, DiscordConfig
from discord_blue.doodads.agent_session import bridge as bridge_module
from discord_blue.doodads.agent_session.protocol import RemoteRequestUserInput, RequestUserInputQuestion
from discord_blue.doodads.agent_session.sessions import AgentSession
from discord_blue.plugs.discord_plug import BlueBot
from tests.fakes_agent_session import FakeBot, FakeThread, FakeWebSocket, make_hello


class PromptFixture:
    def __init__(self) -> None:
        self.thread = FakeThread(555)
        config = cast(Config, SimpleNamespace(agent_session=AgentSessionConfig(), discord=DiscordConfig()))
        config.discord.employee_role_name = ""
        self.bridge = bridge_module.AgentSessionBridge(cast(BlueBot, FakeBot(config, thread=self.thread)))
        self.socket = FakeWebSocket()
        self.session = AgentSession(hello=make_hello(), websocket=cast(Any, self.socket), thread_id=555)
        self.bridge.sessions.register(self.session)

    async def prompt(self, call_id: str = "call-1") -> bridge_module.RequestUserInputView:
        request = RemoteRequestUserInput(
            session_id=self.session.session_id,
            session_epoch=self.session.session_epoch,
            call_id=call_id,
            turn_id="turn-1",
            questions=[
                RequestUserInputQuestion(id="answer", header="Answer", question="Choose", is_other=True, is_secret=False, options=[])
            ],
        )
        await self.bridge.handle_request_user_input(request)
        view = cast(bridge_module.RequestUserInputView, self.thread.sent_views[-1])
        view.set_answer("answer", "yes")
        return view


@contextmanager
def prompt_fixture() -> Iterator[PromptFixture]:
    with patch.object(bridge_module.discord, "Thread", FakeThread):
        yield PromptFixture()
