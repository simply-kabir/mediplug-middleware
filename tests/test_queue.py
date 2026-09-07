"""
Phase 9.1 — queue wrapper tests.

Fully offline: `queue.get_redis` is monkeypatched to an in-memory fake, so
nothing here touches a live Redis. What's under test is the stream-bounding
contract (enqueue carries maxlen/approximate), the `queue_stats` shape, and
that `close_redis` releases + resets the shared client.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mediplug import queue as queue_mod
from mediplug.schemas import JobEnvelope


class _FakeRedis:
    def __init__(self):
        self.xadd_calls: list[tuple] = []
        self.aclosed = False

    async def xadd(self, key, fields, **kwargs):
        self.xadd_calls.append((key, fields, kwargs))
        return "1-0"

    async def xlen(self, key):
        return 3

    async def xpending(self, key, group):
        return {"pending": 2}

    async def xinfo_groups(self, key):
        return [{"name": "mediplug-workers", "pending": 2, "lag": 0}]

    async def xrange(self, key, start, end, count=None):
        return [("1-0", {"payload": '{"case_id": "abc", "error": "boom"}'})]

    async def aclose(self):
        self.aclosed = True


@pytest.mark.asyncio
async def test_enqueue_passes_maxlen_and_approximate(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(queue_mod, "get_redis", lambda: fake)
    monkeypatch.setattr(queue_mod.settings, "stream_maxlen", 10_000)

    await queue_mod.enqueue(JobEnvelope(case_id="c1", stage="preauth"))

    key, fields, kwargs = fake.xadd_calls[0]
    assert key == queue_mod.settings.stream_key
    assert "payload" in fields
    assert kwargs["maxlen"] == 10_000
    assert kwargs["approximate"] is True


@pytest.mark.asyncio
async def test_queue_stats_shape(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(queue_mod, "get_redis", lambda: fake)

    stats = await queue_mod.queue_stats()

    assert set(stats) == {
        "stream_length",
        "dlq_length",
        "pending",
        "groups",
        "dlq_recent",
    }
    assert stats["stream_length"] == 3
    assert stats["pending"] == 2
    assert stats["groups"] == [{"name": "mediplug-workers", "pending": 2, "lag": 0}]
    # DLQ payloads come back JSON-parsed for the admin UI
    assert stats["dlq_recent"] == [{"case_id": "abc", "error": "boom"}]


@pytest.mark.asyncio
async def test_close_redis_resets_global(monkeypatch):
    fake = _FakeRedis()
    queue_mod._pool = fake

    await queue_mod.close_redis()

    assert fake.aclosed is True
    assert queue_mod._pool is None
    # idempotent — a second call with nothing to close is a no-op
    await queue_mod.close_redis()
