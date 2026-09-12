import asyncio
from dataclasses import replace
import unittest

from rules import GPT_NOTE, Message, ReplyStatus, Workflow


class RulesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.workflow = Workflow(owner_id="owner", bot_id="bot")
        self.message = Message("private", "chat", "alice", "m1", 0, "hi")
        self.deliveries = []

    async def status(self, messages):
        return ReplyStatus.UNANSWERED

    async def generate(self, messages):
        return "Got it!"

    async def send(self, anchor, content):
        self.deliveries.append((anchor, content))

    async def tick(self, now=60, **overrides):
        args = dict(now=now, check_human=self.status, generate=self.generate, send=self.send)
        args.update(overrides)
        return await self.workflow.tick(**args)

    async def test_private_waits_full_minute_and_discloses_gpt(self):
        self.workflow.ingest(self.message)
        self.assertEqual(await self.tick(59.999), [])
        self.assertEqual(await self.tick(60), ["m1"])
        self.assertTrue(self.deliveries[0][1].endswith(GPT_NOTE))
        self.assertEqual(await self.tick(120), [])

    async def test_group_requires_verified_owner_or_bot_mention(self):
        for target in ("owner", "bot"):
            msg = replace(self.message, scope="group", chat_id=target, mentioned_ids=frozenset([target]))
            self.assertTrue(self.workflow.ingest(msg))
        for index, targets in enumerate((frozenset(), frozenset(["someone_else"]), frozenset(["all"]))):
            msg = replace(self.message, scope="group", message_id=str(index), mentioned_ids=targets, content="@owner @bot")
            self.assertFalse(self.workflow.ingest(msg))
        await self.tick()
        self.assertEqual(len(self.deliveries), 2)

    async def test_human_reply_at_deadline_cancels(self):
        self.workflow.ingest(self.message)
        self.workflow.human_replied("private", "chat", at=60)
        self.assertEqual(await self.tick(), [])

    async def test_human_reply_during_generation_cancels(self):
        self.workflow.ingest(self.message)

        async def generate(messages):
            self.workflow.human_replied("private", "chat", at=60.1)
            return "late draft"

        self.assertEqual(await self.tick(generate=generate), [])

    async def test_final_status_check_cancels_even_without_local_event(self):
        self.workflow.ingest(self.message)
        states = iter([ReplyStatus.UNANSWERED, ReplyStatus.REPLIED])

        async def check(messages):
            return next(states)

        self.assertEqual(await self.tick(check_human=check), [])

    async def test_unknown_or_failed_monitor_does_not_send(self):
        self.workflow.ingest(self.message)

        async def unknown(messages):
            return ReplyStatus.UNKNOWN

        async def failed(messages):
            raise ConnectionError("monitor disconnected")

        self.assertEqual(await self.tick(check_human=unknown), [])
        self.assertEqual(await self.tick(check_human=failed), [])
        self.assertEqual(self.deliveries, [])

    async def test_duplicates_and_self_bot_events_do_not_trigger(self):
        self.assertTrue(self.workflow.ingest(self.message))
        self.assertFalse(self.workflow.ingest(self.message))
        for actor in ("owner", "bot", "unknown"):
            self.assertFalse(self.workflow.ingest(replace(self.message, message_id=actor, actor=actor)))
        self.assertFalse(self.workflow.ingest(replace(self.message, sender_id="owner", message_id="self")))
        await self.tick()
        self.assertEqual(len(self.deliveries), 1)
        self.assertFalse(self.workflow.ingest(self.message))

    async def test_followups_do_not_reset_first_timer(self):
        self.workflow.ingest(self.message)
        self.workflow.ingest(replace(self.message, message_id="m2", sent_at=50))
        self.assertEqual(await self.tick(60), ["m2"])
        self.assertEqual(len(self.deliveries), 1)

    async def test_group_sender_timers_are_independent(self):
        group = replace(self.message, scope="group", mentioned_ids=frozenset(["bot"]))
        self.workflow.ingest(group)
        self.workflow.ingest(replace(group, sender_id="bob", message_id="bob1", sent_at=10))
        self.workflow.human_replied("group", "chat", at=59, sender_id="alice")
        self.assertEqual(await self.tick(60), [])
        self.assertEqual(await self.tick(70), ["bob1"])

    async def test_reply_in_other_chat_does_not_cancel(self):
        self.workflow.ingest(self.message)
        self.workflow.human_replied("private", "elsewhere", at=59)
        self.assertEqual(await self.tick(), ["m1"])

    async def test_message_after_human_reply_starts_new_timer(self):
        self.workflow.ingest(self.message)
        self.workflow.human_replied("private", "chat", at=20)
        self.workflow.ingest(replace(self.message, message_id="m2", sent_at=21))
        self.assertEqual(await self.tick(60), [])
        self.assertEqual(await self.tick(81), ["m2"])

    async def test_empty_generation_does_not_send(self):
        self.workflow.ingest(self.message)

        async def empty(messages):
            return "  "

        self.assertEqual(await self.tick(generate=empty), [])

    async def test_concurrent_ticks_cannot_send_duplicate(self):
        self.workflow.ingest(self.message)

        async def slow(messages):
            await asyncio.sleep(0)
            return "hello"

        await asyncio.gather(self.tick(generate=slow), self.tick(generate=slow))
        self.assertEqual(len(self.deliveries), 1)

    async def test_uncertain_delivery_is_not_automatically_retried(self):
        self.workflow.ingest(self.message)

        async def uncertain(anchor, content):
            raise TimeoutError("delivery outcome unknown")

        with self.assertRaises(TimeoutError):
            await self.tick(send=uncertain)
        self.assertEqual(await self.tick(120), [])


if __name__ == "__main__":
    unittest.main()
