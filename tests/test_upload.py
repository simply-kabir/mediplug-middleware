"""
Tests for document upload endpoints:
- POST /api/v1/cases/{case_id}/upload-document (multipart/form-data)
- GET /api/v1/cases/{case_id}/documents
"""

import io
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mediplug.gateway import main

client = TestClient(main.app)


class _FakeCursor:
    def __init__(self, fetchone_rows=None, fetchall_rows=None):
        self._fetchone_rows = list(fetchone_rows or [])
        self._fetchall_rows = list(fetchall_rows or [])
        self.executed: list[tuple] = []

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))

    async def fetchone(self):
        return self._fetchone_rows.pop(0) if self._fetchone_rows else None

    async def fetchall(self):
        return self._fetchall_rows

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


def test_upload_case_file_success(monkeypatch, tmp_path):
    case_id = str(uuid4())
    fake_cursor = _FakeCursor(fetchone_rows=[("action_required", "preauth", "MP-TEST123")])
    fake_conn = _FakeConn(fake_cursor)
    fake_pool = _FakePool(fake_conn)

    monkeypatch.setattr(main, "db_pool", fake_pool)
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path)

    enqueued = []

    async def _fake_enqueue(env):
        enqueued.append(env)
        return "job_upload_123"

    monkeypatch.setattr(main, "enqueue", _fake_enqueue)

    file_content = b"%PDF-1.4 test ultrasound content"
    files = {"file": ("usg_report.pdf", io.BytesIO(file_content), "application/pdf")}
    data = {"document_type": "usg_abdomen", "uploaded_by": "test_user"}

    resp = client.post(f"/api/v1/cases/{case_id}/upload-document", data=data, files=files)

    assert resp.status_code == 200
    res = resp.json()
    assert res["case_id"] == case_id
    assert res["tracking_ref"] == "MP-TEST123"
    assert res["status"] == "queued"
    assert res["document"]["document_type"] == "usg_abdomen"
    assert res["document"]["file_name"] == "usg_report.pdf"
    assert len(enqueued) == 1
    assert enqueued[0].trigger == "docs_updated"

    # Verify file was written to disk
    saved_file = tmp_path / case_id / "usg_report.pdf"
    assert saved_file.exists()
    assert saved_file.read_bytes() == file_content


def test_upload_case_file_missing_type(monkeypatch, tmp_path):
    case_id = str(uuid4())
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path)

    files = {"file": ("test.pdf", io.BytesIO(b"content"), "application/pdf")}
    data = {"document_type": "   "}

    resp = client.post(f"/api/v1/cases/{case_id}/upload-document", data=data, files=files)
    assert resp.status_code == 400


def test_list_case_documents(monkeypatch):
    case_id = str(uuid4())
    doc_id = str(uuid4())
    fake_cursor = _FakeCursor(
        fetchall_rows=[(doc_id, "usg_abdomen", "http://localhost:8000/uploads/test.pdf", "test.pdf", None)]
    )
    fake_conn = _FakeConn(fake_cursor)
    fake_pool = _FakePool(fake_conn)
    monkeypatch.setattr(main, "db_pool", fake_pool)

    resp = client.get(f"/api/v1/cases/{case_id}/documents")
    assert resp.status_code == 200
    docs = resp.json()
    assert len(docs) == 1
    assert docs[0]["document_type"] == "usg_abdomen"
    assert docs[0]["file_name"] == "test.pdf"
