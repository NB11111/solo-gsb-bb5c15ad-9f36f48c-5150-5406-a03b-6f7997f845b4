"""样品冷链测温模块端到端测试:

- 测温配置:已有样品登记固定上下限与最大采样间隔;同样品同内容幂等重放,
  异内容 409;样品不存在 409;min>=max / 间隔非正 422
- 测温读数:带时区时间归一化 UTC;同时刻同值重放不新增(不同时区写法同源),
  异值 409;不带时区 422;样品不存在 409
- 越界区间:按当前全部读数实时重算,相邻同向越界读数间隔 <= 配置值归并,
  正常读数/超间隔/方向改变截断;返回起止时间、方向与极值;乱序补录可合并/拆分
- 配置与读数表不可变(触发器);旧库启动自动建表;并发由 BEGIN IMMEDIATE 串行化
"""
from __future__ import annotations

import threading

import pytest
import sqlite3
from fastapi.testclient import TestClient

from app import temperatures as tp
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
        yield c
    app.dependency_overrides.clear()
    mem.close()


def _sample(client, sid, op_no=None):
    r = client.post("/api/v1/operations/sampling", json={
        "type": "sampling", "op_no": op_no or f"OP-{sid}", "sample_id": sid,
        "material": "血清", "amount": 100, "unit": "mL",
    })
    assert r.status_code == 200, r.text


def _profile(client, sid, lo=2.0, hi=8.0, interval=600):
    return client.post("/api/v1/temperature-profiles", json={
        "sample_id": sid, "min_temp": lo, "max_temp": hi,
        "max_interval_seconds": interval,
    })


def _read(client, sid, at, temp):
    return client.post(
        f"/api/v1/samples/{sid}/temperature-readings",
        json={"measured_at": at, "temperature": temp},
    )


def _excursions(client, sid):
    return client.get(f"/api/v1/samples/{sid}/temperature/excursions")


def _reading_count(client, sid):
    conn = next(app.dependency_overrides[get_conn]())
    return conn.execute(
        "SELECT COUNT(*) AS c FROM temperature_readings WHERE sample_id = ?",
        (sid,),
    ).fetchone()["c"]


# ---------------------------------------------------------------- 测温配置

def test_register_profile_success_and_replay(client):
    _sample(client, "S1")
    r = _profile(client, "S1", lo=2.0, hi=8.0, interval=600)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sample_id"] == "S1"
    assert body["min_temp"] == 2.0
    assert body["max_temp"] == 8.0
    assert body["max_interval_seconds"] == 600
    assert body["replayed"] is False
    assert body["created_at"]

    # 同内容重放:返回首次结果,不新增配置
    r2 = _profile(client, "S1", lo=2.0, hi=8.0, interval=600)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["created_at"] == body["created_at"]
    conn = next(app.dependency_overrides[get_conn]())
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM sample_temp_profiles"
    ).fetchone()["c"] == 1


def test_register_profile_different_content_conflicts(client):
    _sample(client, "S1")
    assert _profile(client, "S1", lo=2.0, hi=8.0).status_code == 200
    r = _profile(client, "S1", lo=2.0, hi=9.0)
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "temp_profile_conflict"
    assert c["sample_id"] == "S1"
    # 间隔不同同样冲突(固定配置不可改)
    assert _profile(client, "S1", interval=300).status_code == 409


def test_register_profile_sample_not_found(client):
    r = _profile(client, "GHOST")
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "sample_not_found"


def test_register_profile_invalid_bounds_or_interval_422(client):
    _sample(client, "S1")
    assert _profile(client, "S1", lo=8.0, hi=2.0).status_code == 422
    assert _profile(client, "S1", lo=5.0, hi=5.0).status_code == 422
    assert _profile(client, "S1", interval=0).status_code == 422
    assert _profile(client, "S1", interval=-10).status_code == 422


# ---------------------------------------------------------------- 测温读数

def test_add_reading_normalizes_timezone_to_utc(client):
    _sample(client, "S1")
    _profile(client, "S1")
    r = _read(client, "S1", "2026-09-24T10:00:00+08:00", 5.5)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["measured_at"] == "2026-09-24T02:00:00.000000Z"
    assert body["temperature"] == 5.5
    assert body["replayed"] is False
    assert body["created_at"]


def test_add_reading_naive_time_is_422(client):
    _sample(client, "S1")
    _profile(client, "S1")
    r = _read(client, "S1", "2026-09-24T10:00:00", 5.5)
    assert r.status_code == 422


