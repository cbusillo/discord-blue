from __future__ import annotations

import unittest
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from typing import Any, cast

from discord_blue.agent_client import AgentSessionClient
from discord_blue.question_resolutions import read_resolutions


READER = (sys.executable, str(Path(__file__).parent / "fixtures/question_reader.py"), "--owner", "director")


class QuestionResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_matches_the_question_and_preserves_its_recorded_answer(self) -> None:
        source = {
            "key": "card",
            "number": "Q1218",
            "url": "https://github.com/cbusillo/example/issues/1#issuecomment-1",
            "revision": "0",
        }
        pending = {**source, "number": "Q1219"}
        resolved = await read_resolutions(READER, [source, pending])
        self.assertEqual(len(resolved), 1)
        self.assertEqual(
            resolved[0],
            {**source, "answer": "Keep both.", "decision_url": "https://github.com/cbusillo/example/issues/1#issuecomment-2"},
        )
        with self.assertRaises(ValueError):
            await read_resolutions(READER, [{**source, "url": "https://github.com/cbusillo/example/issues/999"}])
        self.assertEqual(await read_resolutions(READER, [{**source, "number": "Q999"}]), [])
        self.assertEqual(
            await read_resolutions(
                READER, [{**source, "url": "https://github.com/cbusillo/example/issues/999"}, source], strict=False
            ),
            resolved,
        )

    async def test_a_late_card_reply_is_not_sent_to_either_native_harness(self) -> None:
        client = AgentSessionClient(cast(Any, SimpleNamespace(question_reader=READER)), "session")
        source = {
            "key": "card",
            "number": "Q1218",
            "url": "https://github.com/cbusillo/example/issues/1#issuecomment-1",
            "revision": "0",
        }
        resolved = {**source, "answer": "Keep both.", "decision_url": source["url"].replace("-1", "-2")}
        command = {
            **client.event("command"),
            "command_id": "late",
            "kind": "reply",
            "text": "Change it",
            "question_sources": [source],
        }
        with (
            patch.object(client, "question_resolutions", new_callable=AsyncMock, return_value=[resolved]),
            patch.object(client, "run_command", new_callable=AsyncMock) as run,
        ):
            response = await client.handle_control(command)
        run.assert_not_awaited()
        self.assertIsNotNone(response)
        self.assertEqual(cast(dict[str, Any], response)["resolutions"], [resolved])
        self.assertIn(resolved["answer"], cast(dict[str, Any], response)["reason"])
        with (
            patch.object(client, "question_resolutions", new_callable=AsyncMock, side_effect=ValueError),
            patch.object(client, "run_command", new_callable=AsyncMock) as run,
        ):
            failed = await client.handle_control({**command, "command_id": "unavailable"})
        run.assert_not_awaited()
        self.assertEqual(cast(dict[str, Any], failed)["type"], "command_reject")
        with (
            patch.object(client, "question_resolutions", new_callable=AsyncMock, return_value=[]),
            patch.object(client, "run_command", new_callable=AsyncMock) as run,
        ):
            open_reply = await client.handle_control({**command, "command_id": "still-open"})
        run.assert_awaited_once()
        self.assertEqual(cast(dict[str, Any], open_reply)["type"], "command_ack")

        async def change_conversation(_sources: object) -> list[dict[str, str]]:
            client.epoch = "next-conversation"
            return []

        with (
            patch.object(client, "question_resolutions", side_effect=change_conversation),
            patch.object(client, "run_command", new_callable=AsyncMock) as run,
        ):
            stale = await client.handle_control({**command, "command_id": "changed-conversation"})
        run.assert_not_awaited()
        self.assertEqual(cast(dict[str, Any], stale)["type"], "command_reject")
