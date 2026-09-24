"""样品召回服务:用召回号、源样品号和原因开启召回,按召回号关闭。

规则
----
- 开启:单个 `BEGIN IMMEDIATE` 事务内校验源样品存在,写入召回单(active)。
- 活跃召回**实时**沿谱系边(`lineage_edges` 父->子)追踪源样品及其**全部后代**:
  合并(合流)路径对同一召回只计一次(节点去重),深度取最短跳数;
  查询结果始终按当前谱系实时重算,召回开启后新建的分装/合并后代自动纳入,
  关闭召回后实时移除。
- 关闭:按召回号把状态置为 closed(幂等:重复关闭返回首次关闭结果)。
- 核销领用在其原有单事务内逐项检查样品是否受活跃召回影响,
  命中即逐项冲突、整单不扣量;关闭召回后恢复核销(已建预留同样适用)。

幂等
----
同一召回号 + 同一请求体重放返回首次结果(`replayed: true`),
不重复开启;同号不同内容返回 `409 recall_no_conflict`;
对已关闭召回号再次开启返回 `409 recall_closed`。
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from . import schemas as s

_CYCLE_LIMIT = 100000  # 谱系遍历节点上限,防御性保护(谱系本身为 DAG)


class RecallError(Exception):
    """样品召回业务冲突,携带结构化冲突列表。"""

    def __init__(self, conflicts: list[s.Conflict]):
        self.conflicts = conflicts
        super().__init__(conflicts[0].message if conflicts else "recall conflict")


def _conflict(
    code: str,
    message: str,
    *,
    recall_no: Optional[str] = None,
    sample_id: Optional[str] = None,
    recall_nos: Optional[list[str]] = None,
) -> s.Conflict:
    return s.Conflict(
        code=code,
        message=message,
        op_no=recall_no,
        sample_id=sample_id,
        recall_nos=recall_nos,
    )


# ---------------------------------------------------------------- 谱系追踪

def _children_map(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """当前全部谱系边的 父 -> [子] 邻接表。"""
    by_parent: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT parent_id, child_id FROM lineage_edges WHERE parent_id IS NOT NULL"
    ):
        by_parent.setdefault(row["parent_id"], []).append(row["child_id"])
    return by_parent


def _descendant_closure(
    children: dict[str, list[str]], source: str
) -> dict[str, int]:
    """从源样品沿 父->子 方向 BFS,返回 {节点: 最短跳数}。

    包含源样品本身(depth=0);合流(多条合并路径到达同一样品)只计一次,
    深度取首次到达的最短跳数。
    """
    depths = {source: 0}
    queue = [source]
    head = 0
    while head < len(queue):
        node = queue[head]
        head += 1
        for child in children.get(node, ()):
            if child in depths:
                continue
            depths[child] = depths[node] + 1
            if len(depths) > _CYCLE_LIMIT:
                raise RecallError([_conflict(
                    "lineage_too_large",
                    "召回追踪的谱系规模超过遍历上限,拒绝查询",
                    sample_id=source,
                )])
            queue.append(child)
    return depths


def _active_headers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM recalls WHERE status = 'active' ORDER BY recall_no"
    ).fetchall()


def hits_for_samples(
    conn: sqlite3.Connection, sample_ids: list[str]
) -> dict[str, list[s.RecallHitOut]]:
    """批量计算各样品当前命中的全部活跃召回(源样品自身也算命中)。

    每个活跃召回对其源样品的后代闭包只展开一次,合流路径天然去重。
    """
    wanted = set(sample_ids)
    out: dict[str, list[s.RecallHitOut]] = {sid: [] for sid in sample_ids}
    if not wanted:
        return out
    children = _children_map(conn)
    for header in _active_headers(conn):
        closure = _descendant_closure(children, header["source_sample_id"])
        for sid, depth in closure.items():
            if sid in wanted:
                out[sid].append(s.RecallHitOut(
                    recall_no=header["recall_no"],
                    source_sample_id=header["source_sample_id"],
                    reason=header["reason"],
                    depth=depth,
                    operator=header["operator"],
                    created_at=header["created_at"],
                ))
    for hits in out.values():
        hits.sort(key=lambda h: (h.depth, h.recall_no))
    return out


def _affected_for(
    conn: sqlite3.Connection, header: sqlite3.Row
) -> list[s.RecallAffectedSampleOut]:
    """单个召回实时追踪的受影响样品(按跳数、样品号升序)。"""
    children = _children_map(conn)
    closure = _descendant_closure(children, header["source_sample_id"])
    return [
        s.RecallAffectedSampleOut(sample_id=sid, depth=depth)
        for sid, depth in sorted(closure.items(), key=lambda kv: (kv[1], kv[0]))
    ]


# ---------------------------------------------------------------- 查询/组装

def _get_header(
    conn: sqlite3.Connection, recall_no: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM recalls WHERE recall_no = ?", (recall_no,)
    ).fetchone()


def _build_result(
    conn: sqlite3.Connection, header: sqlite3.Row, *, replayed: bool
) -> s.RecallResult:
    # 受影响范围只对活跃召回有意义;已关闭召回不再追踪,返回空列表
    if header["status"] == "active":
        affected = _affected_for(conn, header)
    else:
        affected = []
    return s.RecallResult(
        recall_no=header["recall_no"],
        source_sample_id=header["source_sample_id"],
        reason=header["reason"],
        status=header["status"],
        operator=header["operator"],
        note=header["note"],
        close_operator=header["close_operator"],
        close_note=header["close_note"],
        affected_samples=affected,
        affected_count=len(affected),
        replayed=replayed,
        created_at=header["created_at"],
        closed_at=header["closed_at"],
    )


# ---------------------------------------------------------------- 开启召回

def open_recall(
    conn: sqlite3.Connection, req: s.RecallRequest
) -> s.RecallResult:
    """单事务校验源样品存在并开启召回;同号同内容幂等,异内容冲突。"""
    payload = req.model_dump_json()

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _get_header(conn, req.recall_no)
        if existing is not None:
            if existing["status"] == "closed":
                raise RecallError([
                    _conflict(
                        "recall_closed",
                        f"召回号 {req.recall_no} 已关闭,不能再次开启",
                        recall_no=req.recall_no,
                    )
                ])
            if existing["payload"] != payload:
                raise RecallError([
                    _conflict(
                        "recall_no_conflict",
                        f"召回号 {req.recall_no} 已存在但请求内容不同,"
                        "同一召回号必须对应同一次召回(幂等键冲突)",
                        recall_no=req.recall_no,
                    )
                ])
            result = _build_result(conn, existing, replayed=True)
            conn.execute("COMMIT")
            return result

        if conn.execute(
            "SELECT 1 FROM samples WHERE sample_id = ?", (req.source_sample_id,)
        ).fetchone() is None:
            raise RecallError([
                _conflict(
                    "sample_not_found",
                    f"源样品 {req.source_sample_id} 不存在,无法开启召回",
                    recall_no=req.recall_no,
                    sample_id=req.source_sample_id,
                )
            ])

        conn.execute(
            "INSERT INTO recalls (recall_no, source_sample_id, reason,"
            " operator, note, payload) VALUES (?, ?, ?, ?, ?, ?)",
            (req.recall_no, req.source_sample_id, req.reason,
             req.operator, req.note, payload),
        )
        header = _get_header(conn, req.recall_no)
        result = _build_result(conn, header, replayed=False)
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 关闭召回

def close_recall(
    conn: sqlite3.Connection,
    recall_no: str,
    body: Optional[s.RecallCloseRequest] = None,
) -> s.RecallResult:
    """按召回号关闭(幂等:重复关闭返回首次关闭结果)。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _get_header(conn, recall_no)
        if existing is None:
            raise RecallError([
                _conflict(
                    "recall_not_found",
                    f"召回号 {recall_no} 不存在,无法关闭",
                    recall_no=recall_no,
                )
            ])

        replayed = existing["status"] == "closed"
        if not replayed:
            operator = body.operator if body is not None else None
            note = body.note if body is not None else None
            conn.execute(
                "UPDATE recalls SET status = 'closed', close_operator = ?,"
                " close_note = ?,"
                " closed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
                " WHERE recall_no = ? AND status = 'active'",
                (operator, note, recall_no),
            )
            existing = _get_header(conn, recall_no)

        result = _build_result(conn, existing, replayed=replayed)
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 查询

