"""样品召回模块端到端测试。

覆盖:开启召回(幂等/冲突/源样品校验)、实时沿谱系边追踪源样品及全部后代
(合并汇流只计一次、召回后新增后代即时生效)、按召回号关闭(幂等)、
查询接口(召回单/列表/按样品查召回号)、核销领用逐项阻断与整单不扣量、
关闭后恢复核销、已建预留同样适用、旧库自动建表。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import recalls as rc
from app.db import connect, init_db
from app.main import app, get_conn


@pytest.fixture()
def client():
    mem = connect(":memory:")
    init_db(mem)

    def _override():
        yield mem

    app.dependency_overrides[get_conn] = _override
    with TestClient(app) as c:
        c.db = mem  # type: ignore[attr-defined]
        yield c
    app.dependency_overrides.clear()
    mem.close()


def _sample(client, op_no, sid, amount, unit="mL"):
    r = client.post("/api/v1/operations/sampling", json={
        "type": "sampling", "op_no": op_no, "sample_id": sid,
        "material": "血清", "amount": amount, "unit": unit,
    })
    assert r.status_code == 200, r.text


def _aliquot(client, op_no, parent, outputs):
    r = client.post("/api/v1/operations/aliquot", json={
        "type": "aliquot", "op_no": op_no, "parent_id": parent,
        "outputs": outputs,
    })
    assert r.status_code == 200, r.text


def _merge(client, op_no, inputs, output_id, material="混合血清"):
    r = client.post("/api/v1/operations/merge", json={
        "type": "merge", "op_no": op_no, "inputs": inputs,
        "output_id": output_id, "material": material,
    })
    assert r.status_code == 200, r.text


def _recall(client, no, source, reason="检出污染", **kw):
    body = {"recall_no": no, "source_sample_id": source,
            "reason": reason, **kw}
    return client.post("/api/v1/recalls", json=body)


def _reserve(client, no, items, **kw):
    body = {"reservation_no": no, "items": items, **kw}
    r = client.post("/api/v1/reservations", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _consume(client, no, reservation_no, items, **kw):
    body = {"consumption_no": no, "reservation_no": reservation_no,
            "items": items, **kw}
    return client.post("/api/v1/consumptions", json=body)


def _availability(client, sid):
    r = client.get(f"/api/v1/reservations?sample_id={sid}")
    assert r.status_code == 200, r.text
    return r.json()["samples"][0]


def _diamond(client):
    """S0 -> A1/A2 -> M1 的汇流谱系:M1 可经两条路径从 S0 到达。"""
    _sample(client, "OP-S0", "S0", 100)
    _aliquot(client, "OP-A", "S0", [
        {"sample_id": "A1", "amount": 40},
        {"sample_id": "A2", "amount": 30},
    ])
    _merge(client, "OP-M", [
        {"sample_id": "A1", "amount": 10},
        {"sample_id": "A2", "amount": 10},
    ], "M1")


# ---------------------------------------------------------------- 开启召回

def test_open_recall_traces_source_and_all_descendants(client):
    _diamond(client)
    r = _recall(client, "RC-1", "S0", operator="质检员", note="批次复检")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recall_no"] == "RC-1"
    assert body["source_sample_id"] == "S0"
    assert body["reason"] == "检出污染"
    assert body["status"] == "active"
    assert body["replayed"] is False
    assert body["created_at"] is not None
    assert body["closed_at"] is None
    # 源样品在前,后代按样品号排序;M1 经 A1/A2 两条路径到达但只计一次
    assert body["affected_samples"] == ["S0", "A1", "A2", "M1"]
    assert body["affected_count"] == 4


def test_recall_on_mid_chain_sample_excludes_upstream(client):
    _diamond(client)
    r = _recall(client, "RC-1", "A1")
    assert r.status_code == 200
    # 只覆盖 A1 及其下游;S0/A2 不受影响
    assert r.json()["affected_samples"] == ["A1", "M1"]


def test_affected_scope_is_realtime_for_new_descendants(client):
    _diamond(client)
    _recall(client, "RC-1", "S0")
    # 召回开启后又从 A1 分装出 B1:实时追踪应立即覆盖新后代
    _aliquot(client, "OP-B", "A1", [{"sample_id": "B1", "amount": 5}])
    body = client.get("/api/v1/recalls/RC-1").json()
    assert body["affected_samples"] == ["S0", "A1", "A2", "B1", "M1"]
    # 再从 M1 合并出新样品,同样即时进入受影响范围
    _merge(client, "OP-M2", [{"sample_id": "M1", "amount": 5}], "M2")
    body = client.get("/api/v1/recalls/RC-1").json()
    assert "M2" in body["affected_samples"]
    assert body["affected_count"] == 6


def test_open_recall_unknown_source_sample(client):
    r = _recall(client, "RC-1", "GHOST")
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "sample_not_found"
    assert c["sample_id"] == "GHOST"
    assert client.get("/api/v1/recalls/RC-1").status_code == 404


def test_open_recall_idempotent_replay_and_payload_conflict(client):
    _sample(client, "OP-S0", "S0", 100)
    body = {"recall_no": "RC-1", "source_sample_id": "S0", "reason": "检出污染"}

    r1 = client.post("/api/v1/recalls", json=body)
    assert r1.status_code == 200
    assert r1.json()["replayed"] is False

    r2 = client.post("/api/v1/recalls", json=body)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json() == r1.json() | {"replayed": True}

    # 同号不同内容 -> 409 recall_no_conflict
    r3 = client.post("/api/v1/recalls", json=body | {"reason": "另一原因"})
    assert r3.status_code == 409
    assert r3.json()["conflicts"][0]["code"] == "recall_no_conflict"

    # 只开了一次:审计事件 open + replay 各一条
    n = client.db.execute(
        "SELECT COUNT(*) AS c FROM recalls WHERE recall_no = 'RC-1'"
    ).fetchone()["c"]
    assert n == 1
    actions = [
        row["action"] for row in client.db.execute(
            "SELECT action FROM recall_events WHERE recall_no = 'RC-1'"
            " ORDER BY event_id"
        ).fetchall()
    ]
    assert actions == ["open", "replay"]


# ---------------------------------------------------------------- 关闭召回

def test_close_recall_and_replay(client):
    _sample(client, "OP-S0", "S0", 100)
    _recall(client, "RC-1", "S0")

    r = client.post("/api/v1/recalls/RC-1/close",
                    json={"operator": "质量经理", "note": "复检合格"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "closed"
    assert body["replayed"] is False
    assert body["closed_at"] is not None

    # 重复关闭幂等:返回首次关闭结果
    r2 = client.post("/api/v1/recalls/RC-1/close")
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["closed_at"] == body["closed_at"]

    # 已关闭召回号不能再次开启
    r3 = _recall(client, "RC-1", "S0")
    assert r3.status_code == 409
    assert r3.json()["conflicts"][0]["code"] == "recall_closed"


def test_close_unknown_recall_404(client):
    r = client.post("/api/v1/recalls/NOPE/close")
    assert r.status_code == 404
    assert r.json()["conflicts"][0]["code"] == "recall_not_found"


# ---------------------------------------------------------------- 查询

def test_get_and_list_recalls(client):
    _diamond(client)
    _sample(client, "OP-X", "X1", 50)
    _recall(client, "RC-1", "S0")
    _recall(client, "RC-2", "X1", reason="标签错误")

    r = client.get("/api/v1/recalls/RC-1")
    assert r.status_code == 200
    assert r.json()["affected_samples"] == ["S0", "A1", "A2", "M1"]
    assert client.get("/api/v1/recalls/NOPE").status_code == 404

    all_recalls = client.get("/api/v1/recalls").json()["recalls"]
    assert [x["recall_no"] for x in all_recalls] == ["RC-1", "RC-2"]
    by_no = {x["recall_no"]: x for x in all_recalls}
    assert by_no["RC-2"]["affected_samples"] == ["X1"]

    client.post("/api/v1/recalls/RC-2/close")
    active = client.get("/api/v1/recalls?status=active").json()["recalls"]
    assert [x["recall_no"] for x in active] == ["RC-1"]
    closed = client.get("/api/v1/recalls?status=closed").json()["recalls"]
    assert [x["recall_no"] for x in closed] == ["RC-2"]


def test_sample_recalls_lookup(client):
    _diamond(client)
    _sample(client, "OP-X", "X1", 50)
    _recall(client, "RC-1", "S0")
    _recall(client, "RC-2", "A1", reason="二次召回")

    # M1 同时处于 RC-1(S0 后代)与 RC-2(A1 后代)范围,两个召回号都返回
    r = client.get("/api/v1/samples/M1/recalls")
    assert r.status_code == 200
    body = r.json()
    assert body["recalled"] is True
    assert [x["recall_no"] for x in body["recalls"]] == ["RC-1", "RC-2"]
    assert body["recalls"][0]["source_sample_id"] == "S0"
    assert body["recalls"][1]["source_sample_id"] == "A1"

    # 未受影响样品
    body = client.get("/api/v1/samples/X1/recalls").json()
    assert body["recalled"] is False
    assert body["recalls"] == []

    # 关闭后不再计入
    client.post("/api/v1/recalls/RC-2/close")
    body = client.get("/api/v1/samples/M1/recalls").json()
    assert [x["recall_no"] for x in body["recalls"]] == ["RC-1"]

    # 样品不存在 -> 404
    assert client.get("/api/v1/samples/GHOST/recalls").status_code == 404


# ---------------------------------------------------------------- 核销阻断

def test_consumption_blocked_by_active_recall_and_restored_after_close(client):
    _diamond(client)
    _reserve(client, "RSV-1", [{"sample_id": "M1", "amount": 10}])
    _recall(client, "RC-1", "S0")

    # M1 是召回源 S0 的后代:核销被逐项拦截,整单不扣量
    r = _consume(client, "CV-1", "RSV-1", [{"sample_id": "M1", "amount": 5}])
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "sample_recalled"
    assert c["sample_id"] == "M1"
    assert "RC-1" in c["message"]
    assert _availability(client, "M1")["remaining"] == 20.0
    assert client.get("/api/v1/consumptions/CV-1").status_code == 404

    # 关闭召回后恢复核销
    client.post("/api/v1/recalls/RC-1/close")
    r = _consume(client, "CV-1", "RSV-1", [{"sample_id": "M1", "amount": 5}])
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["remaining"] == 15.0


def test_consumption_blocked_for_recall_source_sample_itself(client):
    _sample(client, "OP-S0", "S0", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S0", "amount": 30}])
    _recall(client, "RC-1", "S0")
    r = _consume(client, "CV-1", "RSV-1", [{"sample_id": "S0", "amount": 10}])
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "sample_recalled"
    assert _availability(client, "S0")["remaining"] == 100.0


def test_recall_blocks_reservation_created_before_recall(client):
    """已建预留同样适用:预留先于召回创建,召回开启后其核销仍被阻断。"""
    _diamond(client)
    _reserve(client, "RSV-OLD", [{"sample_id": "A1", "amount": 10}])
    _recall(client, "RC-1", "S0")  # 召回在预留之后开启

    r = _consume(client, "CV-1", "RSV-OLD", [{"sample_id": "A1", "amount": 4}])
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "sample_recalled"
    assert _availability(client, "A1")["remaining"] == 30.0


def test_recall_conflict_rolls_back_whole_consumption(client):
    """多明细核销单中任一样品命中召回,整单不扣量(其他样品也不动)。"""
    _diamond(client)
    _sample(client, "OP-X", "X1", 50)
    _reserve(client, "RSV-1", [
        {"sample_id": "M1", "amount": 10},
        {"sample_id": "X1", "amount": 10},
    ])
    _recall(client, "RC-1", "S0")  # 只影响 S0 谱系,不影响 X1

    r = _consume(client, "CV-1", "RSV-1", [
        {"sample_id": "M1", "amount": 5},   # 命中召回
        {"sample_id": "X1", "amount": 5},   # 本身合法
    ])
    assert r.status_code == 409
    conflicts = r.json()["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["code"] == "sample_recalled"
    assert conflicts[0]["sample_id"] == "M1"
    # 整单回滚:X1 也未扣量,核销单不存在
    assert _availability(client, "X1")["remaining"] == 50.0
    assert _availability(client, "M1")["remaining"] == 20.0
    assert client.get("/api/v1/consumptions/CV-1").status_code == 404

    # 仅含未受影响样品的核销单在召回期间仍可正常核销
    r = _consume(client, "CV-2", "RSV-1", [{"sample_id": "X1", "amount": 5}])
    assert r.status_code == 200, r.text
    assert _availability(client, "X1")["remaining"] == 45.0


def test_consumption_replay_not_blocked_by_later_recall(client):
    """核销号重放只回放首次结果,不被事后开启的召回拦截。"""
    _diamond(client)
    _reserve(client, "RSV-1", [{"sample_id": "M1", "amount": 10}])
    body = {"consumption_no": "CV-1", "reservation_no": "RSV-1",
            "items": [{"sample_id": "M1", "amount": 5}]}
    first = client.post("/api/v1/consumptions", json=body)
    assert first.status_code == 200

    _recall(client, "RC-1", "S0")
    replay = client.post("/api/v1/consumptions", json=body)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["items"] == first.json()["items"]
    # 未重复扣量
    assert _availability(client, "M1")["remaining"] == 15.0


def test_multiple_active_recalls_listed_in_conflict(client):
    _diamond(client)
    _reserve(client, "RSV-1", [{"sample_id": "M1", "amount": 10}])
    _recall(client, "RC-1", "S0")
    _recall(client, "RC-2", "A1", reason="二次召回")

    r = _consume(client, "CV-1", "RSV-1", [{"sample_id": "M1", "amount": 5}])
    assert r.status_code == 409
    msg = r.json()["conflicts"][0]["message"]
    assert "RC-1" in msg and "RC-2" in msg


def test_closed_recall_does_not_block_and_scope_not_listed(client):
    _diamond(client)
    _recall(client, "RC-1", "S0")
    client.post("/api/v1/recalls/RC-1/close")
    # 已关闭召回不再影响样品查询
    body = client.get("/api/v1/samples/M1/recalls").json()
    assert body["recalled"] is False


# ---------------------------------------------------------------- 账本一致性

def test_balance_ledger_consistent_after_recall_cycle(client):
    _diamond(client)
    _reserve(client, "RSV-1", [{"sample_id": "M1", "amount": 10}])
    _recall(client, "RC-1", "S0")
    client.post("/api/v1/recalls/RC-1/close")
    _consume(client, "CV-1", "RSV-1", [{"sample_id": "M1", "amount": 5}])
    bal = client.get("/api/v1/checks/balances").json()
    assert bal["consistent"] is True


# ---------------------------------------------------------------- 旧库建表

def test_tables_created_on_old_database(tmp_path):
    """旧版本数据库(无召回表)启动初始化后自动补齐,迁移幂等。"""
    db_path = str(tmp_path / "old.db")
    conn = connect(db_path)
    # 只建基础表,模拟旧库
    conn.executescript(
        "CREATE TABLE samples (sample_id TEXT PRIMARY KEY, material TEXT NOT NULL,"
        " unit TEXT NOT NULL, initial_amount REAL NOT NULL, remaining REAL NOT NULL,"
        " version INTEGER NOT NULL DEFAULT 0, location TEXT, is_root INTEGER NOT NULL"
        " DEFAULT 0, created_at TEXT);"
        "CREATE TABLE lineage_edges (edge_id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " parent_id TEXT, child_id TEXT NOT NULL, op_id INTEGER NOT NULL,"
        " amount REAL NOT NULL, unit TEXT NOT NULL, ref_no TEXT NOT NULL);"
        "CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT);"
    )
    conn.execute(
        "INSERT INTO samples (sample_id, material, unit, initial_amount,"
        " remaining, is_root) VALUES ('S1', 'M', 'mL', 100, 100, 1)"
    )
    init_db(conn)
    init_db(conn)  # 幂等:再跑一次不报错
    names = {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"recalls", "recall_events"} <= names

    # 补齐后功能正常
    from app.schemas import RecallRequest

    result = rc.create_recall(conn, RecallRequest(
        recall_no="RC-1", source_sample_id="S1", reason="检出污染",
    ))
    assert result.status == "active"
    assert result.affected_samples == ["S1"]
    closed = rc.close_recall(conn, "RC-1")
    assert closed.status == "closed"
    conn.close()