def test_add_reading_sample_not_found(client):
    r = _read(client, "GHOST", "2026-09-24T10:00:00+08:00", 5.5)
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "sample_not_found"


def test_same_instant_same_value_replay_adds_no_record(client):
    _sample(client, "S1")
    _profile(client, "S1")
    r1 = _read(client, "S1", "2026-09-24T10:00:00+08:00", 5.5)
    assert r1.status_code == 200
    assert r1.json()["replayed"] is False

    # 同一 UTC 时刻的不同时区写法(+08:00 与 Z)归一后相同:同值重放不新增
    r2 = _read(client, "S1", "2026-09-24T02:00:00Z", 5.5)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["measured_at"] == "2026-09-24T02:00:00.000000Z"
    assert r2.json()["created_at"] == r1.json()["created_at"]
    assert _reading_count(client, "S1") == 1


def test_same_instant_different_value_conflicts(client):
    _sample(client, "S1")
    _profile(client, "S1")
    _read(client, "S1", "2026-09-24T10:00:00+08:00", 5.5)
    r = _read(client, "S1", "2026-09-24T02:00:00Z", 6.0)
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "reading_conflict"
    assert c["sample_id"] == "S1"
    assert c["requested"] == 6.0
    assert c["available"] == 5.5
    assert _reading_count(client, "S1") == 1


def test_readings_do_not_require_profile(client):
    """读数写入只要求样品存在;越界查询才要求已登记配置。"""
    _sample(client, "S1")
    r = _read(client, "S1", "2026-09-24T10:00:00+08:00", 99.0)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- 越界区间

def test_excursions_require_existing_sample_and_profile(client):
    r = _excursions(client, "GHOST")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"

    _sample(client, "S1")
    r = _excursions(client, "S1")
    assert r.status_code == 404
    assert r.json()["error"] == "temp_profile_not_found"

    _profile(client, "S1")
    r = _excursions(client, "S1")
    assert r.status_code == 200
    assert r.json()["intervals"] == []


def test_excursion_grouping_truncation_and_extremes(client):
    """相邻同向越界读数间隔 <= 配置值归并;正常读数/超间隔/方向改变截断。"""
    _sample(client, "S1")
    _profile(client, "S1", lo=2.0, hi=8.0, interval=600)  # 10 分钟
    readings = [
        ("2026-09-24T10:00:00Z", 5.0),    # 正常
        ("2026-09-24T10:05:00Z", 9.0),    # ─┐ 高温区间1(间隔 5 分钟)
        ("2026-09-24T10:10:00Z", 10.0),   # ─┘ 极值 10
        ("2026-09-24T10:15:00Z", 6.0),    # 正常 -> 截断
        ("2026-09-24T10:20:00Z", 1.0),    # ─┐ 低温区间(间隔 5 分钟)
        ("2026-09-24T10:25:00Z", 0.5),    # ─┘ 极值 0.5
        ("2026-09-24T10:50:00Z", -1.0),   # 间隔 25 分钟 > 10 分钟 -> 新区间
        ("2026-09-24T10:55:00Z", 9.0),    # 方向改变 -> 新区间
    ]
    for at, temp in readings:
        assert _read(client, "S1", at, temp).status_code == 200

    body = _excursions(client, "S1").json()
    assert body["min_temp"] == 2.0 and body["max_temp"] == 8.0
    assert body["max_interval_seconds"] == 600
    assert body["intervals"] == [
        {"direction": "above",
         "start_at": "2026-09-24T10:05:00.000000Z",
         "end_at": "2026-09-24T10:10:00.000000Z",
         "extreme_value": 10.0,
         "extreme_at": "2026-09-24T10:10:00.000000Z",
         "reading_count": 2},
        {"direction": "below",
         "start_at": "2026-09-24T10:20:00.000000Z",
         "end_at": "2026-09-24T10:25:00.000000Z",
         "extreme_value": 0.5,
         "extreme_at": "2026-09-24T10:25:00.000000Z",
         "reading_count": 2},
        {"direction": "below",
         "start_at": "2026-09-24T10:50:00.000000Z",
         "end_at": "2026-09-24T10:50:00.000000Z",
         "extreme_value": -1.0,
         "extreme_at": "2026-09-24T10:50:00.000000Z",
         "reading_count": 1},
        {"direction": "above",
         "start_at": "2026-09-24T10:55:00.000000Z",
         "end_at": "2026-09-24T10:55:00.000000Z",
         "extreme_value": 9.0,
         "extreme_at": "2026-09-24T10:55:00.000000Z",
         "reading_count": 1},
    ]


