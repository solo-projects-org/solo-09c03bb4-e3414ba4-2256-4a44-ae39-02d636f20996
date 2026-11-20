"""业务逻辑层：底图导入、连续批号增量更新、区域取图与里程汇总。

设计要点
========
* 所有写操作在应用级锁内串行，单事务提交；任何一步失败即回滚，
  旧版本数据保持可读（“更新失败后仍可取原图”）。
* 增量更新只重判两类道路：
    1. 本批 road_ops 直接改动的道路；
    2. 引用了本批改动路点的道路——通过 ``road_point(point_id)`` 索引
       按点号反查，不扫描全图。
* 引用完整性由外键 + 提交前针对“受影响道路集合”的缺失点检查双重保证，
  任一道路缺少引用点则整批拒绝。
* 区域成员关系（region_road）与里程汇总（segment_measure / region_measure）
  随每次提交增量重写，取图/查里程在只读事务内完成，与批号严格同快照。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from .database import WRITE_LOCK, session
from .geometry import polyline_intersects_rect
from .measure import (
    MEASURE_VERSION,
    collect_affected_pairs,
    ensure_measurements,
    ensure_ready_committed,
    rebuild_all,
    recompute_pairs,
    road_pairs,
    stamp_batch,
    total_for_region,
)
from .schemas import (
    BatchRequest,
    ImportRequest,
    MapResponse,
    MileageResponse,
    ReceiptResponse,
    RoadOut,
    WaypointOut,
)

# 写操作串行化锁（SQLite 单文件 + WAL，读不阻塞写）。
_write_lock = WRITE_LOCK

INITIAL_BATCH = 0


class ServiceError(Exception):
    """业务错误基类，HTTP 状态码由子类决定。"""

    status_code = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ConflictError(ServiceError):
    status_code = 409


class NotFoundError(ServiceError):
    status_code = 404


class UnprocessableError(ServiceError):
    status_code = 422


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def canonical_hash(payload: dict[str, Any]) -> str:
    """对请求体做规范化 JSON 后取 SHA-256，用于幂等/冲突判定。"""
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _last_batch_no(conn: sqlite3.Connection) -> Optional[int]:
    value = _get_meta(conn, "last_batch_no")
    return int(value) if value is not None else None


def _load_regions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    # 矩形为固定且数量很少的配置数据，直接全量读取，不涉及路图扫描。
    return list(conn.execute("SELECT * FROM region ORDER BY id").fetchall())


def _road_points(conn: sqlite3.Connection, road_id: int) -> list[tuple[float, float]]:
    rows = conn.execute(
        """
        SELECT w.x AS x, w.y AS y
          FROM road_point rp
          JOIN waypoint w ON w.id = rp.point_id
         WHERE rp.road_id = ?
         ORDER BY rp.seq
        """,
        (road_id,),
    ).fetchall()
    return [(r["x"], r["y"]) for r in rows]


def _recompute_membership(
    conn: sqlite3.Connection, affected_road_ids: set[int], batch_no: int
) -> dict[str, list[int]]:
    """仅对受影响道路重判区域成员关系，返回各区域变化后的成员增量结果。"""
    regions = _load_regions(conn)
    changed: dict[str, list[int]] = {r["id"]: [] for r in regions}

    for road_id in sorted(affected_road_ids):
        # 先清除该道路在所有区域的旧成员行（道路若被删，其成员行随之清空）。
        conn.execute("DELETE FROM region_road WHERE road_id = ?", (road_id,))
        points = _road_points(conn, road_id)
        if not points:
            continue  # 道路已删除：保持无成员行
        for region in regions:
            rect = (region["xmin"], region["ymin"], region["xmax"], region["ymax"])
            if polyline_intersects_rect(points, rect):
                conn.execute(
                    "INSERT INTO region_road(region_id, road_id, batch_no) VALUES (?, ?, ?)",
                    (region["id"], road_id, batch_no),
                )
                changed[region["id"]].append(road_id)
    return changed


# ---------------------------------------------------------------------------
# 底图导入
# ---------------------------------------------------------------------------


def import_base_map(req: ImportRequest, db_path: Optional[str] = None) -> dict[str, Any]:
    with _write_lock, session(db_path) as conn:
        if _get_meta(conn, "initialized") is not None:
            raise ConflictError("底图已导入：如需重置请使用全新数据库或 /import?force=true")

        waypoints = req.waypoints
        roads = req.roads
        wp_ids = [w.id for w in waypoints]
        road_ids = [r.id for r in roads]
        if len(wp_ids) != len(set(wp_ids)):
            raise UnprocessableError("底图路点编号存在重复")
        if len(road_ids) != len(set(road_ids)):
            raise UnprocessableError("底图道路编号存在重复")

        wp_set = set(wp_ids)
        for road in roads:
            missing = [pid for pid in road.point_ids if pid not in wp_set]
            if missing:
                raise UnprocessableError(
                    f"道路 {road.id} 引用了不存在的路点: {missing}"
                )

        try:
            conn.execute("BEGIN")
            for region in req.regions:
                conn.execute(
                    "INSERT INTO region(id, xmin, ymin, xmax, ymax) VALUES (?, ?, ?, ?, ?)",
                    (region.region_id, region.xmin, region.ymin, region.xmax, region.ymax),
                )
            for wp in waypoints:
                conn.execute(
                    "INSERT INTO waypoint(id, x, y, batch_no) VALUES (?, ?, ?, ?)",
                    (wp.id, wp.x, wp.y, INITIAL_BATCH),
                )
            for road in roads:
                conn.execute(
                    "INSERT INTO road(id, name, batch_no) VALUES (?, ?, ?)",
                    (road.id, road.name, INITIAL_BATCH),
                )
                for seq, pid in enumerate(road.point_ids):
                    conn.execute(
                        "INSERT INTO road_point(road_id, seq, point_id) VALUES (?, ?, ?)",
                        (road.id, seq, pid),
                    )

            # 首次导入：全量计算一次区域成员与里程汇总。
            _recompute_membership(conn, set(road_ids), INITIAL_BATCH)
            rebuild_all(conn, INITIAL_BATCH)
            conn.execute(
                "INSERT INTO metadata(key, value) VALUES ('measure_version', ?)",
                (MEASURE_VERSION,),
            )

            conn.execute(
                "INSERT INTO metadata(key, value) VALUES ('last_batch_no', ?)",
                (str(INITIAL_BATCH),),
            )
            conn.execute(
                "INSERT INTO metadata(key, value) VALUES ('initialized', ?)",
                (_now(),),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise UnprocessableError(f"导入数据违反完整性约束: {exc}") from exc

    return {
        "status": "imported",
        "batch_no": INITIAL_BATCH,
        "regions": len(req.regions),
        "waypoints": len(waypoints),
        "roads": len(roads),
    }


def reset_storage(db_path: Optional[str] = None) -> None:
    """force 重置：清空全部业务表（仅允许在显式 force=true 时调用）。"""
    with _write_lock, session(db_path) as conn:
        conn.execute("BEGIN")
        for table in (
            "segment_measure",
            "region_measure",
            "region_road",
            "road_point",
            "road",
            "waypoint",
            "region",
            "batch",
            "metadata",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()


# ---------------------------------------------------------------------------
# 增量批次
# ---------------------------------------------------------------------------


def _locate_affected_roads(
    conn: sqlite3.Connection,
    road_op_ids: set[int],
    changed_point_ids: set[int],
) -> set[int]:
    """定位本批受影响道路，全程走索引，不扫描全图。"""
    affected = set(road_op_ids)
    if changed_point_ids:
        placeholders = ",".join("?" for _ in changed_point_ids)
        rows = conn.execute(
            f"SELECT DISTINCT road_id FROM road_point WHERE point_id IN ({placeholders})",
            tuple(changed_point_ids),
        ).fetchall()
        affected.update(r["road_id"] for r in rows)
    return affected


def apply_batch(req: BatchRequest, db_path: Optional[str] = None) -> ReceiptResponse:
    payload_hash = canonical_hash(req.model_dump(mode="json"))

    with _write_lock, session(db_path) as conn:
        if _get_meta(conn, "initialized") is None:
            raise ConflictError("尚未导入底图，请先调用 /import")

        last = _last_batch_no(conn)
        assert last is not None

        existing = conn.execute(
            "SELECT payload_hash, receipt_json FROM batch WHERE batch_no = ?",
            (req.batch_no,),
        ).fetchone()

        # 幂等 / 冲突判定（批号不大于当前最大批号即视为重送）。
        if existing is not None or req.batch_no <= last:
            if existing is None:
                raise ConflictError(
                    f"批号 {req.batch_no} 已占用且无回执记录，当前最新批号 {last}"
                )
            if existing["payload_hash"] != payload_hash:
                raise ConflictError(
                    f"批号 {req.batch_no} 已应用过不同内容，拒绝覆盖"
                )
            replayed = ReceiptResponse.model_validate_json(existing["receipt_json"])
            # 库存回执保留首次应用记录，仅在响应上标记为重放。
            return replayed.model_copy(update={"status": "replayed"})

        # 批号断档：必须正好是 last + 1。
        if req.batch_no != last + 1:
            raise UnprocessableError(
                f"批号断档：期望 {last + 1}，收到 {req.batch_no}"
            )

        # ---- 静态校验 --------------------------------------------------
        wp_ops = {op.id: op for op in req.waypoint_ops}
        road_ops = {op.id: op for op in req.road_ops}
        current_points = {
            r["id"] for r in conn.execute("SELECT id FROM waypoint").fetchall()
        }
        current_roads = {
            r["id"] for r in conn.execute("SELECT id FROM road").fetchall()
        }

        # 应用路点操作后的点集合（先 upsert 后 delete）。
        end_state_points = set(current_points)
        for op in req.waypoint_ops:
            if op.op == "upsert":
                if op.x is None or op.y is None:
                    raise UnprocessableError(f"路点 {op.id} upsert 必须提供 x、y")
                end_state_points.add(op.id)
            else:
                end_state_points.discard(op.id)

        for op in req.waypoint_ops:
            if op.op == "delete" and op.id not in current_points:
                raise UnprocessableError(f"待删除路点 {op.id} 不存在")

        for op in req.road_ops:
            if op.op == "upsert":
                if not op.point_ids:
                    raise UnprocessableError(f"道路 {op.id} upsert 必须提供非空 point_ids")
                missing = [pid for pid in op.point_ids if pid not in end_state_points]
                if missing:
                    raise UnprocessableError(
                        f"道路 {op.id} 引用了批后不存在的路点: {missing}"
                    )
            elif op.id not in current_roads:
                raise UnprocessableError(f"待删除道路 {op.id} 不存在")

        # 删除路点前，确认批后没有道路引用它（含本批未改的既有道路）。
        deleted_points = {op.id for op in req.waypoint_ops if op.op == "delete"}
        if deleted_points:
            placeholders = ",".join("?" for _ in deleted_points)
            refs = conn.execute(
                f"SELECT DISTINCT road_id, point_id FROM road_point "
                f"WHERE point_id IN ({placeholders})",
                tuple(deleted_points),
            ).fetchall()
            # 本批被整体改写（upsert/delete）的道路，其旧引用会被替换或级联清除，
            # 其余道路若仍引用被删点，则整批拒绝。
            blocked = [
                (r["road_id"], r["point_id"])
                for r in refs
                if r["road_id"] not in road_ops
            ]
            if blocked:
                raise UnprocessableError(
                    f"路点仍被道路引用，无法删除: {sorted(set(p for _, p in blocked))}"
                )

        # ---- 定位受影响道路（索引反查，不扫全图） ----------------------
        affected = _locate_affected_roads(
            conn, set(road_ops.keys()), set(wp_ops.keys())
        )

        # 仅“坐标确实变化或删除”的点才需要重算其线段长度；
        # 同坐标 upsert 只更新批号，不产生无效量算。
        changed_point_ids: set[int] = set()
        for op in req.waypoint_ops:
            if op.op == "delete":
                changed_point_ids.add(op.id)
                continue
            row = conn.execute(
                "SELECT x, y FROM waypoint WHERE id = ?", (op.id,)
            ).fetchone()
            if row is None or row["x"] != op.x or row["y"] != op.y:
                changed_point_ids.add(op.id)

        try:
            conn.execute("BEGIN")
            # 旧库首次写入前先补建汇总（一次全量），之后同事务增量维护。
            ensure_measurements(conn, last)
            # 旧拓扑（读取在事务内，与后续写入同一快照）。
            old_pairs = road_pairs(conn, affected)
            old_pairs.update(road_pairs(conn, road_ops.keys()))

            # ---- 应用路点增删改 ----------------------------------------
            for op in req.waypoint_ops:
                if op.op == "upsert":
                    conn.execute(
                        """
                        INSERT INTO waypoint(id, x, y, batch_no) VALUES (?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET x = excluded.x,
                                                      y = excluded.y,
                                                      batch_no = excluded.batch_no
                        """,
                        (op.id, op.x, op.y, req.batch_no),
                    )
                else:
                    conn.execute(
                        "DELETE FROM road_point WHERE point_id = ?", (op.id,)
                    )
                    conn.execute("DELETE FROM waypoint WHERE id = ?", (op.id,))

            # ---- 应用道路增删改 ----------------------------------------
            for op in req.road_ops:
                if op.op == "delete":
                    # road_point / region_road 由 ON DELETE CASCADE 连带删除。
                    conn.execute("DELETE FROM road WHERE id = ?", (op.id,))
                    continue
                if op.id in current_roads:
                    conn.execute(
                        "UPDATE road SET name = ?, batch_no = ? WHERE id = ?",
                        (op.name, req.batch_no, op.id),
                    )
                else:
                    conn.execute(
                        "INSERT INTO road(id, name, batch_no) VALUES (?, ?, ?)",
                        (op.id, op.name, req.batch_no),
                    )
                conn.execute("DELETE FROM road_point WHERE road_id = ?", (op.id,))
                for seq, pid in enumerate(op.point_ids or []):
                    conn.execute(
                        "INSERT INTO road_point(road_id, seq, point_id) VALUES (?, ?, ?)",
                        (op.id, seq, pid),
                    )

            # ---- 提交前完整性断言：仅限受影响道路，不扫全图 -------------
            if affected:
                placeholders = ",".join("?" for _ in affected)
                broken = conn.execute(
                    f"""
                    SELECT DISTINCT rp.road_id, rp.point_id
                      FROM road_point rp
                      LEFT JOIN waypoint w ON w.id = rp.point_id
                     WHERE rp.road_id IN ({placeholders})
                       AND w.id IS NULL
                    """,
                    tuple(affected),
                ).fetchall()
                if broken:
                    raise UnprocessableError(
                        "存在缺少引用点的道路: "
                        + ", ".join(f"道路{r['road_id']}->点{r['point_id']}" for r in broken)
                    )

            # ---- 增量重判区域成员 --------------------------------------
            regions_changed = _recompute_membership(conn, affected, req.batch_no)

            # ---- 增量重算里程：只碰引用/坐标变化的线段 -------------------
            new_pairs = road_pairs(conn, affected)
            new_pairs.update(road_pairs(conn, road_ops.keys()))
            recompute_pairs(
                conn,
                collect_affected_pairs(
                    old_pairs,
                    new_pairs,
                    set(road_ops.keys()),
                    changed_point_ids,
                    affected,
                ),
                req.batch_no,
            )
            stamp_batch(conn, req.batch_no)

            receipt = ReceiptResponse(
                batch_no=req.batch_no,
                status="applied",
                payload_hash=payload_hash,
                applied_at=_now(),
                affected_roads=sorted(affected),
                regions_changed={k: v for k, v in regions_changed.items() if v},
            )
            conn.execute(
                "INSERT INTO batch(batch_no, payload_hash, receipt_json, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (req.batch_no, payload_hash, receipt.model_dump_json(), receipt.applied_at),
            )
            conn.execute(
                "UPDATE metadata SET value = ? WHERE key = 'last_batch_no'",
                (str(req.batch_no),),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise UnprocessableError(f"批次违反完整性约束，已整批拒绝: {exc}") from exc
        except ServiceError:
            conn.rollback()
            raise
        except Exception as exc:  # pragma: no cover - 防御性回滚
            conn.rollback()
            raise UnprocessableError(f"批次应用失败，已整批拒绝: {exc}") from exc

    return receipt


# ---------------------------------------------------------------------------
# 取图 / 查询
# ---------------------------------------------------------------------------


def get_map(region_id: str, db_path: Optional[str] = None) -> MapResponse:
    with session(db_path) as conn:
        # 单连接内的只读事务提供一致性快照。
        conn.execute("BEGIN")
        region = conn.execute(
            "SELECT * FROM region WHERE id = ?", (region_id,)
        ).fetchone()
        if region is None:
            conn.rollback()
            raise NotFoundError(f"区域不存在: {region_id}")

        batch_no = _last_batch_no(conn)
        if batch_no is None:
            conn.rollback()
            raise ConflictError("尚未导入底图")

        member_rows = conn.execute(
            "SELECT road_id FROM region_road WHERE region_id = ? ORDER BY road_id",
            (region_id,),
        ).fetchall()
        road_ids = [r["road_id"] for r in member_rows]

        roads: list[RoadOut] = []
        referenced_point_ids: set[int] = set()
        for road_id in road_ids:
            road = conn.execute(
                "SELECT id, name FROM road WHERE id = ?", (road_id,)
            ).fetchone()
            if road is None:
                # 理论上不会发生（删除走级联），防御性跳过并告警式处理。
                continue
            point_rows = conn.execute(
                "SELECT point_id FROM road_point WHERE road_id = ? ORDER BY seq",
                (road_id,),
            ).fetchall()
            point_ids = [r["point_id"] for r in point_rows]
            referenced_point_ids.update(point_ids)
            roads.append(RoadOut(id=road["id"], name=road["name"], point_ids=point_ids))

        waypoints: list[WaypointOut] = []
        if referenced_point_ids:
            placeholders = ",".join("?" for _ in referenced_point_ids)
            wp_rows = conn.execute(
                f"SELECT id, x, y FROM waypoint WHERE id IN ({placeholders}) ORDER BY id",
                tuple(referenced_point_ids),
            ).fetchall()
            waypoints = [
                WaypointOut(id=r["id"], x=r["x"], y=r["y"]) for r in wp_rows
            ]
        conn.commit()

    return MapResponse(
        region_id=region_id,
        batch_no=batch_no,
        rectangle={
            "region_id": region["id"],
            "xmin": region["xmin"],
            "ymin": region["ymin"],
            "xmax": region["xmax"],
            "ymax": region["ymax"],
        },
        roads=roads,
        referenced_waypoints=waypoints,
    )


def get_mileage(region_id: str, db_path: Optional[str] = None) -> MileageResponse:
    """读取区域管养里程汇总（直接读总数，不重新量算）。"""
    with session(db_path) as conn:
        region = conn.execute(
            "SELECT id FROM region WHERE id = ?", (region_id,)
        ).fetchone()
        if region is None:
            raise NotFoundError(f"区域不存在: {region_id}")
        batch_no = _last_batch_no(conn)
        if batch_no is None:
            raise ConflictError("尚未导入底图")

    # 旧库可能尚未回填（例如升级后未重启服务）：在独立写事务内补建一次。
    ensure_ready_committed(db_path)

    with session(db_path) as conn:
        conn.execute("BEGIN")
        total = total_for_region(conn, region_id)
        batch_no = _last_batch_no(conn)
        conn.commit()

    if total is None:
        raise NotFoundError(f"区域不存在: {region_id}")
    assert batch_no is not None
    return MileageResponse(
        region_id=region_id,
        batch_no=batch_no,
        total_m=total,
    )


def get_receipt(batch_no: int, db_path: Optional[str] = None) -> ReceiptResponse:
    with session(db_path) as conn:
        row = conn.execute(
            "SELECT receipt_json FROM batch WHERE batch_no = ?", (batch_no,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"批号 {batch_no} 无回执（批号 0 为初始导入，无回执）")
        return ReceiptResponse.model_validate_json(row["receipt_json"])


def health(db_path: Optional[str] = None) -> dict[str, Any]:
    with session(db_path) as conn:
        initialized = _get_meta(conn, "initialized") is not None
        return {
            "status": "ok",
            "initialized": initialized,
            "last_batch_no": _last_batch_no(conn),
        }
