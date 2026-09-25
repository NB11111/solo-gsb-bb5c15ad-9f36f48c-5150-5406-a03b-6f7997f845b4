"""样品召回服务:以召回号对源样品开启/关闭召回,实时追踪受影响范围。

规则
----
- 开启召回:提交召回号(幂等键)、源样品号与原因,在单个
  `BEGIN IMMEDIATE` 事务内校验源样品存在后落库,状态为 `active`。
- 受影响范围**不持久化**:每次查询/校验时依据当前 `lineage_edges`
  实时沿 父->子 方向遍历,源样品及全部后代均受影响;合并造成的
  汇流路径(多样品并入同一后代)经去重后**只计一次**。召回开启后
  新分装/合并出的后代自动进入受影响范围。
- 关闭召回:按召回号把状态置为 `closed`,受影响样品的核销领用随即恢复;
  重复关闭幂等(返回首次关闭结果)。
- 核销领用联动:consumptions 模块在同一事务内逐项调用
  `active_recall_map` 检查样品是否受活跃召回影响,命中即逐项冲突、
  整单不扣量;对已建预留单同样生效(检查发生在核销时刻)。

幂等
----
同一召回号 + 同一请求体重放返回首次结果(`replayed: true`);
同号不同内容返回 `409 recall_no_conflict`;对已关闭召回号再次开启
返回 `409 recall_closed`。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from . import schemas as s


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
) -> s.Conflict:
    return s.Conflict(
        code=code,
        message=message,
        op_no=recall_no,
        sample_id=sample_id,
    )


# ---------------------------------------------------------------- 谱系遍历

def _load_edges(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """当前全部谱系边按父样品聚合(父->子邻接表)。"""
    by_parent: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT parent_id, child_id FROM lineage_edges"
        " WHERE parent_id IS NOT NULL"
    ):
        by_parent.setdefault(row["parent_id"], []).append(row["child_id"])
    return by_parent


def _descendants(by_parent: dict[str, list[str]], start: str) -> list[str]:
    """从 start 沿 父->子 方向遍历全部后代(不含 start)。

    合并汇流时同一样品可经多条路径到达,seen 集合保证只计一次;
    谱系在写入侧已禁止成环,遍历必然终止。
    """
    seen: set[str] = set()
    order: list[str] = []
    stack = [start]
    while stack:
        node = stack.pop()
        for child in by_parent.get(node, ()):  # noqa: PERF
            if child in seen:
                continue
            seen.add(child)
            order.append(child)
            stack.append(child)
    return order


def _affected_samples(
    by_parent: dict[str, list[str]], source_sample_id: str
) -> list[str]:
    """某召回的受影响样品:源样品在前,后代按样品号排序(汇流去重)。"""
    return [source_sample_id] + sorted(_descendants(by_parent, source_sample_id))


def active_recall_map(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """样品号 -> 当前影响它的活跃召回号列表(实时沿谱系边计算)。

    核销领用等写路径在各自事务内调用本函数做受影响判定;
    无活跃召回时返回空 dict,调用方零开销短路。
    """
    rows = conn.execute(
        "SELECT recall_no, source_sample_id FROM recalls"
        " WHERE status = 'active' ORDER BY recall_no"
    ).fetchall()
    if not rows:
        return {}
    by_parent = _load_edges(conn)
    out: dict[str, list[str]] = {}
    for row in rows:
        for sid in _affected_samples(by_parent, row["source_sample_id"]):
            out.setdefault(sid, []).append(row["recall_no"])
    for recall_nos in out.values():
        recall_nos.sort()
    return out


# ---------------------------------------------------------------- 审计事件

def _log_event(
    conn: sqlite3.Connection,
    action: str,
    detail: dict,
    *,
    recall_no: Optional[str] = None,
) -> None:
    conn.execute(
        "INSERT INTO recall_events (recall_no, action, detail) VALUES (?, ?, ?)",
        (recall_no, action, json.dumps(detail, ensure_ascii=False)),
    )


# ---------------------------------------------------------------- 查询/组装

def _get_header(conn: sqlite3.Connection, recall_no: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM recalls WHERE recall_no = ?", (recall_no,)
    ).fetchone()


def _build_result(
    conn: sqlite3.Connection,
    header: sqlite3.Row,
    *,
    replayed: bool,
    by_parent: Optional[dict[str, list[str]]] = None,
) -> s.RecallResult:
    """组装召回单结果;受影响样品按当前谱系实时计算(合并路径只计一次)。"""
    if by_parent is None:
        by_parent = _load_edges(conn)
    affected = _affected_samples(by_parent, header["source_sample_id"])
    return s.RecallResult(
        recall_no=header["recall_no"],
        source_sample_id=header["source_sample_id"],
        reason=header["reason"],
        status=header["status"],
        affected_samples=affected,
        affected_count=len(affected),
        operator=header["operator"],
        note=header["note"],
        replayed=replayed,
        created_at=header["created_at"],
        closed_at=header["closed_at"],
    )


# ---------------------------------------------------------------- 开启召回

def create_recall(conn: sqlite3.Connection, req: s.RecallRequest) -> s.RecallResult:
    """单事务校验源样品存在后开启召回;同号同内容重放,异内容冲突。"""
    payload = req.model_dump_json()

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _get_header(conn, req.recall_no)
        if existing is not None:
            if existing["status"] == "closed":
                raise RecallError([
                    _conflict(
                        "recall_closed",
                        f"召回号 {req.recall_no} 已关闭,不能重复开启,"
                        "如需再次召回请使用新的召回号",
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
            _log_event(conn, "replay", result.model_dump(), recall_no=req.recall_no)
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
            "INSERT INTO recalls (recall_no, source_sample_id, reason, status,"
            " payload, operator, note) VALUES (?, ?, ?, 'active', ?, ?, ?)",
            (req.recall_no, req.source_sample_id, req.reason, payload,
             req.operator, req.note),
        )
        header = _get_header(conn, req.recall_no)
        result = _build_result(conn, header, replayed=False)
        _log_event(conn, "open", result.model_dump(), recall_no=req.recall_no)
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 关闭召回

def close_recall(
    conn: sqlite3.Connection,
    recall_no: str,
    req: Optional[s.RecallCloseRequest] = None,
) -> s.RecallResult:
    """按召回号关闭召回(状态置为 closed);重复关闭幂等。"""
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
            conn.execute(
                "UPDATE recalls SET status = 'closed',"
                " closed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
                " WHERE recall_no = ? AND status = 'active'",
                (recall_no,),
            )
            existing = _get_header(conn, recall_no)

        result = _build_result(conn, existing, replayed=replayed)
        detail = result.model_dump()
        if req is not None:
            detail["close_operator"] = req.operator
            detail["close_note"] = req.note
        _log_event(conn, "close", detail, recall_no=recall_no)
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 查询

def get_recall(conn: sqlite3.Connection, recall_no: str) -> s.RecallResult:
    """按召回号查询召回单,受影响样品实时计算。"""
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
    conn: sqlite3.Connection, status: Optional[str] = None
) -> s.RecallListResponse:
    """召回单列表(可按 active/closed 过滤),每项附实时受影响样品。"""
    if status is not None:
        rows = conn.execute(
            "SELECT * FROM recalls WHERE status = ? ORDER BY recall_no", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM recalls ORDER BY recall_no").fetchall()
    by_parent = _load_edges(conn)
    return s.RecallListResponse(recalls=[
        _build_result(conn, r, replayed=False, by_parent=by_parent) for r in rows
    ])


def get_sample_recalls(
    conn: sqlite3.Connection, sample_id: str
) -> s.SampleRecallsResponse:
    """某样品当前受哪些活跃召回影响(实时沿谱系边判定)。"""
    hits = active_recall_map(conn).get(sample_id, [])
    items: list[s.SampleRecallItem] = []
    for recall_no in hits:
        header = _get_header(conn, recall_no)
        items.append(s.SampleRecallItem(
            recall_no=header["recall_no"],
            source_sample_id=header["source_sample_id"],
            reason=header["reason"],
            created_at=header["created_at"],
        ))
    return s.SampleRecallsResponse(
        sample_id=sample_id,
        recalled=bool(items),
        recalls=items,
    )
