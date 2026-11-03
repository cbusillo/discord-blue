from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from typing import Any, cast

from discord_blue.agent_client import AgentSessionClient
from discord_blue.question_resolutions import read_resolutions


READER = Path(__file__).parent / "fixtures/question_reader.py"


class QuestionResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_matches_the_question_and_preserves_its_recorded_answer(self) -> None:
        source = {"key": "card", "number": "Q1218", "url": "https://github.com/cbusillo/example/issues/1", "revision": "0"}
        pending = {**source, "number": "Q1219"}
        resolved = await read_resolutions(READER, [source, pending])
        self.assertEqual(len(resolved), 1)
        self.assertEqual(
            resolved[0],
            {**source, "answer": "Keep both.", "decision_url": "https://github.com/cbusillo/example/issues/1#issuecomment-2"},
        )
        with self.assertRaises(ValueError):
            await read_resolutions(READER, [{**source, "url": "https://github.com/cbusillo/example/issues/999"}])
        with self.assertRaises(ValueError):
            await read_resolutions(READER, [{**source, "number": "Q999"}])

    async def test_a_late_card_reply_is_not_sent_to_either_native_harness(self) -> None:
        client = AgentSessionClient(cast(Any, object()), "session")
        source = {"key": "card", "number": "Q1218", "url": "https://github.com/cbusillo/example/issues/1", "revision": "0"}
        resolved = {**source, "answer": "Keep both.", "decision_url": f"{source['url']}#issuecomment-2"}
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
