"""Token accounting, pricing and batched usage recording."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ..billing import charge_usage
from ..models import ModelRoute, Organization, UsageEvent

if TYPE_CHECKING:
    from ..context import AppContext

log = logging.getLogger("controlplane.metering")


@dataclass
class Usage:
    """Token counts, with ``input_tokens`` meaning *uncached* prompt tokens."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0

    @property
    def prompt_total(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_5m_tokens + self.cache_write_1h_tokens

    @classmethod
    def from_openai(cls, usage: dict[str, Any] | None) -> Usage:
        if not usage:
            return cls()
        prompt = int(usage.get("prompt_tokens") or 0)
        details = usage.get("prompt_tokens_details") or {}
        cached = int(details.get("cached_tokens") or 0)
        return cls(
            input_tokens=max(prompt - cached, 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            cache_read_tokens=cached,
        )

    def merge_anthropic(self, usage: dict[str, Any] | None) -> None:
        """Fold an Anthropic usage block in (``message_start`` then ``message_delta``)."""
        if not usage:
            return
        if usage.get("input_tokens") is not None:
            self.input_tokens = max(self.input_tokens, int(usage["input_tokens"]))
        if usage.get("output_tokens") is not None:
            self.output_tokens = max(self.output_tokens, int(usage["output_tokens"]))
        if usage.get("cache_read_input_tokens") is not None:
            self.cache_read_tokens = max(self.cache_read_tokens, int(usage["cache_read_input_tokens"]))
        creation = usage.get("cache_creation") or {}
        if creation:
            self.cache_write_5m_tokens = max(self.cache_write_5m_tokens, int(creation.get("ephemeral_5m_input_tokens") or 0))
            self.cache_write_1h_tokens = max(self.cache_write_1h_tokens, int(creation.get("ephemeral_1h_input_tokens") or 0))
        elif usage.get("cache_creation_input_tokens") is not None:
            self.cache_write_5m_tokens = max(self.cache_write_5m_tokens, int(usage["cache_creation_input_tokens"]))


def cost_micros(route: ModelRoute | None, usage: Usage) -> int:
    if route is None:
        return 0
    multiplier = 1.0
    if route.long_context_tokens and usage.prompt_total > route.long_context_tokens:
        multiplier = route.long_context_multiplier or 1.0
    nanos = (
        usage.input_tokens * (route.input_nanos or 0)
        + usage.output_tokens * (route.output_nanos or 0)
        + usage.cache_read_tokens * (route.cache_read_nanos or 0)
        + usage.cache_write_5m_tokens * (route.cache_write_nanos or 0)
        + usage.cache_write_1h_tokens * (route.cache_write_1h_nanos or 0)
    ) * multiplier
    return math.ceil(nanos / 1000)


@dataclass
class _Pending:
    events: list[dict] = field(default_factory=list)
    by_org: dict[str, int] = field(default_factory=lambda: defaultdict(int))


class UsageRecorder:
    """Buffers usage events and applies them in batches.

    Large simulations make thousands of LLM calls per minute; writing a row and
    updating the balance per call would serialise them on the database. Events
    are flushed every ``usage_flush_interval_s``; balance checks subtract the
    not-yet-flushed amounts so enforcement stays accurate between flushes.
    """

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._pending = _Pending()
        # Events being written by a flush: not yet reflected in the balance cache.
        self._inflight = _Pending()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._balance_cache: dict[str, tuple[float, int]] = {}

    def record(self, event: dict) -> None:
        self._pending.events.append(event)
        self._pending.by_org[event["org_id"]] += int(event.get("cost_micros", 0))

    def pending_for(self, org_id: str) -> int:
        return self._pending.by_org.get(org_id, 0) + self._inflight.by_org.get(org_id, 0)

    async def available_micros(self, org_id: str) -> int:
        now = time.monotonic()
        cached = self._balance_cache.get(org_id)
        if cached is None or now - cached[0] > 2.0:
            async with self.ctx.db.sessionmaker() as db:
                balance = (await db.execute(select(Organization.balance_micros).where(Organization.id == org_id))).scalar_one_or_none()
            cached = (now, int(balance or 0))
            self._balance_cache[org_id] = cached
        return cached[1] - self.pending_for(org_id)

    async def flush(self) -> int:
        async with self._lock:
            pending, self._pending = self._pending, _Pending()
            if not pending.events:
                return 0
            self._inflight = pending
            try:
                async with self.ctx.db.sessionmaker() as db:
                    db.add_all([UsageEvent(**event) for event in pending.events])
                    for org_id, amount in pending.by_org.items():
                        await charge_usage(db, org_id, amount)
                    await db.commit()
            except Exception:
                log.exception("usage flush failed; re-queueing %d events", len(pending.events))
                self._inflight = _Pending()
                for event in pending.events:
                    self.record(event)
                return 0
            for org_id in pending.by_org:
                self._balance_cache.pop(org_id, None)
            self._inflight = _Pending()
            return len(pending.events)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.ctx.settings.usage_flush_interval_s)
            with contextlib.suppress(Exception):
                await self.flush()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self.flush()


class ConcurrencyLimiter:
    """Per-organisation cap on in-flight LLM requests (single replica)."""

    def __init__(self) -> None:
        self._sems: dict[tuple[str, int], asyncio.Semaphore] = {}

    def get(self, org_id: str, limit: int) -> asyncio.Semaphore:
        key = (org_id, limit)
        sem = self._sems.get(key)
        if sem is None:
            sem = self._sems[key] = asyncio.Semaphore(limit)
        return sem
