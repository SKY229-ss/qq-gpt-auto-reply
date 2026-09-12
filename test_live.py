from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from gpt_bridge import GPTBridge, DISABLED
from live import Ledger, Service, normalize
from rules import GPT_NOTE, ReplyStatus


class LiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.events = []
        self.service = Service(Path(self.temp.name), lambda *args: self.events.append(args))
        self.service.connected = True
        self.service.heartbeat()
        self.sends = []

        async def send(**kwargs):
            self.sends.append(kwargs)
        self.service.client = SimpleNamespace(api=SimpleNamespace(post_c2c_message=send, post_group_message=send))

    def tearDown(self):
        self.service.ledger.db.close()
        self.temp.cleanup()

    def raw(self, sender="alice", mid="1", age=61):
        return SimpleNamespace(id=mid, timestamp=datetime.fromtimestamp(time.time()-age, timezone.utc).isoformat(),
                               author=SimpleNamespace(user_openid=sender, member_openid=sender),
                               group_openid="group-1", content="Hi")

    async def test_invalid_credentials_do_not_start_a_connection(self):
        invalid_credentials = (
            ("", "fake-secret"),
            ("not-a-number", "fake-secret"),
            ("../123", "fake-secret"),
            ("\u0661\u0662\u0663", "fake-secret"),
            ("123", ""),
            ("123", " \n\t"),
        )
        with patch("live.QQClient") as client, patch.object(Service, "run", new_callable=AsyncMock) as run:
            for app_id, secret in invalid_credentials:
                await self.service.connect(app_id, secret)
                self.assertIsNone(self.service.app_id)
                self.assertIsNone(self.service.task)
            client.assert_not_called()
            run.assert_not_called()
        self.assertEqual(sum(event == ("connection", False) for event in self.events), len(invalid_credentials))

    async def test_different_bots_keep_independent_delivery_ledgers(self):
        other = Service(Path(self.temp.name), lambda *args: None)
        try:
            with patch("live.QQClient"), patch.object(Service, "run", new_callable=AsyncMock):
                await self.service.connect("123", "fake-secret")
                await self.service.task
                await other.connect("456", "fake-secret")
                await other.task
            message = normalize(self.raw(), "private")
            self.assertTrue(self.service.ledger.remember(message))
            self.assertTrue(other.ledger.remember(message))
            self.assertFalse(self.service.ledger.remember(message))
            self.assertFalse(other.ledger.remember(message))
            for app_id in ("123", "456"):
                self.assertTrue((Path(self.temp.name) / app_id / "delivery.sqlite").is_file())
        finally:
            other.ledger.db.close()

    async def test_switching_bots_requires_a_new_session(self):
        with patch("live.QQClient") as client, patch.object(Service, "run", new_callable=AsyncMock) as run:
            await self.service.connect("123", "fake-secret")
            await self.service.task
            original_ledger = self.service.ledger
            original_client = self.service.client
            await self.service.connect("456", "fake-secret")
            self.assertEqual(self.service.app_id, "123")
            self.assertIs(self.service.ledger, original_ledger)
            self.assertIs(self.service.client, original_client)
            self.assertEqual(client.call_count, 1)
            self.assertEqual(run.await_count, 1)
        self.assertFalse((Path(self.temp.name) / "456").exists())
        self.assertIn(("connection", False), self.events)
        self.assertTrue(any(kind == "status" and "Close and reopen" in value for kind, value in self.events))

    async def test_default_mode_does_not_queue_automatic_replies(self):
        await self.service.receive(self.raw(), "private")
        self.assertEqual(len(self.service.entries), 1)
        self.assertEqual(self.service.workflow.pending, {})

    async def test_same_message_id_in_different_chats_is_isolated(self):
        await self.service.receive(self.raw("alice"), "private")
        await self.service.receive(self.raw("bob"), "private")
        self.assertEqual(len(self.service.entries), 2)
        self.assertEqual({e["message"].sender_id for e in self.service.entries.values()}, {"alice", "bob"})

    async def test_manual_reply_cancels_pending_without_gpt_call(self):
        self.service.set_auto(True)
        await self.service.receive(self.raw(), "private")
        await self.service.manual_reply(next(iter(self.service.entries)), "I can answer this")
        self.assertEqual(self.service.workflow.pending, {})
        self.assertEqual(self.sends[0]["content"], "I can answer this")

    async def test_auto_route_and_attribution(self):
        self.service.set_auto(True)
        await self.service.receive(self.raw(), "group")
        msg = next(iter(self.service.entries.values()))["message"]
        await self.service.send_auto(msg, "Got it!")
        self.assertEqual(self.sends[0]["group_openid"], "group-1")
        self.assertTrue(self.sends[0]["content"].endswith(GPT_NOTE))
        with self.assertRaises(RuntimeError):
            await self.service.send_auto(msg, "duplicate")
        self.assertEqual(len(self.sends), 1)

    async def test_frozen_panel_and_disconnection_withhold_replies(self):
        self.service.set_auto(True)
        await self.service.receive(self.raw(), "private")
        msg = next(iter(self.service.entries.values()))["message"]
        self.service.heartbeat_at = 0
        self.assertEqual(await self.service.check_human((msg,)), ReplyStatus.UNKNOWN)
        self.service.heartbeat()
        self.service.connected = False
        self.assertEqual(await self.service.check_human((msg,)), ReplyStatus.UNKNOWN)
        with self.assertRaises(RuntimeError):
            await self.service.send_auto(msg, "must not send")
        self.assertEqual(self.sends, [])

    async def test_gpt_failure_does_not_repeat_forever(self):
        self.service.set_auto(True)
        await self.service.receive(self.raw(), "private")
        calls = []

        async def fail(text):
            calls.append(text)
            raise RuntimeError("test failure")
        self.service.gpt.generate = fail
        for _ in range(2):
            await self.service.workflow.tick(now=time.time(), check_human=self.service.check_human,
                                             generate=self.service.generate, send=self.service.send_auto)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.sends, [])

    async def test_gpt_draft_suffix_survives_length_limit(self):
        await self.service.receive(self.raw(), "private")
        await self.service.manual_reply(next(iter(self.service.entries)), "x" * 2000, True)
        self.assertTrue(self.sends[0]["content"].endswith(GPT_NOTE))

    async def test_replayed_event_survives_restart(self):
        raw = self.raw()
        await self.service.receive(raw, "private")
        self.service.ledger.db.close()
        self.service.ledger = Ledger(Path(self.temp.name) / "delivery.sqlite")
        self.service.entries.clear()
        await self.service.receive(raw, "private")
        self.assertEqual(self.service.entries, {})

    async def test_old_event_and_ambiguous_timestamp_are_ignored(self):
        await self.service.receive(self.raw(age=400), "private")
        raw = self.raw()
        raw.timestamp = "2026-09-12T12:00:00"
        await self.service.receive(raw, "private")
        self.assertEqual(self.service.entries, {})

    async def test_user_pause_cancels_generation_before_send(self):
        self.service.set_auto(True)
        await self.service.receive(self.raw(), "private")

        async def draft(messages):
            self.service.set_auto(False)
            return "do not send"
        await self.service.workflow.tick(now=time.time(), check_human=self.service.check_human,
                                         generate=draft, send=self.service.send_auto)
        self.assertEqual(self.sends, [])


if __name__ == "__main__":
    unittest.main()
