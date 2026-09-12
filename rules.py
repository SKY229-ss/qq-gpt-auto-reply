"""Offline reference rules. No network, QQ login, history access, or settings writes.

All events/identities must be verified by the official transport before ingestion.
One process only; pending state and duplicate tracking are in memory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Awaitable, Callable

WAIT_SECONDS = 60
GPT_NOTE = "(Written by GPT)"


class ReplyStatus(Enum):
    UNANSWERED = "unanswered"
    REPLIED = "replied"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Message:
    scope: str                    # private | group
    chat_id: str                  # namespaced to the official bot/account
    sender_id: str
    message_id: str
    sent_at: float                # trusted timestamp, same clock as tick()
    content: str
    mentioned_ids: frozenset[str] = frozenset()
    actor: str = "person"         # verified person | owner | bot | unknown

    @property
    def key(self) -> tuple[str, str, str]:
        return self.scope, self.chat_id, self.sender_id


@dataclass
class Pending:
    messages: list[Message] = field(default_factory=list)
    cancelled: bool = False

    @property
    def first_at(self) -> float:
        return min(message.sent_at for message in self.messages)

    @property
    def due_at(self) -> float:
        return self.first_at + WAIT_SECONDS


def attributed(body: str) -> str:
    """Apply the disclosure after generation, so a prompt cannot remove it."""
    body = body.strip()
    if not body:
        raise ValueError("GPT produced an empty reply")
    if body.endswith(GPT_NOTE):
        return body
    return body + "\n\n" + GPT_NOTE


class Workflow:
    def __init__(self, *, owner_id: str, bot_id: str):
        if not owner_id or not bot_id:
            raise ValueError("Verified owner and bot identities are required")
        self.targets = frozenset((owner_id, bot_id))
        self.pending: dict[tuple[str, str, str], Pending] = {}
        self.seen: set[tuple[str, str, str]] = set()
        self._ticking = False

    def ingest(self, message: Message) -> bool:
        if message.scope not in ("private", "group"):
            return False
        if message.actor != "person" or message.sender_id in self.targets:
            return False
        if not all((message.chat_id, message.sender_id, message.message_id)):
            return False
        if not math.isfinite(message.sent_at):
            return False
        if message.scope == "group" and not (self.targets & message.mentioned_ids):
            return False
        # Filter group traffic before retaining any content or creating timers.
        identity = (message.scope, message.chat_id, message.message_id)
        if identity in self.seen:
            return False
        self.seen.add(identity)
        pending = self.pending.setdefault(message.key, Pending())
        pending.messages.append(message)
        return True

    def human_replied(
        self, scope: str, chat_id: str, *, at: float, sender_id: str | None = None
    ) -> None:
        """Call only with an authenticated observation of the human's reply.

        If no recipient can be resolved in a group, cancel all older pending
        requests in that group. This is a control signal, never a reply trigger.
        """
        if not math.isfinite(at):
            raise ValueError("A valid human reply timestamp is required")
        for key, pending in list(self.pending.items()):
            if key[:2] != (scope, chat_id):
                continue
            if sender_id is not None and key[2] != sender_id:
                continue
            if at < pending.first_at:
                continue
            pending.cancelled = True
            later_messages = [m for m in pending.messages if m.sent_at > at]
            if later_messages:
                self.pending[key] = Pending(later_messages)
            else:
                del self.pending[key]

    async def tick(
        self,
        *,
        now: float,
        check_human: Callable[[tuple[Message, ...]], Awaitable[ReplyStatus]],
        generate: Callable[[tuple[Message, ...]], Awaitable[str]],
        send: Callable[[Message, str], Awaitable[None]],
    ) -> list[str]:
        """Evaluate due requests; caller schedules ticks using a live timer.

        check_human must verify complete, current reply coverage. No observed
        event, an old export, or a disconnected monitor means UNKNOWN.
        send must recheck human status under the integration's dispatch lock,
        enforce QQ reply expiry/limits, and preserve the suffix on the wire.
        """
        if not math.isfinite(now):
            raise ValueError("A valid current timestamp is required")
        if self._ticking:
            return []
        self._ticking = True
        sent = []
        try:
            for key, pending in list(self.pending.items()):
                if pending.cancelled or now < pending.due_at:
                    continue

                async def ready() -> bool:
                    if pending.cancelled or self.pending.get(key) is not pending:
                        return False
                    try:
                        status = await check_human(tuple(pending.messages))
                    except Exception:
                        status = ReplyStatus.UNKNOWN
                    if pending.cancelled or self.pending.get(key) is not pending:
                        return False
                    if status is ReplyStatus.REPLIED:
                        pending.cancelled = True
                        del self.pending[key]
                    return status is ReplyStatus.UNANSWERED

                if not await ready():
                    continue
                batch = tuple(pending.messages)
                try:
                    text = attributed(await generate(batch))
                except Exception:
                    continue
                if not await ready():
                    continue
                # Claim the batch before I/O. An uncertain send is not retried:
                # an integration must reconcile the delivery result explicitly.
                pending.cancelled = True
                included_ids = {m.message_id for m in batch}
                followups = [m for m in pending.messages if m.message_id not in included_ids]
                if followups:
                    self.pending[key] = Pending(followups)
                else:
                    del self.pending[key]
                anchor = max(batch, key=lambda m: m.sent_at)
                await send(anchor, text)
                sent.append(anchor.message_id)
            return sent
        finally:
            self._ticking = False