def test_excursion_gap_exactly_at_interval_stays_one_interval(client):
    _sample(client, "S1")
    _profile(client, "S1", interval=600)
    _read(client, "S1", "2026-09-24T10:00:00Z", 9.0)
    _read(client, "S1", "2026-09-24T10:10:00Z", 9.5)   # 间隔恰好 600 秒
    _read(client, "S1", "2026-09-24T10:20:01Z", 9.5)   # 间隔 601 秒 -> 截断
    body = _excursions(client, "S1").json()
    assert [(i["start_at"], i["end_at"], i["reading_count"])
            for i in body["intervals"]] == [
        ("2026-09-24T10:00:00.000000Z", "2026-09-24T10:10:00.000000Z", 2),
        ("2026-09-24T10:20:01.000000Z", "2026-09-24T10:20:01.000000Z", 1),
    ]


def test_readings_exactly_at_bounds_are_normal(client):
    """恰等于上下限的读数属于正常读数,会截断区间。"""
    _sample(client, "S1")
    _profile(client, "S1", lo=2.0, hi=8.0)
    _read(client, "S1", "2026-09-24T10:00:00Z", 9.0)
    _read(client, "S1", "2026-09-24T10:05:00Z", 8.0)   # 恰等上限:正常
    _read(client, "S1", "2026-09-24T10:10:00Z", 2.0)   # 恰等下限:正常
    _read(client, "S1", "2026-09-24T10:15:00Z", 1.0)
    body = _excursions(client, "S1").json()
    assert [(i["direction"], i["reading_count"]) for i in body["intervals"]] == [
        ("above", 1), ("below", 1),
    ]


def test_out_of_order_backfill_splits_existing_interval(client):
    """乱序补录的正常读数落在越界读数之间,重算后拆分既有区间。"""
    _sample(client, "S1")
    _profile(client, "S1", interval=600)
    _read(client, "S1", "2026-09-24T10:00:00Z", 9.0)
    _read(client, "S1", "2026-09-24T10:05:00Z", 10.0)
    _read(client, "S1", "2026-09-24T10:10:00Z", 9.0)
    before = _excursions(client, "S1").json()["intervals"]
    assert len(before) == 1 and before[0]["reading_count"] == 3

    # 补录一条 10:02 的正常读数 -> 原区间被拆成两段
    _read(client, "S1", "2026-09-24T10:02:00Z", 5.0)
    after = _excursions(client, "S1").json()["intervals"]
    assert [(i["start_at"], i["end_at"], i["reading_count"]) for i in after] == [
        ("2026-09-24T10:00:00.000000Z", "2026-09-24T10:00:00.000000Z", 1),
        ("2026-09-24T10:05:00.000000Z", "2026-09-24T10:10:00.000000Z", 2),
    ]


def test_out_of_order_backfill_merges_existing_intervals(client):
    """乱序补录的越界读数桥接两个区间(两侧间隔均 <= 配置值),重算后合并。"""
    _sample(client, "S1")
    _profile(client, "S1", interval=600)
    _read(client, "S1", "2026-09-24T10:00:00Z", 9.0)
    _read(client, "S1", "2026-09-24T10:18:00Z", 9.0)   # 间隔 18 分钟 -> 两个区间
    before = _excursions(client, "S1").json()["intervals"]
    assert len(before) == 2

    # 补录 10:09 的越界读数,两侧间隔各 9 分钟 -> 合并为一个区间
    _read(client, "S1", "2026-09-24T10:09:00Z", 9.5)
    after = _excursions(client, "S1").json()["intervals"]
    assert len(after) == 1
    assert after[0]["start_at"] == "2026-09-24T10:00:00.000000Z"
    assert after[0]["end_at"] == "2026-09-24T10:18:00.000000Z"
    assert after[0]["reading_count"] == 3
    assert after[0]["extreme_value"] == 9.5
    assert after[0]["extreme_at"] == "2026-09-24T10:09:00.000000Z"


