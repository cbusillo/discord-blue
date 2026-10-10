"""Persisted restart recovery over real WebSockets and discord.py's HTTP client."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import aiohttp

from discord_blue.doodads.agent_session import bridge as bridge_module
from discord_blue.doodads.agent_session.store import SessionStore, StoredSession
from tests.fake_discord import BOT_ID, FakeDiscord, Fault
from tests.test_attach_scenarios import hello_for, marker, scenario, until
from tests.test_session_grace import CHURN, notices


class SessionStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_slow_older_write_cannot_replace_the_newest_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "agent-sessions.json"
            store = SessionStore(path)
            await store.start()
            original = store._write
            began, release = asyncio.Event(), asyncio.Event()
            loop = asyncio.get_running_loop()

            def slow_write(records: dict[str, StoredSession]) -> None:
                if records["session"].status == "live":
                    loop.call_soon_threadsafe(began.set)
                    asyncio.run_coroutine_threadsafe(release.wait(), loop).result()
                original(records)

            record = StoredSession(123, 456, "marker", "live", 0, time.time())
            with patch.object(store, "_write", new=slow_write):
                store.put("session", record)
                await asyncio.wait_for(began.wait(), 2)
                store.put("session", replace(record, status="grace", grace_until=time.time() + 300))
                store.put("session", replace(record, status="closed"))
                release.set()
                await store.close()
            recovered = SessionStore(path)
            await recovered.start()
            self.assertEqual(recovered.records["session"].status, "closed")
            self.assertEqual(list(path.parent.iterdir()), [path])
            await recovered.close()

    async def test_missing_corrupt_and_invalid_stores_fall_back_and_repair(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "agent-sessions.json"
            for content in (None, "{broken", "[]", '{"bad":{"thread_id":"wrong"}}'):
                if content is not None:
                    path.write_text(content)
                store = SessionStore(path)
                await store.start()
                self.assertEqual(store.records, {})
                record = StoredSession(123, None, "marker", "live", 0, time.time())
                store.put("repaired", record)
                await store.close()
                self.assertEqual(json.loads(path.read_text())["repaired"]["thread_id"], 123)
                path.unlink()

    async def test_a_failed_snapshot_is_reported_and_a_later_write_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            store = SessionStore(Path(home) / "agent-sessions.json")
            await store.start()
            record = StoredSession(123, None, "marker", "live", 0, time.time())
            with patch.object(store, "_write", side_effect=OSError("disk full")):
                store.put("session", record)
                with self.assertRaises(OSError):
                    await store.flush()
            store.put("session", replace(record, status="grace"))
            await store.flush()
            self.assertIsNone(store.error)
            await store.close()


class RestartRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_expired_hint_cannot_close_an_unrelated_thread(self) -> None:
        fake = FakeDiscord(latency=0.002)
        thread = fake.add_thread("other", marker=marker(hello_for("other")), members={BOT_ID})
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "agent-sessions.json"
            store = SessionStore(path)
            await store.start()
            store.put("hint", StoredSession(thread.id, None, marker(hello_for("hint")), "closing", 0, time.time()))
            await store.close()
            async with scenario(fake, store_path=path, listen_host="127.0.0.1", listen_port=0) as running:
                await running.bridge.start()
                await running.bridge.recover_stored_cleanups()
                self.assertFalse(thread.archived)
                self.assertEqual(notices(thread), [])
                self.assertNotIn("hint", running.bridge.store.records)

    async def test_a_discord_failure_during_store_lookup_does_not_create_another_thread(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("returning")
        thread = fake.add_thread("returning", marker=marker(hello), members={BOT_ID})
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "agent-sessions.json"
            store = SessionStore(path)
            await store.start()
            store.put("returning", StoredSession(thread.id, None, marker(hello), "live", 0, time.time()))
            await store.close()
            async with (
                scenario(fake, store_path=path, listen_host="127.0.0.1", listen_port=0) as running,
                aiohttp.ClientSession() as http,
            ):
                await running.bridge.start()
                fake.faults.append(Fault("GET", "/channels/{channel}", times=10, status=503))
                socket = await running.connect(http)
                await socket.send_json(hello)
                self.assertEqual((await socket.receive(timeout=10)).type, aiohttp.WSMsgType.CLOSE)
                self.assertEqual([item.id for item in fake.threads.values()], [thread.id])
                self.assertNotIn(("POST", "/channels/{channel}/threads"), fake.requests)

    async def test_a_reconnect_during_expiry_validation_prevents_cleanup(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("boundary")
        thread = fake.add_thread("boundary", marker=marker(hello), members={BOT_ID})
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "agent-sessions.json"
            store = SessionStore(path)
            await store.start()
            store.put("boundary", StoredSession(thread.id, None, marker(hello), "grace", 0, time.time()))
            await store.close()
            async with (
                scenario(fake, store_path=path, listen_host="127.0.0.1", listen_port=0) as running,
                aiohttp.ClientSession() as http,
            ):
                await running.bridge.start()
                record = running.bridge.store.records["boundary"]
                running.bridge.store.put("boundary", replace(record, grace_until=time.time() - 1))
                validating, release = asyncio.Event(), asyncio.Event()
                original = running.bridge.validated_stored_thread

                async def slow_validation(session_id: str, saved: StoredSession) -> object:
                    validating.set()
                    await release.wait()
                    return await original(session_id, saved)

                with patch.object(running.bridge, "validated_stored_thread", new=slow_validation):
                    expiry = asyncio.create_task(running.bridge.recover_stored_cleanups())
                    await asyncio.wait_for(validating.wait(), 2)
                    socket = await running.connect(http)
                    await socket.send_json(hello)
                    self.assertTrue(await until(lambda: running.bridge.sessions.get("boundary") is not None, 2))
                    release.set()
                    await expiry
                    await socket.receive_json(timeout=10)
                    self.assertEqual(running.archived, [])
                    self.assertEqual(notices(thread), [])
                    await socket.close()

    async def test_eight_sessions_survive_three_restarts_without_archive_or_discovery(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hellos = [hello_for(f"session-{index}") for index in range(8)]
        threads = [fake.add_thread(hello["session_id"], marker=marker(hello), members={BOT_ID}) for hello in hellos]
        with tempfile.TemporaryDirectory() as home:
            async with aiohttp.ClientSession() as http:
                path = Path(home) / "agent-sessions.json"
                for restart in range(4):
                    async with scenario(fake, store_path=path, listen_host="127.0.0.1", listen_port=0) as running:
                        await running.bridge.start()
                        if restart:
                            await running.bridge.cleanup_stale_session_threads()
                        before = len(fake.requests)
                        sockets = await asyncio.gather(*(running.connect(http) for _ in hellos))
                        await asyncio.gather(
                            *(
                                socket.send_json({**hello, "session_epoch": f"e{restart}"})
                                for socket, hello in zip(sockets, hellos, strict=True)
                            )
                        )
                        acknowledgements = await asyncio.gather(*(socket.receive_json(timeout=10) for socket in sockets))
                        if restart:
                            self.assertFalse(any("/threads/archived/" in route for _, route in fake.requests[before:]))
                        self.assertEqual([ack["thread_id"] for ack in acknowledgements], [thread.id for thread in threads])
                        self.assertEqual(running.archived, [])
                        if restart:
                            self.assertFalse(any(request in CHURN for request in fake.requests[before:]))
                        await running.bridge.stop()
                        for socket in sockets:
                            await socket.close()
                        self.assertEqual(running.archived, [])
                    self.assertTrue(path.exists())
                self.assertTrue(all(not thread.archived and not thread.locked and not notices(thread) for thread in threads))

    async def test_startup_grace_protects_threads_and_notifications_then_expires(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("gone-after-restart")
        thread = fake.add_thread("gone", marker=marker(hello), members={BOT_ID})
        with tempfile.TemporaryDirectory() as home:
            async with aiohttp.ClientSession() as http:
                path = Path(home) / "agent-sessions.json"
                async with scenario(fake, store_path=path, listen_host="127.0.0.1", listen_port=0) as first:
                    await first.bridge.start()
                    socket = await first.connect(http)
                    await socket.send_json(hello)
                    await socket.receive_json(timeout=10)
                    notification_id = first.bridge.sessions.get(hello["session_id"]).notification_message_id  # type: ignore[union-attr]
                    await first.bridge.stop()
                    await socket.close()
                async with scenario(fake, store_path=path, listen_host="127.0.0.1", listen_port=0) as second:
                    await second.bridge.start()
                    await second.bridge.cleanup_stale_session_notifications()
                    await second.bridge.cleanup_stale_session_threads()
                    self.assertEqual(notices(thread), [])
                    self.assertFalse(thread.archived)
                    self.assertTrue(any(message.id == notification_id for message in fake.parent_messages))
                    record = second.bridge.store.records[hello["session_id"]]
                    second.bridge.store.put(hello["session_id"], replace(record, grace_until=time.time() - 1))
                    await second.bridge.recover_stored_cleanups()
                    self.assertTrue(await until(lambda: thread.archived and thread.locked, 5))
                    self.assertEqual(notices(thread), [bridge_module.SESSION_ENDED_NOTICE])
                    await second.bridge.recover_stored_cleanups()
                    self.assertEqual(notices(thread), [bridge_module.SESSION_ENDED_NOTICE])

    async def test_deleted_thread_hint_is_replaced_and_missing_notification_is_recreated(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("deleted")
        thread = fake.add_thread("deleted", marker=marker(hello), members={BOT_ID})
        with tempfile.TemporaryDirectory() as home:
            async with aiohttp.ClientSession() as http:
                path = Path(home) / "agent-sessions.json"
                async with scenario(fake, store_path=path, listen_host="127.0.0.1", listen_port=0) as first:
                    await first.bridge.start()
                    socket = await first.connect(http)
                    await socket.send_json(hello)
                    await socket.receive_json(timeout=10)
                    await first.bridge.stop()
                    await socket.close()
                fake.delete_thread(thread.id)
                async with scenario(fake, store_path=path, listen_host="127.0.0.1", listen_port=0) as second:
                    await second.bridge.start()
                    socket = await second.connect(http)
                    await socket.send_json({**hello, "session_epoch": "e2"})
                    ack = await socket.receive_json(timeout=10)
                    self.assertNotEqual(ack["thread_id"], thread.id)
                    await second.bridge.stop()
                    await socket.close()
                fake.parent_messages.clear()
                async with scenario(fake, store_path=path, listen_host="127.0.0.1", listen_port=0) as third:
                    await third.bridge.start()
                    socket = await third.connect(http)
                    await socket.send_json({**hello, "session_epoch": "e3"})
                    self.assertEqual((await socket.receive_json(timeout=10))["thread_id"], ack["thread_id"])
                    self.assertEqual(len(fake.parent_messages), 1)
                    await socket.close()
