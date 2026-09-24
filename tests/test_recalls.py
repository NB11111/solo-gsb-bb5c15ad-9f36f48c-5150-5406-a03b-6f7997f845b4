"""样品召回模块端到端测试。

覆盖:开启/关闭召回与幂等、活跃召回沿谱系边实时追踪源样品及全部后代
(分装/合并/合流去重与实时性)、受影响样品查询、核销领用逐项召回拦截与整单回滚
(命中返回逐项冲突、整单不扣量)、关闭召回后恢复核销(已建预留同样适用)、
多召回叠加、旧库自动建表。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

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


def _sampling(client, op_no, sid, amount=100, unit="mL"):
    r = client.post("/api/v1/operations/sampling", json={
        "type": "sampling", "op_no": op_no, "sample_id": sid,
        "material": "血清", "amount": amount, "unit": unit,
    })
    assert r.status_code == 200, r.text


def _aliquot(client, op_no, parent, outputs):
    r = client.post("/api/v1/operations/aliquot", json={
        "type": "aliquot", "op_no": op_no, "parent_id": parent,
        "outputs": [{"sample_id": s, "amount": a} for s, a in outputs],
    })
    assert r.status_code == 200, r.text


def _merge(client, op_no, inputs, output, material=None):
    body = {
        "type": "merge", "op_no": op_no,
        "inputs": [{"sample_id": s, "amount": a} for s, a in inputs],
        "output_id": output,
    }
    if material:
        body["material"] = material
    r = client.post("/api/v1/operations/merge", json=body)
    assert r.status_code == 200, r.text


def _open_recall(client, no, source, reason="污染", **kw):
    return client.post("/api/v1/recalls", json={
        "recall_no": no, "source_sample_id": source,
        "reason": reason, **kw,
    })


def _reserve(client, no, items, **kw):
    r = client.post("/api/v1/reservations",
                    json={"reservation_no": no, "items": items, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def _consume(client, no, reservation_no, items, **kw):
    return client.post("/api/v1/consumptions", json={
        "consumption_no": no, "reservation_no": reservation_no,
        "items": items, **kw,
    })


def _lineage(client):
    """S0 -> A1/A2;A1 -> B1;A2 -> B1(合流);B1 -> C1"""
    _sampling(client, "OP-1", "S0", 100)
    _aliquot(client, "OP-2", "S0", [("A1", 40), ("A2", 30)])
    _aliquot(client, "OP-3", "A1", [("B1", 20)])
    # 把 A2 也并入已存在的 B1(合流)
    _merge(client, "OP-4", [("A2", 10)], "B1")
    _aliquot(client, "OP-5", "B1", [("C1", 5)])


# ---------------------------------------------------------------- 开启/查询

def test_open_recall_requires_existing_source(client):
    r = _open_recall(client, "RC-1", "GHOST")
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "sample_not_found"
    assert client.get("/api/v1/recalls/RC-1").status_code == 404


def test_open_recall_tracks_source_and_all_descendants(client):
    _lineage(client)
    r = _open_recall(client, "RC-1", "S0", reason="源头污染")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "active"
    assert body["replayed"] is False
    assert body["closed_at"] is None
    affected = {x["sample_id"]: x["depth"] for x in body["affected_samples"]}
    # 源样品自身 + 全部后代;B1 经 A1/A2 两条路径合流只计一次
    assert affected == {"S0": 0, "A1": 1, "A2": 1, "B1": 2, "C1": 3}
    assert body["affected_count"] == 5
    # 按跳数、样品号升序
    assert [x["sample_id"] for x in body["affected_samples"]] == [
        "S0", "A1", "A2", "B1", "C1",
    ]


def test_recall_on_descendant_only_tracks_its_subtree(client):
    _lineage(client)
    r = _open_recall(client, "RC-1", "A2", reason="A2 污染")
    affected = {x["sample_id"] for x in r.json()["affected_samples"]}
    # A2 的后代:经合流到达 B1、C1;不追溯上游 S0/A1
    assert affected == {"A2", "B1", "C1"}


def test_merge_diamond_paths_counted_once(client):
    """菱形合并:S0->A1/A2,A1->M,A2->M(合流),M->D1;M 只计一次。"""
    _sampling(client, "OP-1", "S0", 100)
    _aliquot(client, "OP-2", "S0", [("A1", 30), ("A2", 30)])
    _aliquot(client, "OP-3", "A1", [("M", 10)])
    _merge(client, "OP-4", [("A2", 10)], "M")
    _aliquot(client, "OP-5", "M", [("D1", 5)])
    body = _open_recall(client, "RC-1", "S0").json()
    ids = [x["sample_id"] for x in body["affected_samples"]]
    assert sorted(ids) == sorted(["S0", "A1", "A2", "M", "D1"])
    assert len(ids) == len(set(ids))  # 无重复


def test_affected_query_returns_recall_nos(client):
    _lineage(client)
    _open_recall(client, "RC-1", "S0", reason="污染")
    r = client.get("/api/v1/recalls/affected-samples")
    assert r.status_code == 200
    samples = {x["sample_id"]: x for x in r.json()["samples"]}
    assert set(samples) == {"S0", "A1", "A2", "B1", "C1"}
    assert samples["B1"]["recall_nos"] == ["RC-1"]
    hit = samples["B1"]["recalls"][0]
    assert hit["recall_no"] == "RC-1"
    assert hit["source_sample_id"] == "S0"
    assert hit["reason"] == "污染"
    assert hit["depth"] == 2


def test_affected_query_can_filter_by_sample(client):
    _lineage(client)
    _open_recall(client, "RC-1", "S0")
    r = client.get("/api/v1/recalls/affected-samples?sample_id=B1&sample_id=C1")
    ids = [x["sample_id"] for x in r.json()["samples"]]
    assert ids == ["B1", "C1"]
    # 未受影响的样品不出现在结果中
    _sampling(client, "OP-9", "FREE", 10)
    r = client.get("/api/v1/recalls/affected-samples?sample_id=FREE")
    assert r.json()["samples"] == []
    # 不存在样品 -> 409 sample_not_found
    r = client.get("/api/v1/recalls/affected-samples?sample_id=NOPE")
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "sample_not_found"


def test_sample_recalls_endpoint(client):
    _lineage(client)
    _open_recall(client, "RC-1", "S0", reason="污染")
    r = client.get("/api/v1/samples/C1/recalls")
    assert r.status_code == 200
    body = r.json()
    assert body["sample_id"] == "C1"
    assert body["recalled"] is True
    assert body["recalls"][0]["recall_no"] == "RC-1"
    assert body["recalls"][0]["depth"] == 3

    _sampling(client, "OP-9", "FREE", 10)
    r = client.get("/api/v1/samples/FREE/recalls")
    assert r.json()["recalled"] is False
    assert r.json()["recalls"] == []

    assert client.get("/api/v1/samples/NOPE/recalls").status_code == 409


# ---------------------------------------------------------------- 实时追踪

def test_descendants_born_after_recall_are_tracked_live(client):
    _sampling(client, "OP-1", "S0", 100)
    body = _open_recall(client, "RC-1", "S0").json()
    assert [x["sample_id"] for x in body["affected_samples"]] == ["S0"]

    # 召回开启后新分装的后代实时纳入
    _aliquot(client, "OP-2", "S0", [("A1", 10)])
    body = client.get("/api/v1/recalls/RC-1").json()
    assert {x["sample_id"] for x in body["affected_samples"]} == {"S0", "A1"}

    # 受影响样品查询同样实时
    r = client.get("/api/v1/recalls/affected-samples?sample_id=A1")
    assert r.json()["samples"][0]["recall_nos"] == ["RC-1"]


# ---------------------------------------------------------------- 幂等 / 关闭

def test_recall_idempotent_replay_and_payload_conflict(client):
    _sampling(client, "OP-1", "S0", 100)
    body = {"recall_no": "RC-X", "source_sample_id": "S0",
            "reason": "污染", "operator": "李召回"}
    r1 = client.post("/api/v1/recalls", json=body)
    assert r1.status_code == 200
    assert r1.json()["replayed"] is False

    r2 = client.post("/api/v1/recalls", json=body)
    assert r2.status_code == 200
    assert r2.json() == r1.json() | {"replayed": True}
    assert client.db.execute(
        "SELECT COUNT(*) AS c FROM recalls"
    ).fetchone()["c"] == 1

    # 同号不同内容 -> 409
    r3 = client.post("/api/v1/recalls", json={
        "recall_no": "RC-X", "source_sample_id": "S0", "reason": "其他原因",
    })
    assert r3.status_code == 409
    assert r3.json()["conflicts"][0]["code"] == "recall_no_conflict"


def test_close_recall_is_idempotent_and_lifts_tracking(client):
    _sampling(client, "OP-1", "S0", 100)
    _aliquot(client, "OP-2", "S0", [("A1", 10)])
    _open_recall(client, "RC-1", "S0")

    r = client.post("/api/v1/recalls/RC-1/close",
                    json={"operator": "王关闭", "note": "复检合格"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "closed"
    assert body["replayed"] is False
    assert body["closed_at"] is not None
    assert body["close_operator"] == "王关闭"
    assert body["affected_samples"] == []
    assert body["affected_count"] == 0

    # 重复关闭:幂等回放首次结果
    r2 = client.post("/api/v1/recalls/RC-1/close")
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["replayed"] is True
    assert b2["closed_at"] == body["closed_at"]
    assert b2["close_operator"] == "王关闭"

    # 关闭后不再追踪
    assert client.get("/api/v1/recalls/affected-samples").json()["samples"] == []
    assert client.get("/api/v1/samples/S0/recalls").json()["recalled"] is False

    # 对已关闭召回号再次开启 -> 409 recall_closed
    r3 = _open_recall(client, "RC-1", "S0")
    assert r3.status_code == 409
    assert r3.json()["conflicts"][0]["code"] == "recall_closed"


def test_close_unknown_recall_404(client):
    r = client.post("/api/v1/recalls/NOPE/close")
    assert r.status_code == 404
    assert r.json()["conflicts"][0]["code"] == "recall_not_found"


def test_list_recalls_filter_by_status(client):
    _sampling(client, "OP-1", "S0", 100)
    _open_recall(client, "RC-1", "S0")
    _open_recall(client, "RC-2", "S0")
    client.post("/api/v1/recalls/RC-2/close")

    r = client.get("/api/v1/recalls")
    assert r.json()["total"] == 2
    assert [x["recall_no"] for x in r.json()["items"]] == ["RC-1", "RC-2"]

    active = client.get("/api/v1/recalls?status=active").json()
    assert active["total"] == 1
    assert active["items"][0]["recall_no"] == "RC-1"

    closed = client.get("/api/v1/recalls?status=closed").json()
    assert closed["total"] == 1
    assert closed["items"][0]["recall_no"] == "RC-2"
    assert closed["items"][0]["affected_samples"] == []


# ---------------------------------------------------------------- 核销拦截

def test_consumption_blocked_for_recalled_sample_itemwise(client):
    _sampling(client, "OP-1", "S1", 100)
    _sampling(client, "OP-2", "S2", 100)
    _reserve(client, "RSV-1", [
        {"sample_id": "S1", "amount": 30},
        {"sample_id": "S2", "amount": 20},
    ])
    _open_recall(client, "RC-1", "S1", reason="污染")

    # S1 被召回、S2 正常:逐项冲突返回,整单不扣量
    r = _consume(client, "CV-1", "RSV-1", [
        {"sample_id": "S1", "amount": 10},
        {"sample_id": "S2", "amount": 5},
    ])
    assert r.status_code == 409
    conflicts = r.json()["conflicts"]
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["code"] == "sample_under_recall"
    assert c["sample_id"] == "S1"
    assert c["op_no"] == "CV-1"
    assert c["recall_nos"] == ["RC-1"]
    assert "RC-1" in c["message"] and "污染" in c["message"]

    # 整单不扣量:S2 也没有被核销,余量/版本/锁定不变
    assert client.get("/api/v1/consumptions/CV-1").status_code == 404
    av1 = client.get("/api/v1/reservations?sample_id=S1").json()["samples"][0]
    av2 = client.get("/api/v1/reservations?sample_id=S2").json()["samples"][0]
    assert (av1["remaining"], av1["reserved"]) == (100.0, 30.0)
    assert (av2["remaining"], av2["reserved"]) == (100.0, 20.0)
    assert client.db.execute(
        "SELECT version FROM samples WHERE sample_id = 'S1'"
    ).fetchone()["version"] == 0


def test_consumption_blocked_for_descendant_of_recalled_source(client):
    _lineage(client)
    # 对后代建预留(C1 是 S0 的第三代后代)
    _reserve(client, "RSV-1", [{"sample_id": "C1", "amount": 2}])
    _open_recall(client, "RC-1", "S0", reason="源头污染")

    r = _consume(client, "CV-1", "RSV-1", [{"sample_id": "C1", "amount": 1}])
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "sample_under_recall"
    assert c["sample_id"] == "C1"
    assert c["recall_nos"] == ["RC-1"]


def test_consumption_resumes_after_recall_closed_for_existing_reservation(client):
    """关闭召回后恢复核销;召回开启前已建的预留同样适用。"""
    _sampling(client, "OP-1", "S1", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 30}])  # 召回前已建预留
    _open_recall(client, "RC-1", "S1")

    r = _consume(client, "CV-1", "RSV-1", [{"sample_id": "S1", "amount": 10}])
    assert r.status_code == 409

    assert client.post("/api/v1/recalls/RC-1/close").status_code == 200

    # 关闭后同一核销号、同一预留单核销成功
    r = _consume(client, "CV-1", "RSV-1", [{"sample_id": "S1", "amount": 10}])
    assert r.status_code == 200, r.text
    item = r.json()["items"][0]
    assert item["remaining"] == 90.0
    assert item["remaining_locked"] == 20.0


def test_multiple_recalls_all_listed_in_conflict(client):
    """同一样品经合流同时受多个活跃召回影响时,逐项冲突列出全部召回号。"""
    _sampling(client, "OP-1", "S1", 100)
    _sampling(client, "OP-2", "S2", 100)
    _aliquot(client, "OP-3", "S1", [("M", 10)])
    _merge(client, "OP-4", [("S2", 10)], "M", material="混合")
    _reserve(client, "RSV-1", [{"sample_id": "M", "amount": 5}])
    _open_recall(client, "RC-1", "S1", reason="S1 污染")
    _open_recall(client, "RC-2", "S2", reason="S2 污染")

    r = _consume(client, "CV-1", "RSV-1", [{"sample_id": "M", "amount": 1}])
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "sample_under_recall"
    assert c["recall_nos"] == ["RC-1", "RC-2"]

    # 查询接口同样返回两个召回(按跳数、召回号排序)
    hits = client.get("/api/v1/samples/M/recalls").json()["recalls"]
    assert [h["recall_no"] for h in hits] == ["RC-1", "RC-2"]
    affected = client.get(
        "/api/v1/recalls/affected-samples?sample_id=M"
    ).json()["samples"][0]
    assert affected["recall_nos"] == ["RC-1", "RC-2"]


def test_closing_one_of_two_recalls_keeps_other_blocking(client):
    _sampling(client, "OP-1", "S1", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 30}])
    _open_recall(client, "RC-1", "S1", reason="原因1")
    _open_recall(client, "RC-2", "S1", reason="原因2")

    client.post("/api/v1/recalls/RC-1/close")
    r = _consume(client, "CV-1", "RSV-1", [{"sample_id": "S1", "amount": 1}])
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["recall_nos"] == ["RC-2"]

    client.post("/api/v1/recalls/RC-2/close")
    r = _consume(client, "CV-1", "RSV-1", [{"sample_id": "S1", "amount": 1}])
    assert r.status_code == 200, r.text


def test_recall_and_other_conflicts_collected_together(client):
    """召回冲突与未核销量不足等其他冲突在同一响应中一并返回。"""
    _sampling(client, "OP-1", "S1", 100)
    _sampling(client, "OP-2", "S2", 100)
    _reserve(client, "RSV-1", [
        {"sample_id": "S1", "amount": 10},
        {"sample_id": "S2", "amount": 10},
    ])
    _open_recall(client, "RC-1", "S1")
    r = _consume(client, "CV-BAD", "RSV-1", [
        {"sample_id": "S1", "amount": 1},   # 受召回
        {"sample_id": "S2", "amount": 99},  # 超未核销量
    ])
    assert r.status_code == 409
    codes = {c["code"] for c in r.json()["conflicts"]}
    assert codes == {"sample_under_recall", "insufficient_unconsumed"}
    # 整单不变
    assert client.get("/api/v1/consumptions/CV-BAD").status_code == 404


def test_recall_conflict_does_not_affect_replay(client):
    """核销号已落库后开启召回,同号重放仍返回首次结果且不再次扣量。"""
    _sampling(client, "OP-1", "S1", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 30}])
    body = {"consumption_no": "CV-1", "reservation_no": "RSV-1",
            "items": [{"sample_id": "S1", "amount": 10}]}
    first = client.post("/api/v1/consumptions", json=body)
    assert first.status_code == 200

    _open_recall(client, "RC-1", "S1")
    replay = client.post("/api/v1/consumptions", json=body)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    av = client.get("/api/v1/reservations?sample_id=S1").json()["samples"][0]
    assert av["remaining"] == 90.0  # 没有二次扣减


# ---------------------------------------------------------------- 旧库建表

def test_recall_tables_created_on_old_database(tmp_path):
    """旧版本数据库(无召回表)启动初始化后自动补齐,迁移幂等。"""
    db_path = str(tmp_path / "old.db")
    conn = connect(db_path)
    conn.executescript(
        "CREATE TABLE samples (sample_id TEXT PRIMARY KEY, material TEXT NOT NULL,"
        " unit TEXT NOT NULL, initial_amount REAL NOT NULL, remaining REAL NOT NULL,"
        " version INTEGER NOT NULL DEFAULT 0, location TEXT, is_root INTEGER NOT NULL"
        " DEFAULT 0, created_at TEXT);"
        "CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT);"
    )
    conn.execute(
        "INSERT INTO samples (sample_id, material, unit, initial_amount,"
        " remaining, is_root) VALUES ('S1', 'M', 'mL', 100, 100, 1)"
    )
    init_db(conn)
    init_db(conn)  # 幂等
    names = {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "recalls" in names

    from app import recalls as rcl
    from app.schemas import RecallRequest

    result = rcl.open_recall(conn, RecallRequest(
        recall_no="RC-1", source_sample_id="S1", reason="污染",
    ))
    assert result.status == "active"
    assert [x.sample_id for x in result.affected_samples] == ["S1"]
    closed = rcl.close_recall(conn, "RC-1")
    assert closed.status == "closed"
    conn.close()
