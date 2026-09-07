"""
Phase 9.2 — graceful shutdown + consumer resilience tests.

Fully offline: a fake Redis stands in for the stream, `process_case` /
`install_handlers` / `ensure_group` are monkeypatched. What's under test is
the shutdown wiring (event set, loops wind down promptly, reclaim task
awaited before any cancel), the self-reclaim guard (`_inflight`), and the
poison-pill path (DLQ + XACK, not a fatal exception).
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mediplug import shutdown as shutdown_mod
from mediplug.worker import consumer


@pytest.fixture(autouse=True)
def _reset_state():
    shutdown_mod.shutdown_event.clear()
    consumer._inflight.clear()
    yield
    shutdown_mod.shutdown_event.clear()
    consumer._inflight.clear()


class _FakeRedis:
    def __init__(self, messages=()):
        self._messages = list(messages)  # (msg_id, {"payload": ...})
        self.acked: list[str] = []
        self.xadd_calls: list[tuple] = []

    async def xreadgroup(self, group, consumer_name, streams, count=1, block=0):
        if self._messages:
            return [(consumer.settings.stream_key, [self._messages.pop(0)])]
        await asyncio.sleep(0)
        return []

    async def xack(self, key, group, msg_id):
        self.acked.append(msg_id)

    async def xadd(self, key, fields, **kwargs):
        self.xadd_calls.append((key, fields, kwargs))
        return "9-9"

    async def xautoclaim(self, key, group, name, min_idle_time, count):
        return ("0-0", [], [])


async def _noop(*a, **k):
    return None


# --- request_shutdown --------------------------------------------------------


def test_request_shutdown_sets_the_event():
    assert not shutdown_mod.shutdown_event.is_set()
    shutdown_mod.request_shutdown()
    assert shutdown_mod.shutdown_event.is_set()


# --- run(): finish the in-flight job, then exit ----------------------------


@pytest.mark.asyncio
async def test_run_finishes_inflight_message_then_exits(monkeypatch):
    processed = []

    async def fake_process(job):
        processed.append(job["case_id"])
        shutdown_mod.request_shutdown()  # shutdown lands mid-job

    fake = _FakeRedis(
        [("1-1", {"payload": json.dumps({"case_id": "c1", "attempt": 1, "job_id": "j1"})})]
    )
    monkeypatch.setattr(consumer, "process_case", fake_process)
    monkeypatch.setattr(consumer, "install_handlers", lambda: None)
    monkeypatch.setattr(consumer, "ensure_group", _noop)
    monkeypatch.setattr(consumer, "get_redis", lambda: fake)

    await asyncio.wait_for(consumer.run(), timeout=5)

    assert processed == ["c1"]          # the job ran to completion
    assert "1-1" in fake.acked          # and was ACKed before exit


@pytest.mark.asyncio
async def test_run_awaits_reclaim_task_before_cancel(monkeypatch):
    events = []

    async def fake_reclaim(r):
        await shutdown_mod.shutdown_event.wait()
        events.append("reclaim_clean_exit")

    async def fake_process(job):
        shutdown_mod.request_shutdown()

    monkeypatch.setattr(consumer, "reclaim_stalled", fake_reclaim)
    monkeypatch.setattr(consumer, "process_case", fake_process)
    monkeypatch.setattr(consumer, "install_handlers", lambda: None)
    monkeypatch.setattr(consumer, "ensure_group", _noop)
    fake = _FakeRedis([("1-1", {"payload": json.dumps({"case_id": "c1"})})])
    monkeypatch.setattr(consumer, "get_redis", lambda: fake)

    await asyncio.wait_for(consumer.run(), timeout=5)

    # If run() had cancelled the task instead of awaiting it, this stays empty.
    assert events == ["reclaim_clean_exit"]


# --- reclaim_stalled -------------------------------------------------------


@pytest.mark.asyncio
async def test_reclaim_stalled_returns_promptly_instead_of_sleeping(monkeypatch):
    calls = []

    async def fake_autoclaim(*a, **k):
        calls.append(1)
        shutdown_mod.request_shutdown()
        return ("0-0", [], [])

    fake = _FakeRedis()
    fake.xautoclaim = fake_autoclaim
    monkeypatch.setattr(consumer.settings, "reclaim_interval_s", 3600)  # would hang if slept

    await asyncio.wait_for(consumer.reclaim_stalled(fake), timeout=2)

    assert len(calls) == 1  # one pass, then _sleep_or_shutdown short-circuited


@pytest.mark.asyncio
async def test_reclaim_stalled_skips_messages_this_process_holds(monkeypatch):
    handled = []

    async def fake_handle(r, msg_id, raw):
        handled.append(msg_id)

    passes = []

    async def fake_autoclaim(*a, **k):
        if passes:
            return ("0-0", [], [])
        passes.append(1)
        return ("0-0", [("5-5", {"payload": "{}"}), ("6-6", {"payload": "{}"})], [])

    monkeypatch.setattr(consumer, "_handle", fake_handle)
    monkeypatch.setattr(consumer.settings, "reclaim_interval_s", 0)
    consumer._inflight.add("5-5")
    fake = _FakeRedis()
    fake.xautoclaim = fake_autoclaim

    task = asyncio.create_task(consumer.reclaim_stalled(fake))
    await asyncio.sleep(0.1)
    shutdown_mod.request_shutdown()
    await asyncio.wait_for(task, timeout=2)

    assert "5-5" not in handled  # self-reclaim blocked
    assert "6-6" in handled


@pytest.mark.asyncio
async def test_reclaim_stalled_logs_vanished_entries(monkeypatch):
    seen = {}

    async def fake_autoclaim(*a, **k):
        shutdown_mod.request_shutdown()
        return ("0-0", [], ["11-1", "12-1"])  # 3rd element: trimmed-away PEL ids

    def fake_error(event, **kw):
        if event == "reclaim_entries_vanished":
            seen.update(kw)

    fake = _FakeRedis()
    fake.xautoclaim = fake_autoclaim
    monkeypatch.setattr(consumer.log, "error", fake_error)

    await asyncio.wait_for(consumer.reclaim_stalled(fake), timeout=2)

    assert seen.get("count") == 2
    assert set(seen.get("ids", [])) == {"11-1", "12-1"}


# --- _handle: poison pill ------------------------------------------------------


@pytest.mark.asyncio
async def test_poison_pill_message_is_dlqd_not_fatal():
    fake = _FakeRedis()

    # Unparseable payload, a payload key missing, and a None raw all survive.
    await consumer._handle(fake, "7-7", {"payload": "not-json{{{"})
    await consumer._handle(fake, "8-8", {})
    await consumer._handle(fake, "9-9", None)

    assert {"7-7", "8-8", "9-9"} <= set(fake.acked)
    dlq_calls = [c for c in fake.xadd_calls if c[0] == consumer.settings.dlq_stream_key]
    assert len(dlq_calls) == 3
    # the DLQ is the only forensic record — it must NOT be trimmed
    for _key, _fields, kwargs in dlq_calls:
        assert "maxlen" not in kwargs


@pytest.mark.asyncio
async def test_inflight_is_cleared_after_handle():
    fake = _FakeRedis()
    await consumer._handle(fake, "7-7", {"payload": "not-json"})
    assert "7-7" not in consumer._inflight