def test_out_of_order_insert_with_fractional_seconds_sorts_correctly(client):
    """带毫秒与不带毫秒的读数混排时仍按时间排序(字符串排序即时间排序)。"""
    _sample(client, "S1")
    _profile(client, "S1", interval=600)
    _read(client, "S1", "2026-09-24T10:00:00.500+08:00", 9.0)
    _read(client, "S1", "2026-09-24T02:00:00Z", 9.5)   # 同一秒整,乱序补录
    body = _excursions(client, "S1").json()
    assert len(body["intervals"]) == 1
    iv = body["intervals"][0]
    assert iv["start_at"] == "2026-09-24T02:00:00.000000Z"
    assert iv["end_at"] == "2026-09-24T02:00:00.500000Z"
    assert iv["extreme_value"] == 9.5
    assert iv["extreme_at"] == "2026-09-24T02:00:00.000000Z"


# ---------------------------------------------------------------- 不可变审计

def test_temperature_tables_are_immutable(client):
    _sample(client, "S1")
    _profile(client, "S1")
    _read(client, "S1", "2026-09-24T10:00:00Z", 5.0)

    conn = next(app.dependency_overrides[get_conn]())
    for sql in (
        "UPDATE sample_temp_profiles SET min_temp = 0",
        "DELETE FROM sample_temp_profiles",
        "UPDATE temperature_readings SET temperature = 0",
        "DELETE FROM temperature_readings",
    ):
        with pytest.raises(sqlite3.Error):
            conn.execute(sql)


# ---------------------------------------------------------------- 并发

def test_concurrent_conflicting_readings_serialize(tmp_path):
    """两线程对同样品同一 UTC 时刻写不同温度:BEGIN IMMEDIATE 串行化,
    一者成功一者 reading_conflict,库中只有一条读数。"""
    db_path = str(tmp_path / "t.db")
    setup = connect(db_path)
    init_db(setup)
    setup.execute(
        "INSERT INTO samples (sample_id, material, unit, initial_amount,"
        " remaining, version, is_root) VALUES ('S1', 'M', 'mL', 100, 100, 0, 1)"
    )
    setup.commit()
    setup.close()

    from app.schemas import TemperatureReadingIn

    winners: list[float] = []
    losers: list[str] = []
    barrier = threading.Barrier(2)

    def worker(temp: float) -> None:
        c = connect(db_path)
        barrier.wait()
        try:
            tp.add_reading(c, "S1", TemperatureReadingIn(
                measured_at="2026-09-24T10:00:00+08:00", temperature=temp,
            ))
            winners.append(temp)
        except tp.TemperatureError as exc:
            losers.append(exc.conflicts[0].code)
        finally:
            c.close()

    t1 = threading.Thread(target=worker, args=(5.0,))
    t2 = threading.Thread(target=worker, args=(6.0,))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert len(winners) == 1
    assert losers == ["reading_conflict"]

    check = connect(db_path)
    rows = list(check.execute(
        "SELECT measured_at_utc, temperature FROM temperature_readings"
    ))
    assert len(rows) == 1
    assert rows[0]["measured_at_utc"] == "2026-09-24T02:00:00.000000Z"
    assert rows[0]["temperature"] == winners[0]
    check.close()


# ---------------------------------------------------------------- 旧库迁移

def test_init_db_creates_temperature_tables_on_legacy_database(tmp_path):
    """旧版本数据库(仅有老 samples 表)启动初始化后,测温表自动补齐且全流程可用。"""
    db_path = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(db_path)
    raw.execute(
        "CREATE TABLE samples (sample_id TEXT PRIMARY KEY, material TEXT NOT NULL,"
        " unit TEXT NOT NULL, initial_amount REAL NOT NULL, remaining REAL NOT NULL,"
        " location TEXT, is_root INTEGER NOT NULL DEFAULT 0, created_at TEXT)"
    )
    raw.execute(
        "INSERT INTO samples (sample_id, material, unit, initial_amount,"
        " remaining, location, is_root) VALUES"
        " ('S1', 'M', 'mL', 100, 100, NULL, 1)"
    )
    raw.commit()
    raw.close()

    conn = connect(db_path)
    init_db(conn)
    init_db(conn)  # 再执行一次也不重复创建

    tables = {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"sample_temp_profiles", "temperature_readings"} <= tables

    from app.schemas import TemperatureReadingIn, TempProfileRequest

    res = tp.register_profile(conn, TempProfileRequest(
        sample_id="S1", min_temp=2.0, max_temp=8.0, max_interval_seconds=600,
    ))
    assert res.replayed is False
    tp.add_reading(conn, "S1", TemperatureReadingIn(
        measured_at="2026-09-24T10:00:00+08:00", temperature=9.0,
    ))
    got = tp.get_excursions(conn, "S1")
    assert len(got.intervals) == 1
    assert got.intervals[0].direction == "above"
    conn.close()
