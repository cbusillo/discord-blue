"""The Codex bridge against the real agent-session server, not FakeDiscordBlue.

Each side has its own tests against a stand-in for the other: the Codex bridge against FakeDiscordBlue, the server
against hand-written hellos and commands. Here the Codex bridge connects to the agent-session cog on a real
discord.py client over FakeDiscord, so a message one side sends and the other does not understand fails a test.
Codex itself is still FakeRpc; `test_codex_bridge_stock` runs a real app-server where one is installed.
"""

from __future__ import annotations

import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiohttp

from discord_blue.codex_bridge.bridge import CodexBridge
from discord_blue.codex_bridge.config import BridgeConfig
from discord_blue.doodads.agent_session import bridge as bridge_module
from tests.discord_client import OPERATOR, OPERATOR_ID, TOKEN, message_create, offering, reaction_add, running_cog, sent_now
from tests.fake_discord import ADD_REACTION, FakeDiscord, FakeThreadState
from tests.test_attach_scenarios import until
from tests.test_codex_bridge import FakeRpc, thread

Json = dict[str, Any]


def contents(discord_thread: FakeThreadState) -> list[str]:
    return [message.content for message in discord_thread.messages]


class CodexBridgeServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_codex_session_round_trips_through_the_real_server(self) -> None:
        fake = FakeDiscord(latency=0.002)
        fake.route_latency[ADD_REACTION] = 0.1  # The bot's reactions show one by one, as Discord's rate limit spaces them.
        rpc = FakeRpc(thread("root"))
        async with running_cog(fake) as running, aiohttp.ClientSession() as http:
            config = BridgeConfig(
                server_url=running.url,
                token=TOKEN,
                socket_path=Path("/unused"),
                host_label="Codex on test",
                reconnect_seconds=0.05,
                unnamed_retry_seconds=(0.01, 0.01, 0.01),
            )
            codex = CodexBridge(config)
            codex.rpc, codex.http = rpc, http
            await codex.discover()
            try:
                bridge = running.cog.bridge

                def session_acknowledged() -> bool:
                    session = bridge.sessions.get("root")
                    return session is not None and session.acknowledged

                self.assertTrue(await until(session_acknowledged, 5), "the server never acknowledged the Codex session")
                session = bridge.sessions.get("root")
                assert session is not None and session.thread_id is not None
                discord_thread = fake.threads[session.thread_id]

                def posted(predicate: Callable[[str], bool]) -> Callable[[], bool]:
                    return lambda: any(predicate(text) for text in contents(discord_thread))

                # Codex -> Discord: a finished turn's answer.
                await codex.dispatch({"method": "turn/started", "params": {"threadId": "root", "turn": {"id": "t1"}}})
                item = {"type": "agentMessage", "phase": "final_answer", "text": "Fixed the login bug."}
                await codex.dispatch({"method": "item/completed", "params": {"threadId": "root", "turnId": "t1", "item": item}})
                await codex.dispatch(
                    {"method": "turn/completed", "params": {"threadId": "root", "turn": {"id": "t1", "status": "completed"}}}
                )
                answered = await until(posted(lambda text: "Fixed the login bug." in text), 5)

                # Discord -> Codex: an operator's reply starts a turn.
                reply = sent_now(discord_thread.id, "now run the tests", OPERATOR_ID)
                discord_thread.messages.append(reply)
                message_create(running.bot, reply, OPERATOR)
                started = await until(lambda: bool(rpc.called("turn/start")), 5)
                # Queued until Codex acknowledges the command; the session controls carry its status after that.
                acknowledged = await until(lambda: not reply.reactions, 5)

                # Both ways: Codex asks to run a command, and an operator approves it from Discord.
                approval = {"threadId": "root", "turnId": "t2", "itemId": "item-1", "command": "make test", "cwd": "/work/project"}
                await codex.dispatch({"id": 7, "method": "item/commandExecution/requestApproval", "params": approval})
                # The operator taps approve as soon as it shows, while the bot is still adding deny.
                approve = bridge_module.REACTION_APPROVAL_APPROVE
                self.assertTrue(await until(lambda: offering(discord_thread, approve) is not None, 5), "no approval was offered")
                approval_message = offering(discord_thread, approve)
                assert approval_message is not None and "make test" in approval_message.content
                reaction_add(running.bot, discord_thread.id, approval_message.id, approve, OPERATOR)
                decided = await until(lambda: bool(rpc.responses), 5)
            finally:
                await codex.detach_all()

        self.assertTrue(answered, f"Codex's final answer never reached the thread: {contents(discord_thread)}")
        self.assertTrue(started, "the operator's reply never started a Codex turn")
        [params] = rpc.called("turn/start")
        assert params is not None
        self.assertEqual(params["input"], [{"type": "text", "text": "now run the tests"}])
        self.assertTrue(acknowledged, f"the server never took Codex's acknowledgement of the reply: {reply.reactions}")
        self.assertTrue(decided, "the operator's approval never reached Codex")
        self.assertEqual(rpc.responses, [(7, {"decision": "accept"})])


if __name__ == "__main__":
    unittest.main()
