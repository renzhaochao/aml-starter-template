"""AML interface tests including positive retrieval and authentication."""

from __future__ import annotations

import os
import tempfile

os.environ["MEMORY_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ.setdefault("AML_AUTH_MODE", "none")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.store import SQLiteMemoryStore  # noqa: E402


@pytest.fixture()
def client():
    store = SQLiteMemoryStore(":memory:")
    app.dependency_overrides.clear()
    # Routes call get_store directly; patch it for independent test databases.
    from unittest.mock import patch
    with patch("app.main.get_store", return_value=store):
        with TestClient(app) as c:
            yield c
    store.close()


def _add(client, request_id, user_id, contents):
    return client.post(
        "/aml/add",
        json={
            "request_id": request_id,
            "messages": [{"role": "user", "content": c} for c in contents],
            "user_id": user_id,
            "session_id": "s-1",
        },
    )


def test_health(client):
    assert client.get("/health").status_code == 200


def test_add_echoes_contract_fields(client):
    """硬要求 1：契约字段回显（平台逐字节比对）。"""
    r = _add(client, "req-1", "u1", ["hello world"])
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["request_id"] == "req-1"
    assert body["user_id"] == "u1"
    assert body["session_id"] == "s-1"


def test_user_isolation(client):
    """硬要求 2：user_id 隔离（隐私红线）。"""
    _add(client, "req-2", "u-secret", ["My passport number is XK123456."])
    _add(client, "req-3", "u-other", ["I like pizza very much."])
    r = client.post(
        "/aml/search", json={"query": "passport number", "user_id": "u-other", "top_k": 10}
    )
    assert r.status_code == 200
    assert all("XK123456" not in d["content"] for d in r.json()["data"])

    r = client.post("/aml/search", json={"query": "passport", "user_id": "u-secret", "top_k": 10})
    assert any("XK123456" in d["content"] for d in r.json()["data"])


def test_idempotent_retry(client):
    """硬要求 3：request_id 幂等（平台超时重试不产生重复）。"""
    before = client.get("/health").json()["memories"]
    payload = {
        "request_id": "req-dup",
        "messages": [{"role": "user", "content": "I moved to Porto."}],
        "user_id": "u1",
        "session_id": "s-1",
    }
    assert client.post("/aml/add", json=payload).status_code == 200
    assert client.post("/aml/add", json=payload).status_code == 200
    health = client.get("/health").json()
    assert health["memories"] == before + 1  # 只落库一条

    # 同一 request_id、不同负载 → 409
    conflict = dict(payload, messages=[{"role": "user", "content": "different"}])
    assert client.post("/aml/add", json=conflict).status_code == 409


def test_search_response_structure(client):
    """硬要求 4：检索返回结构 {"data": [{"id","content","score","created_at"}]}，空数组=弃权。"""
    _add(client, "req-4", "u1", ["some memory"])
    r = client.post("/aml/search", json={"query": "some memory", "user_id": "u1", "top_k": 10})
    assert r.status_code == 200
    data = r.json()["data"]
    assert isinstance(data, list)
    assert data
    assert len(data) <= 10
    assert [d["score"] for d in data] == sorted([d["score"] for d in data], reverse=True)
    for item in data:
        assert set(("id", "content", "score", "created_at")) <= set(item)


@pytest.mark.parametrize("headers", [
    {"Authorization": "Bearer test-key"},
    {"Authorization": "Token test-key"},
    {"X-Api-Key": "test-key"},
])
def test_auth_and_health(client, headers, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr("app.main.settings", SimpleNamespace(api_key="test-key", auth_mode="bearer"))
    assert client.get("/health").status_code == 200
    payload = {"query": "memory", "user_id": "u1", "top_k": 10}
    assert client.post("/aml/search", json=payload).status_code == 401
    assert client.post("/aml/search", json=payload, headers=headers).status_code == 200
    assert client.post("/aml/search", json=payload, headers={"X-Api-Key": "wrong"}).status_code == 401


@pytest.mark.parametrize("top_k", [0, -1, 101])
def test_invalid_top_k(client, top_k):
    assert client.post("/aml/search", json={"query": "memory", "user_id": "u", "top_k": top_k}).status_code == 422
