"""Behavioral regressions for ingestion, evidence coverage and AML boundaries."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.store import SQLiteMemoryStore, RequestConflictError


@pytest.fixture
def store():
    instance = SQLiteMemoryStore(":memory:")
    yield instance
    instance.close()


def add(store, request, text, *, user="alice", session="project", timestamp=None, role="user"):
    return store.add(request, user, session, [{"role": role, "content": text, "timestamp": timestamp}])


def source(item):
    header, body = item["content"].split("\n", 1)
    return json.loads(header[len("[source "):-1]), body


def test_single_record_and_unrelated_abstention(store):
    add(store, "one", "我的护照编号是 XK123456。")
    results = store.search("alice", "护照编号", 10)
    assert results and "XK123456" in results[0]["content"]
    assert store.search("alice", "火星天气气温", 10) == []
    assert store.search("bob", "护照编号", 10) == []


def test_raw_timestamp_order_repeat_and_idempotency(store):
    text = "我选择 PostgreSQL。"
    add(store, "one", text, timestamp=1700000000000)
    assert add(store, "one", text, timestamp=1700000000000) == "duplicate"
    add(store, "two", text, timestamp=1710000000000)
    with pytest.raises(RequestConflictError):
        add(store, "one", "different")
    rows = store._conn.execute("SELECT * FROM memories ORDER BY message_index").fetchall()
    assert [r["content"] for r in rows] == [text, text]
    assert [r["original_timestamp"] for r in rows] == [1700000000000, 1710000000000]
    assert [r["message_index"] for r in rows] == [0, 1]
    assert rows[0]["id"] != rows[1]["id"]


def test_document_example_and_roles(store):
    add(store, "e1", "毕业设计先用 MySQL，因为我更熟悉它。", session="first", timestamp=1700000000000)
    store.add("e234", "alice", "second", [
        {"role": "user", "content": "项目增加了复杂 JSON 查询需求。", "timestamp": 1710000000000},
        {"role": "assistant", "content": "可以考虑 PostgreSQL。", "timestamp": 1710000001000},
        {"role": "user", "content": "考虑到新增查询需求，就按这个建议改成 PostgreSQL。", "timestamp": 1710000002000},
    ])
    add(store, "foreign", "绝密数据库 MySQL", user="bob", session="second")
    result = store.search("alice", "我最初选了什么数据库，后来为什么修改？", 10)
    bodies = [source(item)[1] for item in result]
    assert any("MySQL" in body for body in bodies)
    assert any("改成 PostgreSQL" in body for body in bodies)
    assert all("绝密" not in body for body in bodies)
    assert all(source(item)[0]["role"] in {"user", "assistant"} for item in result)
    assert all(source(item)[0]["source_id"] == item["id"] for item in result)


def test_context_only_same_user_session(store):
    store.add("one", "alice", "same", [
        {"role": "assistant", "content": "建议使用 PostgreSQL。"},
        {"role": "user", "content": "按这个建议执行，项目预算编号 12345。"},
    ])
    add(store, "two", "外部上下文秘密", session="other")
    add(store, "three", "其他用户秘密", user="bob", session="same")
    results = store.search("alice", "12345", 5)
    assert len(results) == 2
    assert {source(item)[0]["evidence"] for item in results} == {"direct", "context"}
    assert all(source(item)[0]["session_id"] == "same" for item in results)


def test_time_bounds_apply_to_neighbors_and_unknown_times(store):
    store.add("one", "alice", "s", [
        {"role": "user", "content": "项目数据库 MySQL", "timestamp": 1704067200000},
        {"role": "user", "content": "项目数据库 PostgreSQL", "timestamp": 1735689600000},
        {"role": "assistant", "content": "项目数据库 unknown"},
    ])
    results = store.search("alice", "2025年前的项目数据库", 100)
    assert results
    assert all(source(item)[0]["timestamp_ms"] < 1735689600000 for item in results)


def test_initial_latest_top_k_and_budget(store):
    add(store, "one", "我选择数据库 MySQL", timestamp=1700000000000)
    add(store, "two", "我确认数据库改成 PostgreSQL", timestamp=1710000000000)
    assert "MySQL" in store.search("alice", "最初数据库", 1)[0]["content"]
    assert "PostgreSQL" in store.search("alice", "现在数据库", 1)[0]["content"]
    result = store.search("alice", "数据库", 1)
    assert len(result) <= 1
    store._retriever.budget = 1
    assert store.search("alice", "数据库", 100) == []


def test_rollback_on_index_failure(store, monkeypatch):
    def fail(*args):
        raise RuntimeError("index failure")
    monkeypatch.setattr(store, "_index", fail)
    with pytest.raises(RuntimeError):
        add(store, "one", "写入必须完整回滚")
    assert store.stats()["memories"] == 0
    assert store._conn.execute("SELECT COUNT(*) FROM request_ledger").fetchone()[0] == 0


def test_migration_persistence_and_delete(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE memories(id TEXT PRIMARY KEY,user_id TEXT,session_id TEXT,role TEXT,content TEXT,created_at TEXT)")
    conn.execute("INSERT INTO memories VALUES ('stable','alice','s','user','legacy MySQL','2024-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()
    instance = SQLiteMemoryStore(path)
    assert instance.search("alice", "MySQL", 10)[0]["id"] == "stable"
    assert not instance.delete_memory("bob", "stable")
    instance.close()
    instance = SQLiteMemoryStore(path)
    assert instance.delete_memory("alice", "stable")
    assert instance.search("alice", "MySQL", 10) == []
    instance.close()


def test_concurrent_retries_and_search(store):
    def work(i):
        add(store, "same", "memory database MySQL")
        return store.search("alice", "MySQL", 10)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(work, range(12)))
    assert store.stats()["memories"] == 1


def test_conflicting_records_are_retained_without_inferred_truth(store):
    add(store, "a", "我的数据库是 MySQL。", session="a", timestamp=1700000000000)
    add(store, "b", "我的数据库是 PostgreSQL。", session="b", timestamp=1710000000000)
    results = store.search("alice", "数据库", 10)
    assert any("MySQL" in source(item)[1] for item in results)
    assert any("PostgreSQL" in source(item)[1] for item in results)
    assert all("原因" not in source(item)[1] for item in results)


def test_fts_syntax_is_literal_and_no_external_content(store):
    add(store, "a", "database MySQL")
    for query in ['" OR *', 'NEAR(database) -sqlite', "数据库' OR 1=1 --"]:
        result = store.search("alice", query, 10)
        assert all(source(item)[1] == "database MySQL" for item in result)
