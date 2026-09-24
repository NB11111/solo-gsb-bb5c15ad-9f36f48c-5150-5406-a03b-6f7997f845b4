"""样品冷链测温服务:固定温区配置、带时区测温读数与越界区间重算。

规则
----
- 每个样品先登记**一份固定**测温配置:允许温度上下限 `min_temp`/`max_temp`
  与最大采样间隔 `max_interval_seconds`(秒)。样品必须已存在;配置登记后
  不可修改(触发器禁止 UPDATE/DELETE),同样品同内容重放返回首次结果,
  同样品不同内容返回 `409 temp_profile_conflict`。
- 测温读数按样品写入,请求中的测温时间**必须带时区**,服务端归一化为 UTC
  (ISO 8601,`Z` 结尾)后与样品号共同构成幂等键:
    * 同样品同 UTC 时刻 + 同温度重放 -> 不新增记录,返回原读数 `replayed: true`;
    * 同样品同 UTC 时刻 + 异温度 -> `409 reading_conflict`。
  读数本身不可变(触发器禁止 UPDATE/DELETE),允许乱序补录。
- 越界区间查询**不持久化区间**,每次依据当前全部读数按 UTC 时间升序重算:
  读数温度严格低于下限为 below、严格高于上限为 above;相邻**同向**越界读数
  仅在时间间隔不超过 `max_interval_seconds`(<=)时归为同一区间;正常读数、
  超间隔或方向改变都截断区间。每个区间返回起止时间、越界方向与极值
  (below 取最低、above 取最高)。因此乱序补录的正常读数可**拆分**既有区间,
  补录的越界读数可**合并**既有区间。
- **带保管位置的越界区间查询**在上述重算之外,另按位置交接明细
  (`location_transfer_items`)与冲正审计明细
  (`location_transfer_reversal_items`)的 UTC 落库时间重建读数发生时的保管
  位置:自变更时刻起取新位置(事件时刻与读数时刻相同的,先应用事件再判定该
  读数);首笔变更之前取**该首笔变更的原位置**;没有任何变更则取样品当前
  位置(未登记为 null)。同一时刻的事件先交接后冲正,同类事件按明细编号
  (`item_id`)升序应用。连续同向越界读数除满足间隔条件外,还要求两读数之间
  (前一读数时刻, 本读数时刻] 没有位置变更事件(即同位置)才归为同一区间。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from . import schemas as s


class TemperatureError(Exception):
    """冷链测温业务冲突,携带结构化冲突列表。"""

    def __init__(self, conflicts: list[s.Conflict]):
        self.conflicts = conflicts
        super().__init__(conflicts[0].message if conflicts else "temperature conflict")


def _conflict(
    code: str,
    message: str,
    *,
    sample_id: Optional[str] = None,
    requested: Optional[float] = None,
    available: Optional[float] = None,
) -> s.Conflict:
    return s.Conflict(
        code=code,
        message=message,
        sample_id=sample_id,
        requested=requested,
        available=available,
    )


# ---------------------------------------------------------------- 时间归一化

def normalize_utc(dt: datetime) -> str:
    """把带时区的时间归一化为 UTC ISO 8601 字符串(固定 6 位微秒、Z 结尾)。

    固定宽度保证字符串排序与时间排序一致(SQL 可直接 ORDER BY),
    且同一时刻的不同时区写法归一后为同一字符串(幂等键稳定)。
    """
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_utc(text: str) -> datetime:
    """解析归一化的 UTC 时间字符串(归一化保证可被 fromisoformat 解析)。"""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# ---------------------------------------------------------------- 测温配置

def _sample_exists(conn: sqlite3.Connection, sample_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM samples WHERE sample_id = ?", (sample_id,)
    ).fetchone() is not None


def _get_profile(
    conn: sqlite3.Connection, sample_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sample_temp_profiles WHERE sample_id = ?", (sample_id,)
    ).fetchone()


def register_profile(
    conn: sqlite3.Connection, req: s.TempProfileRequest
) -> s.TempProfileResult:
    """为已有样品登记固定温区与最大采样间隔(按样品幂等,同内容重放/异内容冲突)。"""
    payload = req.model_dump_json()

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _get_profile(conn, req.sample_id)
        if existing is not None:
            if existing["payload"] != payload:
                raise TemperatureError([_conflict(
                    "temp_profile_conflict",
                    f"样品 {req.sample_id} 已登记固定测温配置"
                    f" [{existing['min_temp']}, {existing['max_temp']}] /"
                    f" {existing['max_interval_seconds']}s,"
                    "固定配置不可修改(同一样品只能登记一份)",
                    sample_id=req.sample_id,
                )])
            conn.execute("COMMIT")
            return s.TempProfileResult(
                sample_id=existing["sample_id"],
                min_temp=existing["min_temp"],
                max_temp=existing["max_temp"],
                max_interval_seconds=existing["max_interval_seconds"],
                replayed=True,
                created_at=existing["created_at"],
            )

        if not _sample_exists(conn, req.sample_id):
            raise TemperatureError([_conflict(
                "sample_not_found",
                f"样品 {req.sample_id} 不存在,无法登记测温配置",
                sample_id=req.sample_id,
            )])

        conn.execute(
            "INSERT INTO sample_temp_profiles (sample_id, min_temp, max_temp,"
            " max_interval_seconds, payload) VALUES (?, ?, ?, ?, ?)",
            (req.sample_id, req.min_temp, req.max_temp,
             req.max_interval_seconds, payload),
        )
        row = _get_profile(conn, req.sample_id)
        result = s.TempProfileResult(
            sample_id=row["sample_id"],
            min_temp=row["min_temp"],
            max_temp=row["max_temp"],
            max_interval_seconds=row["max_interval_seconds"],
            replayed=False,
            created_at=row["created_at"],
        )
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 测温读数

def add_reading(
    conn: sqlite3.Connection, sample_id: str, req: s.TemperatureReadingIn
) -> s.TemperatureReadingResult:
    """写入一条带时区测温读数;同样品同 UTC 时刻同值重放不新增,异值冲突。"""
    measured_utc = normalize_utc(req.measured_at)

    conn.execute("BEGIN IMMEDIATE")
    try:
        if not _sample_exists(conn, sample_id):
            raise TemperatureError([_conflict(
                "sample_not_found",
                f"样品 {sample_id} 不存在,无法写入测温读数",
                sample_id=sample_id,
            )])

        existing = conn.execute(
            "SELECT * FROM temperature_readings"
            " WHERE sample_id = ? AND measured_at_utc = ?",
            (sample_id, measured_utc),
        ).fetchone()
        if existing is not None:
            if existing["temperature"] != req.temperature:
                raise TemperatureError([_conflict(
                    "reading_conflict",
                    f"样品 {sample_id} 在 UTC 时刻 {measured_utc} 已有测温值"
                    f" {existing['temperature']},与本次提交 {req.temperature} 不一致;"
                    "同一时刻的读数不可更改",
                    sample_id=sample_id,
                    requested=req.temperature,
                    available=existing["temperature"],
                )])
            conn.execute("COMMIT")
            return s.TemperatureReadingResult(
                sample_id=sample_id,
                measured_at=existing["measured_at_utc"],
                temperature=existing["temperature"],
                replayed=True,
                created_at=existing["created_at"],
            )

        cur = conn.execute(
            "INSERT INTO temperature_readings (sample_id, measured_at_utc, temperature)"
            " VALUES (?, ?, ?)",
            (sample_id, measured_utc, req.temperature),
        )
        row = conn.execute(
            "SELECT * FROM temperature_readings WHERE reading_id = ?",
            (cur.lastrowid,),
        ).fetchone()
        result = s.TemperatureReadingResult(
            sample_id=sample_id,
            measured_at=row["measured_at_utc"],
            temperature=row["temperature"],
            replayed=False,
            created_at=row["created_at"],
        )
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 越界区间重算

def compute_excursions(
    rows: list[sqlite3.Row],
    *,
    min_temp: float,
    max_temp: float,
    max_interval_seconds: int,
) -> list[s.ExcursionInterval]:
    """按 UTC 时间升序扫描全部读数,重算越界区间。

    相邻同向越界读数的时间间隔 <= max_interval_seconds 才延续同一区间;
    正常读数(含恰等上下限)、超间隔或方向改变均截断。
    """
    intervals: list[s.ExcursionInterval] = []
    current: Optional[dict] = None

    for row in rows:
        at = parse_utc(row["measured_at_utc"])
        value = row["temperature"]
        if value < min_temp:
            direction = "below"
        elif value > max_temp:
            direction = "above"
        else:
            direction = None  # 正常读数截断区间

        if direction is None:
            # 正常读数截断区间:先收束正在累积的区间
            if current is not None:
                intervals.append(_interval_from(current))
                current = None
            continue

        gap_ok = (
            current is not None
            and current["direction"] == direction
            and (at - current["last_at"]).total_seconds() <= max_interval_seconds
        )
        if not gap_ok:
            if current is not None:
                intervals.append(_interval_from(current))
            current = {
                "direction": direction,
                "start_at": at,
                "last_at": at,
                "extreme_value": value,
                "extreme_at": at,
                "count": 1,
            }
            continue

        current["last_at"] = at
        current["count"] += 1
        more_extreme = (
            value < current["extreme_value"]
            if direction == "below"
            else value > current["extreme_value"]
        )
        if more_extreme:
            current["extreme_value"] = value
            current["extreme_at"] = at

    if current is not None:
        intervals.append(_interval_from(current))
    return intervals


def _interval_from(current: dict) -> s.ExcursionInterval:
    return s.ExcursionInterval(
        direction=current["direction"],
        start_at=normalize_utc(current["start_at"]),
        end_at=normalize_utc(current["last_at"]),
        extreme_value=current["extreme_value"],
        extreme_at=normalize_utc(current["extreme_at"]),
        reading_count=current["count"],
    )


def get_excursions(
    conn: sqlite3.Connection, sample_id: str
) -> s.ExcursionResponse:
    """依据样品固定配置与当前全部读数实时重算越界区间;未登记配置抛 404。"""
    profile = _get_profile(conn, sample_id)
    if profile is None:
        raise TemperatureError([_conflict(
            "temp_profile_not_found",
            f"样品 {sample_id} 尚未登记测温配置(允许温度上下限与最大采样间隔),"
            "无法计算越界区间",
            sample_id=sample_id,
        )])

    rows = conn.execute(
        "SELECT measured_at_utc, temperature FROM temperature_readings"
        " WHERE sample_id = ? ORDER BY measured_at_utc ASC, reading_id ASC",
        (sample_id,),
    ).fetchall()
    intervals = compute_excursions(
        rows,
        min_temp=profile["min_temp"],
        max_temp=profile["max_temp"],
        max_interval_seconds=profile["max_interval_seconds"],
    )
    return s.ExcursionResponse(
        sample_id=sample_id,
        min_temp=profile["min_temp"],
        max_temp=profile["max_temp"],
        max_interval_seconds=profile["max_interval_seconds"],
        intervals=intervals,
    )


# ---------------------------------------------------- 保管位置重建 + 越界区间

def _load_location_events(
    conn: sqlite3.Connection, sample_id: str
) -> list[dict]:
    """取该样品全部位置变更事件(交接 + 冲正),按应用顺序排序。

    排序键:(事件 UTC 时间, 种类[交接先于冲正], 明细编号升序)。
    交接明细的 to_location 为新位置;冲正明细的 to_location 为恢复后的
    原位置(可能为 null)。两张审计表的 created_at 与事件在同一事务写入,
    即变更生效时刻(UTC)。
    """
    rows = conn.execute(
        "SELECT 'transfer' AS kind, item_id AS item_id, from_location AS"
        " from_location, to_location AS to_location, created_at AS at"
        " FROM location_transfer_items WHERE sample_id = ?"
        " UNION ALL"
        " SELECT 'reversal' AS kind, item_id AS item_id, from_location AS"
        " from_location, to_location AS to_location, created_at AS at"
        " FROM location_transfer_reversal_items WHERE sample_id = ?",
        (sample_id, sample_id),
    ).fetchall()
    events = [
        {
            "kind": r["kind"],
            "item_id": r["item_id"],
            "from_location": r["from_location"],
            "to_location": r["to_location"],
            "at": parse_utc(r["at"]),
        }
        for r in rows
    ]
    # 同刻事件先交接(0)后冲正(1),同类按明细编号升序
    events.sort(key=lambda e: (e["at"], 0 if e["kind"] == "transfer" else 1,
                               e["item_id"]))
    return events


def compute_location_excursions(
    readings: list[sqlite3.Row],
    events: list[dict],
    *,
    current_location: Optional[str],
    min_temp: float,
    max_temp: float,
    max_interval_seconds: int,
) -> list[s.LocationExcursionInterval]:
    """按 UTC 时间升序扫描读数,重算带保管位置的越界区间。

    保管位置:自变更时刻起取新位置(事件与读数同刻先应用事件);首笔变更前
    取该首笔变更的原位置;没有任何变更取样品当前位置(可为 null)。
    归并:相邻同向越界读数除时间间隔 <= max_interval_seconds 外,还要求两
    读数之间(前一读数时刻, 本读数时刻] 没有位置变更事件,即两读数位于同一
    保管位置;正常读数、超间隔、方向改变或期间发生位置变更都截断。
    """
    # 读数在入库时已按 UTC 归一化(固定 6 位微秒),字符串排序即时间排序;
    # 事件 created_at 为毫秒精度,故统一解析为 datetime 后归并。
    ordered = sorted(
        (parse_utc(r["measured_at_utc"]), r["temperature"]) for r in readings
    )

    # 首笔变更之前的位置 = 首笔变更明细登记的原位置;无变更则为样品当前位置
    location = events[0]["from_location"] if events else current_location
    event_idx = 0
    intervals: list[s.LocationExcursionInterval] = []
    current: Optional[dict] = None

    for at, value in ordered:
        # 应用时刻不晚于本读数的全部事件(同刻:交接先、冲正后,item_id 升序)
        changed = False
        while event_idx < len(events) and events[event_idx]["at"] <= at:
            location = events[event_idx]["to_location"]
            changed = True
            event_idx += 1

        if value < min_temp:
            direction = "below"
        elif value > max_temp:
            direction = "above"
        else:
            direction = None  # 正常读数截断区间

        if direction is None:
            if current is not None:
                intervals.append(_location_interval_from(current))
                current = None
            continue

        gap_ok = (
            current is not None
            and current["direction"] == direction
            and (at - current["last_at"]).total_seconds() <= max_interval_seconds
            # changed 恰表示(前一读数时刻, 本读数时刻] 内有位置变更事件:
            # 事件指针在上一读数处理时已消费掉全部 <= 前一读数时刻的事件
            and not changed
        )
        if not gap_ok:
            if current is not None:
                intervals.append(_location_interval_from(current))
            current = {
                "direction": direction,
                "location": location,
                "start_at": at,
                "last_at": at,
                "extreme_value": value,
                "extreme_at": at,
                "count": 1,
            }
            continue

        current["last_at"] = at
        current["count"] += 1
        more_extreme = (
            value < current["extreme_value"]
            if direction == "below"
            else value > current["extreme_value"]
        )
        if more_extreme:
            current["extreme_value"] = value
            current["extreme_at"] = at

    if current is not None:
        intervals.append(_location_interval_from(current))
    return intervals


def _location_interval_from(current: dict) -> s.LocationExcursionInterval:
    return s.LocationExcursionInterval(
        location=current["location"],
        direction=current["direction"],
        start_at=normalize_utc(current["start_at"]),
        end_at=normalize_utc(current["last_at"]),
        extreme_value=current["extreme_value"],
        extreme_at=normalize_utc(current["extreme_at"]),
        reading_count=current["count"],
    )


def get_location_excursions(
    conn: sqlite3.Connection, sample_id: str
) -> s.LocationExcursionResponse:
    """实时重算带保管位置的越界区间;样品或配置缺失由调用方/异常处理为 404。"""
    profile = _get_profile(conn, sample_id)
    if profile is None:
        raise TemperatureError([_conflict(
            "temp_profile_not_found",
            f"样品 {sample_id} 尚未登记测温配置(允许温度上下限与最大采样间隔),"
            "无法计算带保管位置的越界区间",
            sample_id=sample_id,
        )])

    sample = conn.execute(
        "SELECT location FROM samples WHERE sample_id = ?", (sample_id,)
    ).fetchone()
    if sample is None:
        raise TemperatureError([_conflict(
            "sample_not_found",
            f"样品 {sample_id} 不存在",
            sample_id=sample_id,
        )])

    readings = conn.execute(
        "SELECT measured_at_utc, temperature FROM temperature_readings"
        " WHERE sample_id = ? ORDER BY measured_at_utc ASC, reading_id ASC",
        (sample_id,),
    ).fetchall()
    events = _load_location_events(conn, sample_id)
    intervals = compute_location_excursions(
        readings,
        events,
        current_location=sample["location"],
        min_temp=profile["min_temp"],
        max_temp=profile["max_temp"],
        max_interval_seconds=profile["max_interval_seconds"],
    )
    return s.LocationExcursionResponse(
        sample_id=sample_id,
        min_temp=profile["min_temp"],
        max_temp=profile["max_temp"],
        max_interval_seconds=profile["max_interval_seconds"],
        intervals=intervals,
    )
