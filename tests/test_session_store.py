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
from tests.fake_discord import BOT_ID, PARENT_ID, FakeDiscord, FakeMessage, Fault
from tests.test_attach_scenarios import hello_for, marker, scenario, until
from tests.test_session_grace import CHURN, notices
from tests.test_session_cleanup_failures import make_bridge


class SessionStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_crashed_writer_is_unhealthy_and_can_be_restarted(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            store = SessionStore(Path(home) / "agent-sessions.json")
            await store.start()
            record = StoredSession(123, None, "marker", "live", 0, time.time())
            with patch.object(store, "_write", side_effect=RuntimeError("executor unavailable")):
                store.put("session", record)
                with self.assertRaises(RuntimeError):
                    await asyncio.wait_for(store.flush(), 1)
                self.assertTrue(store.unhealthy(bridge_module.MAINTENANCE_INTERVAL_SECONDS))
                with self.assertRaises(RuntimeError):
                    await store.close()
            await store.start()
            store.put("session", record)
            await store.flush()
            self.assertFalse(store.unhealthy(bridge_module.MAINTENANCE_INTERVAL_SECONDS))
            await store.close()

    async def test_reloading_a_bridge_waits_for_its_previous_writer_to_finish(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "agent-sessions.json"
            first = bridge_module.AgentSessionBridge(make_bridge().bot, store_path=path).store
            await first.start()
            original = first._write
            writing, release = asyncio.Event(), asyncio.Event()
            loop = asyncio.get_running_loop()

            def slow_write(records: dict[str, StoredSession]) -> None:
                loop.call_soon_threadsafe(writing.set)
                asyncio.run_coroutine_threadsafe(release.wait(), loop).result()
                original(records)

            record = StoredSession(123, None, "marker", "live", 0, time.time())
            with patch.object(first, "_write", new=slow_write):
                first.put("session", record)
                await asyncio.wait_for(writing.wait(), 1)
                stopping = asyncio.create_task(first.close())
                await asyncio.sleep(0)
                next_store = bridge_module.AgentSessionBridge(make_bridge().bot, store_path=path).store
                starting = asyncio.create_task(next_store.start())
                await asyncio.sleep(0.02)
                self.assertFalse(starting.done(), "a new writer started while the old snapshot was still in flight")
                release.set()
                await asyncio.gather(stopping, starting)
            next_store.put("session", replace(record, thread_id=456))
            await next_store.close()
            self.assertEqual(json.loads(path.read_text())["session"]["thread_id"], 456)

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
                self.assertTrue(store.unhealthy(0), "a writer exceeding its progress budget was still healthy")
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
            store.retry_failed_write()
            await store.flush()
            self.assertIsNone(store.error)
            await store.close()


class RestartRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_forbidden_stored_hint_falls_back_to_discovery(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("lost-access")
        old = fake.add_thread("lost-access", marker=marker(hello), members={BOT_ID})
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "agent-sessions.json"
            store = SessionStore(path)
            await store.start()
            store.put(hello["session_id"], StoredSession(old.id, None, marker(hello), "live", 0, time.time()))
            await store.close()
            async with (
                scenario(fake, store_path=path, listen_host="127.0.0.1", listen_port=0) as running,
                aiohttp.ClientSession() as http,
            ):
                await running.bridge.start()

                def match(ids: dict[str, str]) -> bool:
                    return int(ids["channel"]) == old.id

                fake.faults.extend(
                    [
                        Fault("GET", "/channels/{channel}", times=10, status=403, match=match),
                        Fault("GET", "/channels/{channel}/messages", times=10, status=403, match=match),
                    ]
                )
                socket = await running.connect(http)
                await socket.send_json(hello)
                ack = await socket.receive_json(timeout=5)
                self.assertNotEqual(ack["thread_id"], old.id)
                await socket.close()

    async def test_a_slow_recovery_read_does_not_block_the_next_record_or_get_cancelled(self) -> None:
        fake = FakeDiscord(latency=0.002)
        first_hello, next_hello = hello_for("slow"), hello_for("ready")
        first = fake.add_thread("slow", marker=marker(first_hello), members={BOT_ID})
        second = fake.add_thread("ready", marker=marker(next_hello), members={BOT_ID})
        with tempfile.TemporaryDirectory() as home:
            async with scenario(
                fake, store_path=Path(home) / "agent-sessions.json", listen_host="127.0.0.1", listen_port=0
            ) as running:
                await running.bridge.start()
                for hello, thread in ((first_hello, first), (next_hello, second)):
                    running.bridge.store.put(
                        hello["session_id"], StoredSession(thread.id, None, marker(hello), "closing", 0, time.time())
                    )
                release, completed = asyncio.Event(), asyncio.Event()
                cancelled = False
                original = running.bridge.validated_stored_thread

                async def slow_read(session_id: str, record: StoredSession) -> object:
                    nonlocal cancelled
                    if session_id == "slow":
                        try:
                            await release.wait()
                        except asyncio.CancelledError:
                            cancelled = True
                            raise
                        completed.set()
                    return await original(session_id, record)

                try:
                    with (
                        patch.object(running.bridge, "validated_stored_thread", new=slow_read),
                        patch.object(bridge_module, "THREAD_LOOKUP_TIMEOUT_SECONDS", 0.03),
                    ):
                        await asyncio.wait_for(running.bridge.recover_stored_cleanups(), 1)
                    self.assertTrue(second.archived)
                    self.assertFalse(cancelled)
                finally:
                    release.set()
                    await asyncio.wait_for(completed.wait(), 1)

    async def test_restart_retries_only_remaining_close_steps(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("partly-closed")
        thread = fake.add_thread("partly-closed", marker=marker(hello), archived=True, locked=True, members={7})
        thread.messages.append(FakeMessage(next(fake.ids), thread.id, bridge_module.SESSION_ENDED_NOTICE))
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "agent-sessions.json"
            store = SessionStore(path)
            await store.start()
            store.put(hello["session_id"], StoredSession(thread.id, None, marker(hello), "closing", 0, time.time(), ("members",)))
            await store.close()
            async with scenario(fake, store_path=path, listen_host="127.0.0.1", listen_port=0) as running:
                await running.bridge.start()
                await running.bridge.recover_stored_cleanups()
                self.assertEqual(notices(thread), [bridge_module.SESSION_ENDED_NOTICE])
                self.assertEqual(thread.members, set())
                self.assertTrue(thread.archived and thread.locked)

    async def test_a_stalled_store_flush_does_not_block_session_acknowledgements(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hellos = [hello_for("first"), hello_for("next")]
        for hello in hellos:
            fake.add_thread(hello["session_id"], marker=marker(hello), members={BOT_ID})
        with tempfile.TemporaryDirectory() as home:
            async with (
                scenario(fake, store_path=Path(home) / "agent-sessions.json", listen_host="127.0.0.1", listen_port=0) as running,
                aiohttp.ClientSession() as http,
            ):
                await running.bridge.start()
                release = asyncio.Event()

                original = running.bridge.store._write
                loop = asyncio.get_running_loop()

                def stalled_write(records: dict[str, StoredSession]) -> None:
                    asyncio.run_coroutine_threadsafe(release.wait(), loop).result()
                    original(records)

                sockets = [await running.connect(http) for _ in hellos]
                try:
                    with (
                        patch.object(running.bridge.store, "_write", new=stalled_write),
                        patch.object(bridge_module, "SESSION_STORE_WAIT_TIMEOUT_SECONDS", 0.03),
                    ):
                        for socket, hello in zip(sockets, hellos, strict=True):
                            await socket.send_json(hello)
                        acknowledgements = await asyncio.gather(*(socket.receive_json(timeout=1) for socket in sockets))
                        self.assertTrue(all(ack["type"] == "hello_ack" for ack in acknowledgements))
                        self.assertIsNone(running.bridge.store.error, "a pending snapshot was reported as a failed write")
                finally:
                    release.set()
                    for socket in sockets:
                        await socket.close()

    async def test_an_exhausted_cleanup_does_not_reannounce_the_session_end(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("refused-member")
        thread = fake.add_thread("refused-member", marker=marker(hello), members={BOT_ID, 7})
        with tempfile.TemporaryDirectory() as home:
            async with scenario(
                fake, store_path=Path(home) / "agent-sessions.json", listen_host="127.0.0.1", listen_port=0
            ) as running:
                await running.bridge.start()
                running.bridge.store.put(
                    hello["session_id"], StoredSession(thread.id, None, marker(hello), "closing", 0, time.time())
                )
                fake.faults.append(Fault("DELETE", "/channels/{channel}/thread-members/{member}", times=100, status=403))
                for _ in range(bridge_module.PENDING_CLEANUP_MAX_ATTEMPTS + 2):
                    await running.bridge.recover_stored_cleanups()
                    await running.bridge.retry_pending_cleanups()
                self.assertEqual(notices(thread), [bridge_module.SESSION_ENDED_NOTICE])

    async def test_a_forbidden_stored_thread_does_not_block_another_cleanup(self) -> None:
        fake = FakeDiscord(latency=0.002)
        bad, good = hello_for("bad"), hello_for("good")
        first = fake.add_thread("bad", marker=marker(bad), members={BOT_ID})
        second = fake.add_thread("good", marker=marker(good), members={BOT_ID})
        with tempfile.TemporaryDirectory() as home:
            async with scenario(
                fake, store_path=Path(home) / "agent-sessions.json", listen_host="127.0.0.1", listen_port=0
            ) as running:
                await running.bridge.start()
                for hello, thread in ((bad, first), (good, second)):
                    running.bridge.store.put(
                        hello["session_id"], StoredSession(thread.id, None, marker(hello), "closing", 0, time.time())
                    )
                fault = Fault("GET", "/channels/{channel}", times=100, status=403, match=lambda ids: int(ids["channel"]) == first.id)
                fake.faults.append(fault)
                for _ in range(bridge_module.PENDING_CLEANUP_MAX_ATTEMPTS + 2):
                    await running.bridge.recover_stored_cleanups()
                self.assertTrue(second.archived)
                self.assertFalse(first.archived)
                self.assertLessEqual(100 - fault.times, bridge_module.PENDING_CLEANUP_MAX_ATTEMPTS)

    async def test_a_notification_failure_does_not_prevent_a_stored_session_attaching(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("parent-unavailable")
        thread = fake.add_thread("parent-unavailable", marker=marker(hello), members={BOT_ID})
        notice = FakeMessage(next(fake.ids), PARENT_ID, f"Agent session connected for `repo`: <#{thread.id}>")
        fake.parent_messages.append(notice)
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "agent-sessions.json"
            store = SessionStore(path)
            await store.start()
            store.put(hello["session_id"], StoredSession(thread.id, notice.id, marker(hello), "live", 0, time.time()))
            await store.close()
            async with (
                scenario(fake, store_path=path, listen_host="127.0.0.1", listen_port=0) as running,
                aiohttp.ClientSession() as http,
            ):
                await running.bridge.start()
                fake.faults.append(Fault("GET", "/channels/{channel}/messages/{message}", times=10, status=403))
                socket = await running.connect(http)
                await socket.send_json(hello)
                self.assertEqual((await socket.receive_json(timeout=5))["thread_id"], thread.id)
                await socket.close()

    async def test_duplicate_live_notifications_are_cleaned_with_persistence_enabled(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("notices")
        fake.add_thread("notices", marker=marker(hello), members={BOT_ID})
        with tempfile.TemporaryDirectory() as home:
            async with (
                scenario(fake, store_path=Path(home) / "agent-sessions.json", listen_host="127.0.0.1", listen_port=0) as running,
                aiohttp.ClientSession() as http,
            ):
                await running.bridge.start()
                socket = await running.connect(http)
                await socket.send_json(hello)
                await socket.receive_json(timeout=10)
                notice = fake.parent_messages[0]
                fake.parent_messages.append(FakeMessage(next(fake.ids), PARENT_ID, notice.content))
                await running.bridge.cleanup_stale_session_notifications()
                self.assertEqual([message.id for message in fake.parent_messages], [notice.id])
                await socket.close()

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
