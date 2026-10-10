from __future__ import annotations

import json
from typing import Any
import unittest

from discord_blue.doodads.agent_session.protocol import RemoteApprovalRequest
from discord_blue.doodads.agent_session.bridge import AgentSessionBridge
from tests.test_attach_scenarios import until
from tests.test_codex_bridge import FakeRpc, command, running_bridge, thread


def file_item(diff: str = "+whole file\n") -> dict[str, Any]:
    return {
        "type": "fileChange",
        "id": "item",
        "status": "inProgress",
        "changes": [{"path": "/work/new.txt", "kind": {"type": "add"}, "diff": diff}],
    }


class SliceTwoTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_types_show_full_content_and_answer_exact_request_once(self) -> None:
        for kind in ("file_change", "permissions"):
            for decision in ("approved", "denied"):
                with self.subTest(kind=kind, decision=decision):
                    rpc = FakeRpc(thread("root"))
                    async with running_bridge(rpc) as (bridge, discord):
                        await discord.next("hello")
                        self.assertTrue(await until(lambda: bridge.sessions["root"].server_features is not None, 2))
                        params: dict[str, Any] = {"threadId": "root", "turnId": "turn", "itemId": "item", "reason": "Testing"}
                        if kind == "file_change":
                            await bridge.dispatch({"method": "item/started", "params": {**params, "item": file_item()}})
                            method = "item/fileChange/requestApproval"
                            expected: dict[str, Any] = {"decision": "accept" if decision == "approved" else "decline"}
                        else:
                            method = "item/permissions/requestApproval"
                            params |= {
                                "cwd": "/work",
                                "permissions": {"network": {"enabled": True}, "fileSystem": {"write": ["/work"]}},
                            }
                            expected = {"permissions": params["permissions"] if decision == "approved" else {}, "scope": "turn"}
                        await bridge.dispatch({"id": 42, "method": method, "params": params})
                        approval = await discord.next("approval_request")
                        display = AgentSessionBridge.format_approval_request(RemoteApprovalRequest.from_payload(approval))
                        self.assertIn(approval["content_text"], display)
                        self.assertEqual(json.loads(approval["content_text"])["request"], params)
                        self.assertEqual(rpc.responses, [])
                        msg = {
                            "type": "approval_decision",
                            "approval_id": approval["approval_id"],
                            "decision": decision,
                            "session_id": "root",
                            "session_epoch": bridge.sessions["root"].epoch,
                        }
                        self.assertEqual((await discord.control(msg))["type"], "approval_decision_ack")
                        self.assertEqual((await discord.control(msg))["type"], "approval_decision_reject")
                        self.assertEqual(rpc.responses, [(42, expected)])

    async def test_missing_unknown_oversized_fenced_and_session_scope_stay_local(self) -> None:
        cases: list[tuple[str | None, dict[str, Any]]] = [
            (None, {}),
            ("+ok", {"futureGrant": True}),
            ("+ok", {"grantRoot": "/"}),
            ("x" * 2000, {}),
            ("+fenced", {}),
        ]
        for diff, extra in cases:
            with self.subTest(diff=diff, extra=extra):
                rpc = FakeRpc(thread("root"))
                async with running_bridge(rpc) as (bridge, discord):
                    await discord.next("hello")
                    self.assertTrue(await until(lambda: bridge.sessions["root"].server_features is not None, 2))
                    session = bridge.sessions["root"]
                    if diff is not None:
                        session.on_file_item("t", file_item(diff if diff != "+fenced" else "+" + chr(96) * 3))
                    session.on_request(
                        1, "item/fileChange/requestApproval", {"threadId": "root", "turnId": "t", "itemId": "item", **extra}
                    )
                    self.assertEqual((await discord.next("status_changed"))["message"], "Waiting on a decision in the Codex TUI")
                    self.assertEqual(session.approvals, {})
                    self.assertEqual(rpc.responses, [])

    async def test_changed_patch_or_local_resolution_invalidates_old_decision(self) -> None:
        for changed in (True, False):
            rpc = FakeRpc(thread("root"))
            async with running_bridge(rpc) as (bridge, discord):
                await discord.next("hello")
                self.assertTrue(await until(lambda: bridge.sessions["root"].server_features is not None, 2))
                session = bridge.sessions["root"]
                session.on_file_item("t", file_item("+old"))
                session.on_request(1, "item/fileChange/requestApproval", {"threadId": "root", "turnId": "t", "itemId": "item"})
                approval = await discord.next("approval_request")
                if changed:
                    session.on_file_item("t", file_item("+new"))
                else:
                    session.on_resolved(1)
                msg = {
                    "type": "approval_decision",
                    "approval_id": approval["approval_id"],
                    "decision": "approved",
                    "session_id": "root",
                    "session_epoch": session.epoch,
                }
                self.assertEqual((await discord.control(msg))["type"], "approval_decision_reject")
                self.assertEqual(rpc.responses, [])

    async def test_old_server_never_receives_content_approval(self) -> None:
        rpc = FakeRpc(thread("root"))
        async with running_bridge(rpc, ["command_text"]) as (bridge, discord):
            await discord.next("hello")
            self.assertTrue(await until(lambda: bridge.sessions["root"].server_features is not None, 2))
            session = bridge.sessions["root"]
            session.on_request(
                1,
                "item/permissions/requestApproval",
                {"threadId": "root", "turnId": "t", "itemId": "item", "cwd": "/work", "permissions": {"network": {"enabled": True}}},
            )
            await discord.next("status_changed")
            self.assertEqual(session.approvals, {})

    async def test_new_session_uses_selected_threads_folder_and_stays_loaded(self) -> None:
        rpc = FakeRpc(thread("root", cwd="/work/chosen"))
        async with running_bridge(rpc) as (bridge, discord):
            await discord.next("hello")
            self.assertTrue(await until(lambda: bridge.sessions["root"].server_features is not None, 2))
            msg = command(bridge.sessions["root"], "new", "new_session")
            self.assertEqual((await discord.control(msg))["type"], "command_ack")
            self.assertEqual((await discord.control(msg))["type"], "command_ack")
            hello = await discord.next("hello")
            self.assertEqual((hello["session_id"], hello["cwd"]), ("new-thread", "/work/chosen"))
            self.assertEqual(rpc.called("thread/start"), [{"cwd": "/work/chosen"}])
            created = bridge.sessions["new-thread"]
            await created.release()
            self.assertTrue(created.subscribed)
            self.assertNotIn({"threadId": "new-thread"}, rpc.called("thread/unsubscribe"))

    async def test_replayed_patch_is_read_from_the_exact_turn_and_item(self) -> None:
        class ReplayRpc(FakeRpc):
            async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
                if method == "thread/items/list":
                    self.calls.append((method, params))
                    return {
                        "data": [{"turnId": "other", "item": file_item("+wrong")}, {"turnId": "turn", "item": file_item("+right")}],
                        "nextCursor": None,
                    }
                return await super().request(method, params)

        rpc = ReplayRpc(thread("root"))
        async with running_bridge(rpc) as (bridge, discord):
            await discord.next("hello")
            self.assertTrue(await until(lambda: bridge.sessions["root"].server_features is not None, 2))
            bridge.sessions["root"].catch_up({"id": "turn", "status": "inProgress", "items": [file_item("+summary")]})
            await bridge.dispatch(
                {
                    "id": 4,
                    "method": "item/fileChange/requestApproval",
                    "params": {"threadId": "root", "turnId": "turn", "itemId": "item"},
                }
            )
            approval = await discord.next("approval_request")
            self.assertIn("+right", approval["content_text"])
            self.assertNotIn("+wrong", approval["content_text"])
            self.assertNotIn("+summary", approval["content_text"])
            self.assertEqual((rpc.called("thread/items/list")[0] or {})["turnId"], "turn")

    async def test_unknown_permission_scope_and_stale_epoch_cannot_grant(self) -> None:
        rpc = FakeRpc(thread("root"))
        async with running_bridge(rpc) as (bridge, discord):
            await discord.next("hello")
            self.assertTrue(await until(lambda: bridge.sessions["root"].server_features is not None, 2))
            session = bridge.sessions["root"]
            base = {"threadId": "root", "turnId": "t", "itemId": "item", "cwd": "/work"}
            session.on_request(1, "item/permissions/requestApproval", {**base, "permissions": {"network": {"future": True}}})
            await discord.next("status_changed")
            self.assertFalse(session.approvals)
            session.on_request(2, "item/permissions/requestApproval", {**base, "permissions": {"network": {"enabled": True}}})
            approval = await discord.next("approval_request")
            response = await discord.control(
                {
                    "type": "approval_decision",
                    "approval_id": approval["approval_id"],
                    "decision": "approved",
                    "session_id": "root",
                    "session_epoch": "old",
                }
            )
            self.assertEqual(response["type"], "approval_decision_reject")
            self.assertEqual(rpc.responses, [])

    async def test_pending_content_waits_for_negotiation_and_old_servers_drop_it(self) -> None:
        from discord_blue.codex_bridge.config import BridgeConfig
        from discord_blue.codex_bridge.session import ThreadSession
        from pathlib import Path

        for features in (frozenset({"approval_content", "command_text"}), frozenset({"command_text"})):
            rpc = FakeRpc(thread("root"))
            session = ThreadSession(
                BridgeConfig("ws://127.0.0.1/agent-session/connect", "fake", Path("/unused"), "test"), rpc, thread("root"), {}
            )
            session.on_request(
                1,
                "item/permissions/requestApproval",
                {
                    "threadId": "root",
                    "turnId": "turn",
                    "itemId": "item",
                    "cwd": "/work",
                    "permissions": {"network": {"enabled": True}},
                },
            )
            self.assertTrue(session.prompts)
            self.assertEqual(rpc.responses, [])
            session.server_features = features
            session.on_server_features()
            self.assertEqual(bool(session.prompts), "approval_content" in features)
            self.assertEqual(bool(session.approvals), "approval_content" in features)

    async def test_owned_idle_thread_resubscribes_after_reconnect_and_closure_prunes_it(self) -> None:
        rpc = FakeRpc(thread("root"))
        async with running_bridge(rpc) as (bridge, discord):
            await discord.next("hello")
            await bridge.start_session("/work")
            await discord.next("hello")
            await bridge.detach("new-thread")
            await bridge.join("new-thread")
            await discord.next("hello")
            self.assertTrue(bridge.sessions["new-thread"].subscribed)
            await bridge.dispatch({"method": "thread/closed", "params": {"threadId": "new-thread"}})
            self.assertNotIn("new-thread", bridge.owned_threads)

    async def test_completed_patch_is_removed_without_a_false_change_notice(self) -> None:
        rpc = FakeRpc(thread("root"))
        async with running_bridge(rpc) as (bridge, discord):
            await discord.next("hello")
            self.assertTrue(await until(lambda: bridge.sessions["root"].server_features is not None, 2))
            session = bridge.sessions["root"]
            item = file_item()
            session.on_file_item("t", item)
            session.on_request(1, "item/fileChange/requestApproval", {"threadId": "root", "turnId": "t", "itemId": "item"})
            await discord.next("approval_request")
            item["status"] = "completed"
            session.on_file_item("t", item)
            self.assertFalse(session.file_items)
            self.assertFalse(session.approvals)
            self.assertFalse(any(e["type"] == "notice" for e in session.outbox))

    async def test_failed_new_session_setup_releases_its_subscription(self) -> None:
        from discord_blue.codex_bridge.rpc import RpcError

        class BrokenSetupRpc(FakeRpc):
            async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
                if method == "thread/read" and (params or {}).get("threadId") == "new-thread":
                    raise RpcError(-1)
                return await super().request(method, params)

        rpc = BrokenSetupRpc(thread("root"))
        async with running_bridge(rpc) as (bridge, discord):
            await discord.next("hello")
            result = await discord.control(command(bridge.sessions["root"], "new", "new_session"))
            self.assertEqual(result["type"], "command_reject")
            self.assertIn("new-thread", result["reason"])
            self.assertNotIn("new-thread", bridge.owned_threads)
            self.assertIn({"threadId": "new-thread"}, rpc.called("thread/unsubscribe"))


class LocalDecisionStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_decision_resolution_publishes_resumed_status(self) -> None:
        rpc = FakeRpc(thread("root"))
        async with running_bridge(rpc) as (bridge, discord):
            await discord.next("hello")
            session = bridge.sessions["root"]
            session.on_request(
                10, "item/commandExecution/requestApproval", {"threadId": "root", "turnId": "t", "itemId": "item", "command": None}
            )
            waiting = await discord.next("status_changed")
            self.assertIn("Waiting", waiting["message"])
            session.on_server_request_resolved(99)  # An unrelated resolution cannot clear the wait.
            session.on_server_request_resolved(10)
            resumed = await discord.next("status_changed")
            self.assertIn("continuing", resumed["message"])
            self.assertEqual(session.local_decisions, set())