def get_recall(conn: sqlite3.Connection, recall_no: str) -> s.RecallResult:
    header = _get_header(conn, recall_no)
    if header is None:
        raise RecallError([
            _conflict(
                "recall_not_found",
                f"召回号 {recall_no} 不存在",
                recall_no=recall_no,
            )
        ])
    return _build_result(conn, header, replayed=False)


def list_recalls(
    conn: sqlite3.Connection,
    *,
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> s.RecallListResponse:
    """召回单列表(可按状态过滤),各单附当前实时受影响样品。"""
    where = "WHERE status = ?" if status in ("active", "closed") else ""
    params: tuple = (status,) if where else ()
    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM recalls {where}", params
    ).fetchone()["c"]
    rows = conn.execute(
        f"SELECT * FROM recalls {where} ORDER BY recall_no LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return s.RecallListResponse(
        total=total,
        items=[_build_result(conn, r, replayed=False) for r in rows],
    )


def get_affected_samples(
    conn: sqlite3.Connection, sample_ids: Optional[list[str]] = None
) -> s.AffectedSamplesResponse:
    """查询当前受活跃召回影响的全部样品(或指定样品)及对应召回号。

    结果实时按当前谱系重算;合并路径对同一召回只计一次,
    同一样品可因合流同时受多个召回影响。
    """
    conn.execute("BEGIN")
    try:
        children = _children_map(conn)
        # 样品 -> {召回号: hit}
        agg: dict[str, dict[str, s.RecallHitOut]] = {}
        for header in _active_headers(conn):
            closure = _descendant_closure(children, header["source_sample_id"])
            for sid, depth in closure.items():
                agg.setdefault(sid, {})[header["recall_no"]] = s.RecallHitOut(
                    recall_no=header["recall_no"],
                    source_sample_id=header["source_sample_id"],
                    reason=header["reason"],
                    depth=depth,
                    operator=header["operator"],
                    created_at=header["created_at"],
                )

        if sample_ids is not None:
            ids = list(dict.fromkeys(sample_ids))  # 去重保序
            found = {
                r["sample_id"]
                for r in conn.execute(
                    f"SELECT sample_id FROM samples"
                    f" WHERE sample_id IN ({','.join('?' * len(ids))})",
                    ids,
                ).fetchall()
            } if ids else set()
            missing = [sid for sid in ids if sid not in found]
            if missing:
                raise RecallError([
                    _conflict(
                        "sample_not_found",
                        f"样品 {', '.join(missing)} 不存在",
                        sample_id=missing[0],
                    )
                ])
            ordered = [sid for sid in ids if sid in agg]
            # 指定样品查询保持入参顺序;重复入参只返回一次
            ordered = list(dict.fromkeys(ordered))
        else:
            ordered = sorted(agg.keys())

        result = s.AffectedSamplesResponse(samples=[
            s.AffectedSampleOut(
                sample_id=sid,
                recall_nos=sorted(agg[sid].keys()),
                recalls=sorted(agg[sid].values(), key=lambda h: (h.depth, h.recall_no)),
            )
            for sid in ordered
        ])
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_sample_recalls(
    conn: sqlite3.Connection, sample_id: str
) -> s.SampleRecallsResponse:
    """查询单个样品当前命中的全部活跃召回(含源样品自身)。"""
    conn.execute("BEGIN")
    try:
        if conn.execute(
            "SELECT 1 FROM samples WHERE sample_id = ?", (sample_id,)
        ).fetchone() is None:
            raise RecallError([
                _conflict(
                    "sample_not_found",
                    f"样品 {sample_id} 不存在",
                    sample_id=sample_id,
                )
            ])
        hits = hits_for_samples(conn, [sample_id])[sample_id]
        result = s.SampleRecallsResponse(
            sample_id=sample_id,
            recalled=bool(hits),
            recalls=hits,
        )
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise
