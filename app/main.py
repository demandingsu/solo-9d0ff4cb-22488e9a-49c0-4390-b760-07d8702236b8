"""园区公共用电逐小时净电量 API。

- POST /api/hierarchy            上传整版电表层级 CSV（上级表, 下级表），整版替换
- POST /api/readings             按批补传累计电量 CSV（表号, UTC整点, kWh读数）
- GET  /api/meters/{id}/daily-net?date=YYYY-MM-DD   查询某 UTC 日逐小时净电量
- POST /api/rewire/check         核查：某分表从原总表改挂到目标根总表的模拟结果
- POST /api/rewire/{id}/confirm  确认改接：只调整这一条父子关系
- GET  /api/rewire/suggestions   查看核查建议（含已过期）
- GET  /api/hierarchy            查看当前已生效层级（便于演示核对）
- GET  /api/batches              查看批次审计（含被拒批次及原因）
- GET  /api/health

一致性约定：每个请求一个事务；校验全部通过后才落库，任一步失败即回滚，
已提交的旧层级/旧读数/旧查询结果不受影响。确认改接在 BEGIN IMMEDIATE 内
重新读取当前层级与读数，与核查时的依据逐项比对，不一致即判为过期，
绝不按旧依据写入。
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field

from . import core, db, rewire
from .parsing import CsvError, parse_hierarchy_csv, parse_readings_csv

DEFAULT_DB_PATH = os.environ.get(
    "ENERGY_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "energy.db")
)

# 过期原因里使用的字段名
_FP_LABELS = {
    "from_children": "原总表直属表集合",
    "to_children": "目标总表直属表集合",
    "readings": "参与表读数",
}


# ---------- 响应模型（让 /docs 自描述） ----------

class BatchResult(BaseModel):
    batch_id: str
    status: str
    kind: str
    rows_applied: int
    message: str


class HourResult(BaseModel):
    hour_start: str
    status: str  # "ok" | "pending_review"
    net_kwh: Optional[float] = None
    master_delta_kwh: Optional[float] = None
    children_delta_kwh: Optional[float] = None
    review_reason: Optional[str] = None


class DailyNetResult(BaseModel):
    meter_id: str
    date: str
    child_meters: list[str]
    hours: list[HourResult]


class HierarchyEdge(BaseModel):
    parent_meter: str
    child_meter: str


class BatchRecord(BaseModel):
    batch_id: str
    kind: str
    status: str
    reason: Optional[str]
    row_count: int
    created_at: str


class RewireHour(BaseModel):
    hour_start: str
    child_delta_kwh: Optional[float] = None
    from_net_before_kwh: Optional[float] = None
    from_net_after_kwh: Optional[float] = None
    to_net_before_kwh: Optional[float] = None
    to_net_after_kwh: Optional[float] = None


class RewireCheckRequest(BaseModel):
    child_meter: str
    from_parent: Optional[str] = Field(
        default=None,
        description="原直属总表；留空则取当前层级中的直属总表，传了则必须与当前层级一致",
    )
    to_parent: str = Field(description="目标根总表；不能是分表自身或其后代")
    date: date


class RewireCheckResult(BaseModel):
    suggestion_id: Optional[str] = Field(
        default=None, description="仅在 confirmable=true 时生成"
    )
    status: str  # "confirmable" | "not_confirmable"
    child_meter: str
    from_parent: str
    to_parent: str
    date: str
    from_children: list[str]
    to_children: list[str]
    from_children_after: list[str]
    to_children_after: list[str]
    before_from_hours_ok: bool
    before_to_hours_ok: bool
    after_from_hours_ok: bool
    after_to_hours_ok: bool
    before_from_negative_hours: list[str]
    before_to_negative_hours: list[str]
    after_from_negative_hours: list[str]
    after_to_negative_hours: list[str]
    missing: list[str]
    backwards: list[str]
    reasons: list[str]
    hours: list[RewireHour]


class RewireConfirmResult(BaseModel):
    suggestion_id: str
    status: str  # "confirmed" | "expired"
    child_meter: str
    from_parent: str
    to_parent: str
    message: str
    hierarchy: list["HierarchyEdge"]


class RewireSuggestionRecord(BaseModel):
    suggestion_id: str
    status: str
    child_meter: str
    from_parent: str
    to_parent: str
    date: str
    checks: dict
    created_at: str
    confirmed_at: Optional[str] = None


# ---------- 应用工厂 ----------

def create_app(db_path: Optional[str] = None) -> FastAPI:
    app = FastAPI(
        title="园区公共用电逐小时净电量 API",
        description=(
            "上传整版电表层级与按批补传累计电量，查询指定总表某 UTC 日的逐小时净电量。"
            "每小时 = 总表相邻整点差值 - 直接下级表差值之和（孙表不再扣）。"
            "另提供改接核查：模拟某分表从原总表改挂到目标根总表后的逐小时净值，"
            "仅在两边全天齐全、无读数倒退、原总表有负净值且改接后两边均无负值时"
            "生成建议；确认时只改这一条父子关系，依据变化则建议过期。"
        ),
        version="1.1.0",
    )
    app.state.db_path = os.path.abspath(db_path or DEFAULT_DB_PATH)
    db.init_db(app.state.db_path)

    def get_conn(request: Request):
        conn = db.connect(request.app.state.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _record_rejected(conn, kind: str, reason: str) -> None:
        """拒收批次留痕。独立短事务，不影响主数据。"""
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO batches (batch_id, kind, status, reason, row_count, created_at)"
                " VALUES (?, ?, 'rejected', ?, 0, ?)",
                (str(uuid.uuid4()), kind, reason, _utcnow()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")

    # ---------- 上传：整版层级 ----------

    @app.post("/api/hierarchy", response_model=BatchResult, summary="上传整版电表层级 CSV（整版替换）")
    async def upload_hierarchy(file: UploadFile = File(...), conn=Depends(get_conn)):
        text = await _read_csv_text(file)
        try:
            edges = parse_hierarchy_csv(text)  # 纯函数校验：成环/多上级/自环在此拒收
        except CsvError as exc:
            _record_rejected(conn, "hierarchy", str(exc))
            raise HTTPException(status_code=422, detail=str(exc))

        batch_id = str(uuid.uuid4())
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM hierarchy")  # 整版替换
            conn.executemany(
                "INSERT INTO hierarchy (child_meter, parent_meter) VALUES (?, ?)",
                [(child, parent) for parent, child in edges],
            )
            conn.execute(
                "INSERT INTO batches (batch_id, kind, status, reason, row_count, created_at)"
                " VALUES (?, 'hierarchy', 'applied', NULL, ?, ?)",
                (batch_id, len(edges), _utcnow()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return BatchResult(
            batch_id=batch_id, status="applied", kind="hierarchy",
            rows_applied=len(edges),
            message=f"层级已整版替换，生效 {len(edges)} 条上下级关系",
        )

    # ---------- 上传：一批读数 ----------

    @app.post("/api/readings", response_model=BatchResult, summary="按批补传累计电量 CSV")
    async def upload_readings(file: UploadFile = File(...), conn=Depends(get_conn)):
        text = await _read_csv_text(file)
        try:
            rows = parse_readings_csv(text)  # 批内同键异值在此整批拒收
        except CsvError as exc:
            _record_rejected(conn, "readings", str(exc))
            raise HTTPException(status_code=422, detail=str(exc))

        now = _utcnow()
        batch_id = str(uuid.uuid4())
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 同键异值 = 更正（覆盖）；同值重传 = 幂等（WHERE 条件使其不变）
            conn.executemany(
                """
                INSERT INTO readings (meter_id, ts_utc, kwh, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (meter_id, ts_utc) DO UPDATE SET
                    kwh = excluded.kwh, updated_at = excluded.updated_at
                WHERE readings.kwh <> excluded.kwh
                """,
                [(m, ts, kwh, now) for m, ts, kwh in rows],
            )
            conn.execute(
                "INSERT INTO batches (batch_id, kind, status, reason, row_count, created_at)"
                " VALUES (?, 'readings', 'applied', NULL, ?, ?)",
                (batch_id, len(rows), now),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return BatchResult(
            batch_id=batch_id, status="applied", kind="readings",
            rows_applied=len(rows),
            message=f"读数批次已提交，{len(rows)} 个（表号, 整点）键已写入/更正",
        )

    # ---------- 查询：逐小时净电量 ----------

    @app.get(
        "/api/meters/{meter_id}/daily-net",
        response_model=DailyNetResult,
        summary="查询指定总表某 UTC 日的逐小时净电量",
    )
    def daily_net(
        meter_id: str,
        date_: str = Query(..., alias="date", description="UTC 日期，格式 YYYY-MM-DD"),
        conn=Depends(get_conn),
    ):
        try:
            day = date.fromisoformat(date_)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"日期格式应为 YYYY-MM-DD: {date_!r}")

        # 单事务快照：层级与读数取自同一已提交状态
        conn.execute("BEGIN")
        try:
            children = [
                r["child_meter"]
                for r in conn.execute(
                    "SELECT child_meter FROM hierarchy WHERE parent_meter = ?"
                    " ORDER BY child_meter",
                    (meter_id,),
                )
            ]
            participants = [meter_id, *children]
            placeholders = ",".join("?" for _ in participants)
            t_start, t_end = core.hour_str(day, 0), core.hour_str(day, 24)
            rows = conn.execute(
                f"SELECT meter_id, ts_utc, kwh FROM readings"
                f" WHERE meter_id IN ({placeholders}) AND ts_utc BETWEEN ? AND ?",
                (*participants, t_start, t_end),
            ).fetchall()
            readings = {(r["meter_id"], r["ts_utc"]): r["kwh"] for r in rows}
            hours = core.compute_daily_net(meter_id, day, children, readings)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        return DailyNetResult(
            meter_id=meter_id, date=day.isoformat(), child_meters=children, hours=hours
        )

    # ---------- 辅助端点 ----------

    @app.get("/api/hierarchy", response_model=list[HierarchyEdge], summary="查看当前已生效层级")
    def get_hierarchy(conn=Depends(get_conn)):
        conn.execute("BEGIN")
        try:
            rows = conn.execute(
                "SELECT parent_meter, child_meter FROM hierarchy"
                " ORDER BY parent_meter, child_meter"
            ).fetchall()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return [HierarchyEdge(**dict(r)) for r in rows]

    @app.get("/api/batches", response_model=list[BatchRecord], summary="批次审计（含被拒批次）")
    def list_batches(conn=Depends(get_conn)):
        conn.execute("BEGIN")
        try:
            rows = conn.execute(
                "SELECT * FROM batches ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return [BatchRecord(**dict(r)) for r in rows]

    # ---------- 改接核查 ----------

    def _children(conn, parent: str) -> list[str]:
        return [
            r["child_meter"]
            for r in conn.execute(
                "SELECT child_meter FROM hierarchy WHERE parent_meter = ? ORDER BY child_meter",
                (parent,),
            )
        ]

    def _parent_of(conn, child: str) -> Optional[str]:
        row = conn.execute(
            "SELECT parent_meter FROM hierarchy WHERE child_meter = ?", (child,)
        ).fetchone()
        return row["parent_meter"] if row else None

    def _descendants(conn, meter: str) -> set[str]:
        seen: set[str] = set()
        stack = [meter]
        while stack:
            node = stack.pop()
            for c in _children(conn, node):
                if c not in seen:
                    seen.add(c)
                    stack.append(c)
        return seen

    def _read_day(conn, meters: list[str], day: date) -> dict[tuple[str, str], float]:
        """在调用方事务内读取原子表全天 25 个整点（含次日 00:00）。"""
        if not meters:
            return {}
        placeholders = ",".join("?" for _ in meters)
        rows = conn.execute(
            f"SELECT meter_id, ts_utc, kwh FROM readings"
            f" WHERE meter_id IN ({placeholders}) AND ts_utc BETWEEN ? AND ?",
            (*meters, core.hour_str(day, 0), core.hour_str(day, 24)),
        ).fetchall()
        return {(r["meter_id"], r["ts_utc"]): r["kwh"] for r in rows}

    def _fingerprint(
        from_parent: str,
        to_parent: str,
        from_children: list[str],
        to_children: list[str],
        participants: list[str],
        readings: dict[tuple[str, str], float],
    ) -> dict:
        """核查依据：两边直属表集合 + 参与表全天读数。顺序无关，便于直接比较。"""
        return {
            "from_parent": from_parent,
            "to_parent": to_parent,
            "from_children": sorted(from_children),
            "to_children": sorted(to_children),
            "readings": sorted(
                [meter, ts, kwh]
                for (meter, ts), kwh in readings.items()
                if meter in participants
            ),
        }

    def _check_and_persist(conn, child: str, from_parent: str, to_parent: str, day: date) -> dict:
        """假定已在写事务内；返回核查结论，可确认时落一条 pending 建议。"""
        from_children = _children(conn, from_parent)
        to_children = _children(conn, to_parent)
        participants = sorted({from_parent, to_parent, child, *from_children, *to_children})
        readings = _read_day(conn, participants, day)

        result = rewire.simulate_rewire(
            child, from_parent, to_parent, from_children, to_children, readings, day
        )
        fingerprint = _fingerprint(
            from_parent, to_parent, from_children, to_children, participants, readings
        )

        suggestion_id = None
        if result["confirmable"]:
            fingerprint_json = json.dumps(fingerprint, ensure_ascii=False, sort_keys=True)
            # 同一依据重复核查：复用仍在 pending 的建议，避免重复堆积
            existing = conn.execute(
                "SELECT suggestion_id FROM rewire_suggestions"
                " WHERE status = 'pending' AND child_meter = ? AND from_parent = ?"
                "   AND to_parent = ? AND day = ? AND fingerprint_json = ?"
                " ORDER BY created_at LIMIT 1",
                (child, from_parent, to_parent, day.isoformat(), fingerprint_json),
            ).fetchone()
            if existing is not None:
                result["suggestion_id"] = existing["suggestion_id"]
                return result

            suggestion_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO rewire_suggestions"
                " (suggestion_id, child_meter, from_parent, to_parent, day, status,"
                "  fingerprint_json, checks_json, created_at, confirmed_at)"
                " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, NULL)",
                (
                    suggestion_id, child, from_parent, to_parent, day.isoformat(),
                    fingerprint_json,
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    _utcnow(),
                ),
            )
            conn.execute(
                "INSERT INTO batches (batch_id, kind, status, reason, row_count, created_at)"
                " VALUES (?, 'rewire', 'applied', ?, 0, ?)",
                (
                    str(uuid.uuid4()),
                    f"核查通过，生成待确认建议 {suggestion_id}: {child} "
                    f"{from_parent} -> {to_parent} @ {day.isoformat()}",
                    _utcnow(),
                ),
            )
        else:
            conn.execute(
                "INSERT INTO batches (batch_id, kind, status, reason, row_count, created_at)"
                " VALUES (?, 'rewire', 'rejected', ?, 0, ?)",
                (
                    str(uuid.uuid4()),
                    f"核查不通过，未生成建议: {child} {from_parent} -> {to_parent}"
                    f" @ {day.isoformat()}: " + "；".join(result["reasons"]),
                    _utcnow(),
                ),
            )
        result["suggestion_id"] = suggestion_id
        return result

    @app.post(
        "/api/rewire/check",
        response_model=RewireCheckResult,
        summary="核查分表改接：用现有读数模拟改挂后两边的逐小时净值",
    )
    def check_rewire(payload: RewireCheckRequest, conn=Depends(get_conn)):
        child = payload.child_meter.strip()
        from_parent = (payload.from_parent or "").strip()
        to_parent = payload.to_parent.strip()
        day = payload.date
        if not child or not to_parent:
            raise HTTPException(status_code=422, detail="分表与目标总表不能为空")
        if child == to_parent:
            raise HTTPException(status_code=422, detail="目标根总表不能是分表自身")

        conn.execute("BEGIN IMMEDIATE")
        try:
            actual_parent = _parent_of(conn, child)
            if actual_parent is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"层级中不存在分表 {child!r} 的父子关系，无法核查改接",
                )
            if from_parent and from_parent != actual_parent:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"分表 {child!r} 当前直属总表是 {actual_parent!r}，"
                        f"与核查请求的原总表 {from_parent!r} 不一致"
                    ),
                )
            from_parent = actual_parent
            if from_parent == to_parent:
                raise HTTPException(
                    status_code=422, detail="原总表与目标总表相同，无需改接"
                )
            if to_parent in _descendants(conn, child):
                raise HTTPException(
                    status_code=422,
                    detail=f"目标根总表 {to_parent!r} 是分表 {child!r} 的后代，改接会成环",
                )
            result = _check_and_persist(conn, child, from_parent, to_parent, day)
            conn.execute("COMMIT")
        except HTTPException:
            conn.execute("ROLLBACK")
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise

        return {
            **result,
            "status": "confirmable" if result["confirmable"] else "not_confirmable",
        }

    # ---------- 改接确认 ----------

    @app.post(
        "/api/rewire/{suggestion_id}/confirm",
        response_model=RewireConfirmResult,
        summary="确认改接：只调整这一条父子关系（依据不一致则过期）",
    )
    def confirm_rewire(suggestion_id: str, response: Response, conn=Depends(get_conn)):
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM rewire_suggestions WHERE suggestion_id = ?",
                (suggestion_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="核查建议不存在")

            child = row["child_meter"]
            from_parent = row["from_parent"]
            to_parent = row["to_parent"]
            day = date.fromisoformat(row["day"])

            def expire(reason: str):
                conn.execute(
                    "UPDATE rewire_suggestions SET status = 'expired'"
                    " WHERE suggestion_id = ? AND status = 'pending'",
                    (suggestion_id,),
                )
                conn.execute(
                    "INSERT INTO batches (batch_id, kind, status, reason, row_count, created_at)"
                    " VALUES (?, 'rewire', 'expired', ?, 0, ?)",
                    (
                        str(uuid.uuid4()),
                        f"建议 {suggestion_id} 已过期，未改接: {reason}",
                        _utcnow(),
                    ),
                )
                conn.execute("COMMIT")
                response.status_code = 409
                return RewireConfirmResult(
                    suggestion_id=suggestion_id, status="expired",
                    child_meter=child, from_parent=from_parent, to_parent=to_parent,
                    message=reason, hierarchy=_hierarchy(conn),
                )

            if row["status"] != "pending":
                raise HTTPException(
                    status_code=409,
                    detail=f"建议状态为 {row['status']!r}，只能确认 pending 的建议",
                )

            # 在写事务内重新读取当前层级与读数，与核查依据逐项比对
            actual_parent = _parent_of(conn, child)
            if actual_parent != from_parent:
                return expire(
                    f"当前层级中分表 {child!r} 的直属总表是 {actual_parent!r}，"
                    f"不再是核查时的 {from_parent!r}"
                )
            from_children = _children(conn, from_parent)
            to_children = _children(conn, to_parent)
            participants = sorted({from_parent, to_parent, child, *from_children, *to_children})
            readings = _read_day(conn, participants, day)
            current = _fingerprint(
                from_parent, to_parent, from_children, to_children, participants, readings
            )
            recorded = json.loads(row["fingerprint_json"])
            if current != recorded:
                changed = [
                    name for name in ("from_children", "to_children", "readings")
                    if current[name] != recorded[name]
                ]
                return expire(
                    "核查依据已变化（" + "、".join(_FP_LABELS[c] for c in changed)
                    + "），建议不再适用"
                )
            # 只调整这一条父子关系；其余边原样保留。
            # 条件更新兜底：并发确认时只有一方能改状态，另一方必定失败回滚。
            now = _utcnow()
            claimed = conn.execute(
                "UPDATE rewire_suggestions SET status = 'confirmed', confirmed_at = ?"
                " WHERE suggestion_id = ? AND status = 'pending'",
                (now, suggestion_id),
            ).rowcount
            if claimed != 1:
                raise HTTPException(status_code=409, detail="建议已被其他请求处理")
            conn.execute(
                "UPDATE hierarchy SET parent_meter = ? WHERE child_meter = ?",
                (to_parent, child),
            )
            conn.execute(
                "INSERT INTO batches (batch_id, kind, status, reason, row_count, created_at)"
                " VALUES (?, 'rewire', 'applied', ?, 1, ?)",
                (
                    str(uuid.uuid4()),
                    f"建议 {suggestion_id} 已确认，分表 {child} 由 {from_parent} 改挂到"
                    f" {to_parent} @ {day.isoformat()}（历史日期按新层级重算）",
                    now,
                ),
            )
            hierarchy = _hierarchy(conn)
            conn.execute("COMMIT")
        except HTTPException:
            conn.execute("ROLLBACK")
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise

        return RewireConfirmResult(
            suggestion_id=suggestion_id, status="confirmed",
            child_meter=child, from_parent=from_parent, to_parent=to_parent,
            message=f"已确认：分表 {child} 从 {from_parent} 改挂到 {to_parent}",
            hierarchy=hierarchy,
        )

    def _hierarchy(conn) -> list[HierarchyEdge]:
        rows = conn.execute(
            "SELECT parent_meter, child_meter FROM hierarchy ORDER BY parent_meter, child_meter"
        ).fetchall()
        return [HierarchyEdge(**dict(r)) for r in rows]

    @app.get(
        "/api/rewire/suggestions",
        response_model=list[RewireSuggestionRecord],
        summary="查看改接核查建议（含已确认/已过期）",
    )
    def list_rewire_suggestions(conn=Depends(get_conn)):
        conn.execute("BEGIN")
        try:
            rows = conn.execute(
                "SELECT * FROM rewire_suggestions ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return [
            RewireSuggestionRecord(
                suggestion_id=r["suggestion_id"],
                status=r["status"],
                child_meter=r["child_meter"],
                from_parent=r["from_parent"],
                to_parent=r["to_parent"],
                date=r["day"],
                checks=json.loads(r["checks_json"]),
                created_at=r["created_at"],
                confirmed_at=r["confirmed_at"],
            )
            for r in rows
        ]

    @app.get("/api/health", summary="健康检查")
    def health():
        return {"status": "ok"}

    return app


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _read_csv_text(file: UploadFile) -> str:
    raw = await file.read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="CSV 必须是 UTF-8 编码")


app = create_app()
