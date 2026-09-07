"""
Phase 8 — adjudication trigger tests.

Fully offline: the DB seams in `adjudication` (`_connect`, `_apply`,
`_submitted_cases`) are monkeypatched, and a fake httpx client is injected
into `poll_once`. What's under test is the glue — outcome→status mapping,
the `submitted`-only guard, and one poll pass — not a live payer or DB.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mediplug.worker import adjudication


class _FakeCursor:
    def __init__(self, status_row):
        self._status_row = status_row
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._status_row


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True


# --- apply_adjudication: outcome -> status mapping -----------------------


def test_apply_adjudication_approved_maps_to_payer_approved(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        adjudication,
        "_apply",
        lambda cid, status, detail: seen.update(cid=cid, status=status, detail=detail)
        or True,
    )

    assert adjudication.apply_adjudication("case-1", "approved") is True
    assert seen["status"] == "payer_approved"
    assert seen["detail"]["outcome"] == "approved"


def test_apply_adjudication_rejected_maps_to_payer_rejected(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        adjudication,
        "_apply",
        lambda cid, status, detail: seen.update(status=status, detail=detail) or True,
    )

    adjudication.apply_adjudication("case-1", "rejected", detail={"source": "mock-poller"})
    assert seen["status"] == "payer_rejected"
    assert seen["detail"] == {"outcome": "rejected", "source": "mock-poller"}


def test_apply_adjudication_rejects_unknown_outcome(monkeypatch):
    monkeypatch.setattr(adjudication, "_apply", lambda *a, **k: pytest.fail("_apply called"))
    with pytest.raises(ValueError, match="unknown adjudication outcome"):
        adjudication.apply_adjudication("case-1", "garbage")


# --- _apply: submitted-only guard -------------------------------------------


def test_apply_noops_when_case_not_submitted(monkeypatch):
    cur = _FakeCursor(("payer_approved",))
    monkeypatch.setattr(adjudication, "_connect", lambda: _FakeConn(cur))

    result = adjudication._apply("case-1", "payer_approved", {"outcome": "approved"})

    assert result is False
    # only the status SELECT ran — no UPDATE, no INSERT
    assert len(cur.executed) == 1
    assert "SELECT status" in cur.executed[0][0]


def test_apply_transitions_when_case_is_submitted(monkeypatch):
    cur = _FakeCursor(("submitted",))
    conn = _FakeConn(cur)
    monkeypatch.setattr(adjudication, "_connect", lambda: conn)

    result = adjudication._apply("case-1", "payer_approved", {"outcome": "approved"})

    assert result is True
    assert conn.committed is True
    statements = " ".join(sql for sql, _ in cur.executed)
    assert "UPDATE cases SET status" in statements
    assert "INSERT INTO case_events" in statements
    # actor is 'payer' and the detail jsonb is passed
    insert = next(p for sql, p in cur.executed if "INSERT INTO case_events" in sql)
    assert insert[3].obj == {"outcome": "approved"}


# --- poll_once -----------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    async def get(self, url):
        self.calls.append(url)
        return _FakeResponse(self._payload)


@pytest.mark.asyncio
async def test_poll_once_applies_only_resolved_outcomes(monkeypatch):
    monkeypatch.setattr(adjudication.settings, "dispatch_mode", "mock")
    monkeypatch.setattr(
        adjudication, "_submitted_cases", lambda: [("case1", "c1"), ("case2", "c2")]
    )
    applied = []
    monkeypatch.setattr(
        adjudication,
        "apply_adjudication",
        lambda cid, outcome, **k: applied.append((cid, outcome)) or True,
    )

    client = _FakeClient({"c1": {"outcome": "approved"}, "c2": {"outcome": "pending"}})
    n = await adjudication.poll_once(client=client)

    assert n == 1
    assert applied == [("case1", "approved")]
    assert client.calls == ["http://localhost:8081/received"]


@pytest.mark.asyncio
async def test_poll_once_is_a_noop_when_dispatch_mode_is_not_mock(monkeypatch):
    monkeypatch.setattr(adjudication.settings, "dispatch_mode", "nhcx")
    monkeypatch.setattr(
        adjudication, "_submitted_cases", lambda: pytest.fail("should not query")
    )

    assert await adjudication.poll_once() == 0
