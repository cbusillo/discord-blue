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
        self.assertTrue(source.items() <= resolved[0].items())
        self.assertIn("Keep both.", resolved[0]["answer"])
        self.assertNotIn("<!--", resolved[0]["answer"])
        self.assertEqual(resolved[0]["decision_url"], source["url"].replace("-1", "-2"))
        with self.assertRaises(ValueError):
            await read_resolutions(READER, [{**source, "url": "https://github.com/cbusillo/example/issues/999"}])
        self.assertEqual(await read_resolutions(READER, [{**source, "number": "Q999"}]), [])
        self.assertEqual(
            await read_resolutions(
                READER, [{**source, "url": "https://github.com/cbusillo/example/issues/999"}, source], strict=False
            ),
            resolved,
        )

    async def test_a_conversation_change_during_lookup_prevents_native_delivery(self) -> None:
        client = AgentSessionClient(cast(Any, SimpleNamespace(question_reader=READER)), "session")
        command = {
            **client.event("command"),
            "command_id": "stale",
            "kind": "reply",
            "text": "Change it",
            "question_sources": [{"key": "card"}],
        }

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
