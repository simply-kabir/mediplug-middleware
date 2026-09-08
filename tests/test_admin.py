"""
Phase 9.3 — /admin/queue + /admin/cases/{id}/requeue tests.

Fully offline: importing `mediplug.gateway.main.app` is safe (db_pool is
built with open=False, so no live DB), `queue.get_redis` is monkeypatched
for the read route, and `db_pool` + `enqueue` are monkeypatched for the
requeue route. No lifespan is run — TestClient(app) is used without the
context-manager form on purpose.
"""

import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import RedisError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mediplug import queue as queue_mod
from mediplug.config import settings
from mediplug.gateway import main

TOKEN = settings.admin_token
client = TestClient(main.app)


# --- fakes -----------------------------------------------------------------


class _FakeRedis:
    async def xlen(self, key):
        return 5

    async def xpending(self, key, group):
        return {"pending": 1}

    async def xinfo_groups(self, key):
        return []

    async def xrange(self, key, start, end, count=None):
        return []


class _FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)
        self.executed: list[tuple] = []

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))

    async def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    async def commit(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return self._conn


# --- /admin/queue --------------------------------------------------------------


def test_admin_queue_returns_live_numbers(monkeypatch):
    monkeypatch.setattr(queue_mod, "get_redis", lambda: _FakeRedis())

    resp = client.get("/admin/queue", headers={"X-Admin-Token": TOKEN})

    assert resp.status_code == 200
    body = resp.json()
    assert body["stream_length"] == 5
    assert body["pending"] == 1
    assert body["groups"] == []
    assert body["dlq_recent"] == []


def test_admin_queue_503_when_redis_unavailable(monkeypatch):
    class _Broken:
        async def xlen(self, key):
            raise RedisError("connection refused")

    monkeypatch.setattr(queue_mod, "get_redis", lambda: _Broken())

    resp = client.get("/admin/queue", headers={"X-Admin-Token": TOKEN})
    assert resp.status_code == 503


def test_admin_routes_401_without_token():
    assert client.get("/admin/queue").status_code == 401
    assert client.get("/admin/queue", headers={"X-Admin-Token": "wrong"}).status_code == 401


# --- /admin/cases/{id}/requeue ----------------------------------------------


def test_requeue_enqueues_with_manual_retry_trigger(monkeypatch):
    cur = _FakeCursor([("action_required", "preauth")])
    monkeypatch.setattr(main, "db_pool", _FakePool(_FakeConn(cur)))

    captured = {}

    async def fake_enqueue(envelope):
        captured["envelope"] = envelope
        return "job-xyz"

    monkeypatch.setattr(main, "enqueue", fake_enqueue)

    case_id = str(uuid4())
    resp = client.post(f"/admin/cases/{case_id}/requeue", headers={"X-Admin-Token": TOKEN})

    assert resp.status_code == 202
    assert captured["envelope"].trigger == "manual_retry"
    assert captured["envelope"].case_id == case_id
    body = resp.json()
    assert body["job_id"] == "job-xyz"
    assert body["status"] == "action_required"
    # an admin-actor audit row was written
    assert any("case_events" in sql for sql, _ in cur.executed)


def test_requeue_404_for_unknown_case(monkeypatch):
    monkeypatch.setattr(main, "db_pool", _FakePool(_FakeConn(_FakeCursor([]))))
    monkeypatch.setattr(main, "enqueue", lambda *_: pytest.fail("should not enqueue"))

    resp = client.post(
        f"/admin/cases/{uuid4()}/requeue", headers={"X-Admin-Token": TOKEN}
    )
    assert resp.status_code == 404


def test_requeue_401_without_token():
    resp = client.post(f"/admin/cases/{uuid4()}/requeue")
    assert resp.status_code == 401
