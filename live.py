"""Official QQ transport and a local human reply channel. No personal QQ login."""
import asyncio
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
import sqlite3
import time
import logging as std_logging

import botpy

from gpt_bridge import GPTBridge
from rules import Message, ReplyStatus, Workflow, attributed

REPLY_WINDOW = 270  # Leave a margin within the SDK's documented five minutes.


class Ledger:
    """Own delivery metadata only: never message text or QQ chat history."""
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, state TEXT, ts REAL)")
        self.db.execute("DELETE FROM events WHERE ts < ?", (time.time() - 86400,))
        self.db.commit()

    @staticmethod
    def key(message):
        return f"{message.scope}:{message.chat_id}:{message.message_id}"

    def remember(self, message):
        result = self.db.execute("INSERT OR IGNORE INTO events VALUES (?, 'received', ?)",
                                 (self.key(message), time.time()))
        self.db.commit()
        return result.rowcount == 1

    def claim(self, message):
        result = self.db.execute("UPDATE events SET state='sending' WHERE id=? AND state='received'",
                                 (self.key(message),))
        self.db.commit()
        return result.rowcount == 1

    def finish(self, message, state):
        self.db.execute("UPDATE events SET state=? WHERE id=?", (state, self.key(message)))
        self.db.commit()


def normalize(raw, scope):
    stamp = datetime.fromisoformat(str(raw.timestamp).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("QQ event timestamp has no timezone")
    sender = raw.author.user_openid if scope == "private" else raw.author.member_openid
    chat = sender if scope == "private" else raw.group_openid
    if not sender or not chat or not raw.id:
        raise ValueError("Incomplete QQ event")
    return Message(scope, str(chat), str(sender), str(raw.id), stamp.timestamp(),
                   str(raw.content or ""),
                   frozenset(["official-bot"]) if scope == "group" else frozenset())


class Service:
    def __init__(self, state_dir: Path, emit):
        state_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir = state_dir
        self.app_id = None
        self.emit = emit
        self.ledger = Ledger(state_dir / "delivery.sqlite")
        self.gpt = GPTBridge(state_dir / "reply-cwd")
        # Human operator is authenticated by the local desktop session. Their
        # replies must come through this panel in the explicitly enabled mode.
        self.workflow = Workflow(owner_id="local-human-operator", bot_id="official-bot")
        self.entries = OrderedDict()
        self.auto = False
        self.connected = False
        self.heartbeat_at = 0.0
        self.held = set()
        self.dispatch_lock = asyncio.Lock()
        self.client = None
        self.task = None

    def status(self, text):
        self.emit("status", text)

    def heartbeat(self):
        self.heartbeat_at = time.monotonic()

    def set_auto(self, enabled):
        self.auto = bool(enabled and self.connected)
        for pending in self.workflow.pending.values():
            pending.cancelled = True
        self.workflow.pending.clear()
        self.status("Auto replies enabled for new bot messages" if self.auto else "Auto replies paused")

    async def connect(self, app_id, secret):
        if self.task and not self.task.done():
            return
        if not app_id.isascii() or not app_id.isdigit() or not secret.strip():
            self.status("Enter a numeric AppID and the matching AppSecret.")
            self.emit("connection", False)
            return
        if self.app_id is not None and self.app_id != app_id:
            self.status("Close and reopen this window before connecting a different bot.")
            self.emit("connection", False)
            return
        if self.app_id is None:
            bot_dir = self.state_dir / app_id
            bot_dir.mkdir(parents=True, exist_ok=True)
            self.ledger.db.close()
            self.ledger = Ledger(bot_dir / "delivery.sqlite")
            self.app_id = app_id
        self.status("Connecting to official QQ servers…")
        self.client = QQClient(self)
        self.task = asyncio.create_task(self.run(app_id, secret))

    async def run(self, app_id, secret):
        ticker = asyncio.create_task(self.ticker())
        try:
            async with self.client:
                await self.client.start(appid=app_id, secret=secret)
        except Exception as exc:
            self.status("QQ connection failed (" + type(exc).__name__ + "). Check AppSecret and QQ permissions.")
        finally:
            self.connected = False
            self.auto = False
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
            self.emit("connection", False)

    async def disconnect(self):
        self.set_auto(False)
        self.connected = False
        if self.client:
            await self.client.close()
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.emit("connection", False)
        self.status("Disconnected; no messages will be sent")

    async def receive(self, raw, scope):
        try:
            message = normalize(raw, scope)
        except (ValueError, TypeError, AttributeError):
            self.status("Ignored an incomplete QQ event")
            return
        now = time.time()
        if not (0 <= now - message.sent_at < REPLY_WINDOW):
            return
        if not self.ledger.remember(message):
            return
        event_key = self.ledger.key(message)
        self.entries[event_key] = {"message": message, "state": "waiting" if self.auto else "paused"}
        while len(self.entries) > 200:
            old_id, old = self.entries.popitem(last=False)
            pending = self.workflow.pending.pop(old["message"].key, None)
            if pending:
                pending.cancelled = True
        self.emit("message", (event_key, message, self.entries[event_key]["state"]))
        if self.auto:
            self.workflow.ingest(message)
            # Durable ledger already rejects all repeated events; avoid an
            # additional unbounded in-memory duplicate index in a long run.
            self.workflow.seen.clear()

    async def check_human(self, messages):
        if not self.connected or not self.auto or time.monotonic() - self.heartbeat_at > 4:
            return ReplyStatus.UNKNOWN
        if messages[0].key in self.held:
            return ReplyStatus.UNKNOWN
        for message in messages:
            item = self.entries.get(self.ledger.key(message))
            if not item:
                return ReplyStatus.UNKNOWN
            if item["state"] in ("human replied", "cancelled"):
                return ReplyStatus.REPLIED
        return ReplyStatus.UNANSWERED

    async def generate(self, messages):
        try:
            return await self.gpt.generate("\n".join(m.content for m in messages))
        except Exception:
            pending = self.workflow.pending.pop(messages[0].key, None)
            if pending:
                pending.cancelled = True
            for message in messages:
                self.update(message, "GPT failed; draft manually to retry")
            self.status("GPT reply failed. Automatic retry stopped; check Codex login or usage.")
            raise

    async def post(self, message, body):
        if not self.connected or time.time() - message.sent_at >= REPLY_WINDOW:
            raise RuntimeError("Connection unavailable or QQ reply window expired")
        args = dict(content=body, msg_type=0, msg_id=message.message_id, msg_seq=1)
        if message.scope == "private":
            await self.client.api.post_c2c_message(openid=message.sender_id, **args)
        else:
            await self.client.api.post_group_message(group_openid=message.chat_id, **args)

    def update(self, message, state):
        event_key = self.ledger.key(message)
        item = self.entries.get(event_key)
        if item:
            item["state"] = state
            self.emit("message", (event_key, message, state))

    async def send_auto(self, message, body):
        async with self.dispatch_lock:
            if await self.check_human((message,)) is not ReplyStatus.UNANSWERED:
                raise RuntimeError("Reply cancelled before sending")
            if not self.ledger.claim(message):
                raise RuntimeError("This QQ message already has a delivery attempt")
            try:
                await self.post(message, attributed(body))
            except Exception:
                self.ledger.finish(message, "uncertain")
                self.update(message, "delivery uncertain; check QQ")
                raise
            self.ledger.finish(message, "sent")
            self.update(message, "GPT reply sent")

    async def manual_reply(self, message_id, body, gpt_written=False):
        item = self.entries.get(message_id)
        if not item or not body.strip():
            return
        if gpt_written:
            body = attributed(body.strip()[:1700])
        elif len(body.strip()) > 1800:
            self.status("Please shorten your reply to 1,800 characters before sending.")
            return
        message = item["message"]
        self.held.add(message.key)
        async with self.dispatch_lock:
            if not self.ledger.claim(message):
                self.status("Already attempted delivery; check QQ before another reply")
                return
            try:
                await self.post(message, body.strip())
            except Exception as exc:
                self.ledger.finish(message, "uncertain")
                self.update(message, "human delivery uncertain; check QQ")
                self.status("Manual reply not confirmed (" + type(exc).__name__ + ")")
                return
            self.ledger.finish(message, "human replied")
            self.workflow.human_replied(message.scope, message.chat_id, at=time.time(), sender_id=message.sender_id)
            for other in self.entries.values():
                if other["message"].key == message.key and other["state"] in ("waiting", "paused"):
                    self.update(other["message"], "human replied")
            self.held.discard(message.key)
            self.status("Your reply was sent through the bot; pending GPT reply cancelled")

    def cancel(self, message_id):
        item = self.entries.get(message_id)
        if item:
            msg = item["message"]
            self.workflow.human_replied(msg.scope, msg.chat_id, at=time.time(), sender_id=msg.sender_id)
            self.update(msg, "cancelled")

    async def ticker(self):
        while True:
            now = time.time()
            for key, pending in list(self.workflow.pending.items()):
                if now - max(m.sent_at for m in pending.messages) >= REPLY_WINDOW:
                    pending.cancelled = True
                    del self.workflow.pending[key]
                    for msg in pending.messages:
                        self.update(msg, "QQ reply window expired")
            try:
                await self.workflow.tick(now=now, check_human=self.check_human,
                                         generate=self.generate, send=self.send_auto)
            except Exception as exc:
                self.status("Reply withheld or failed (" + type(exc).__name__ + "); check the message status")
            await asyncio.sleep(0.25)


class QQClient(botpy.Client):
    def __init__(self, service):
        super().__init__(intents=botpy.Intents(public_messages=True), timeout=20,
                         bot_log=False, ext_handlers=False)
        # Prevent even lastResort logging from echoing an API response body.
        std_logging.getLogger("botpy").disabled = True
        self.service = service

    async def on_ready(self):
        self.loop.set_exception_handler(lambda loop, context: self.service.status(
            "QQ connection task needs attention; automatic delivery may be paused"))
        self.service.connected = True
        self.service.emit("connection", True)
        self.service.status("QQ connected. Auto replies remain paused until you enable them.")

    async def on_resumed(self):
        await self.on_ready()

    async def bot_connect(self, session):
        try:
            await super().bot_connect(session)
        finally:
            self.service.connected = False
            self.service.set_auto(False)
            self.service.emit("connection", False)

    async def on_c2c_message_create(self, message):
        await self.service.receive(message, "private")

    async def on_group_at_message_create(self, message):
        # The official event proves an explicit @bot mention. No all-group
        # handler, guild handler, or text-based mention matching is installed.
        await self.service.receive(message, "group")

    async def on_error(self, event_method, *args, **kwargs):
        self.service.status("QQ event could not be processed; no automatic reply was confirmed")
