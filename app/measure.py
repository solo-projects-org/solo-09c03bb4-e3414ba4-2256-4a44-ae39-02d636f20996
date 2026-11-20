"""按区域汇总的管养里程。

里程口径
========
* 路点 ``x/y`` 直接以米为单位，线段长度为欧氏距离；
* 相邻路点构成线段；无向点号对 ``(low, high)`` 为计量单元，
  同路内重复引用、反向引用（A→B 与 B→A）天然合并；
* 每个区域内同一无向点号对只计一次——多条道路共用同一线段不会重复累计；
* 只累计线段落在矩形内的部分，Liang-Barsky 裁剪把贴边部分也算作区内。

增量策略
========
``segment_measure`` 保存 (区域, 无向点号对) → 区内长度，
``region_measure`` 保存区域总米数。每次提交只对“引用变化或端点坐标变化”的
点号对删除重算，并从明细 SUM 刷新相关区域总数；查询里程只读总数，不重新量算。
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Iterable, Optional

from .database import WRITE_LOCK, session
from .geometry import Point, Rect, segment_clipped_length

logger = logging.getLogger(__name__)

Pair = tuple[int, int]

MEASURE_VERSION = "1"

# 旧库一次性回填与批次写互斥（跨进程由 BEGIN IMMEDIATE 串行）。
_bootstrap_lock = WRITE_LOCK


def normalize_pair(a: int, b: int) -> Pair:
    """无向点号对：反向引用得到同一个键。"""
    return (a, b) if a <= b else (b, a)


def road_pairs(
    conn: sqlite3.Connection, road_ids: Iterable[int]
) -> dict[int, set[Pair]]:
    """读取若干道路的有序相邻点号对（无向，同路内重复自动合并）。"""
    ids = sorted(set(road_ids))
    result: dict[int, set[Pair]] = {rid: set() for rid in ids}
    if not ids:
        return result
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT road_id, point_id FROM road_point "
        f"WHERE road_id IN ({placeholders}) ORDER BY road_id, seq",
        tuple(ids),
    ).fetchall()
    prev_road: Optional[int] = None
    prev_point: Optional[int] = None
    for row in rows:
        road_id = row["road_id"]
        point_id = row["point_id"]
        if road_id != prev_road:
            prev_road = road_id
            prev_point = None
        if prev_point is not None and prev_point != point_id:
            result[road_id].add(normalize_pair(prev_point, point_id))
        prev_point = point_id
    return result


def collect_affected_pairs(
    old_pairs: dict[int, set[Pair]],
    new_pairs: dict[int, set[Pair]],
    road_op_ids: set[int],
    changed_point_ids: set[int],
    affected_road_ids: set[int],
) -> set[Pair]:
    """只挑出本批需要重算的点号对。

    * 道路操作（新增/改线/删除）：该道路新旧拓扑的**对称差**，
      即只有引用关系真正变化的线段才重算；
    * 路点移动/删除：受影响道路中包含该点的点号对，
      即便区域道路名单未变（路线未改）也要重算长度。
    """
    pairs: set[Pair] = set()
    for road_id in road_op_ids:
        pairs |= old_pairs.get(road_id, set()) ^ new_pairs.get(road_id, set())
    if changed_point_ids:
        for road_id in affected_road_ids:
            for pair in old_pairs.get(road_id, set()) | new_pairs.get(road_id, set()):
                if pair[0] in changed_point_ids or pair[1] in changed_point_ids:
                    pairs.add(pair)
    return {p for p in pairs if p[0] != p[1]}


def _load_point_coords(
    conn: sqlite3.Connection, point_ids: set[int]
) -> dict[int, Point]:
    if not point_ids:
        return {}
    placeholders = ",".join("?" for _ in point_ids)
    rows = conn.execute(
        f"SELECT id, x, y FROM waypoint WHERE id IN ({placeholders})",
        tuple(point_ids),
    ).fetchall()
    return {r["id"]: (r["x"], r["y"]) for r in rows}


def _pair_still_referenced(conn: sqlite3.Connection, low: int, high: int) -> bool:
    """是否仍有任意道路把这两点作为相邻点引用（双向都算）。

    某条道路被删除后，只要还有别的道路引用同一线段，它应继续计入。
    """
    sql = """
        SELECT 1
          FROM road_point a
          JOIN road_point b
            ON b.road_id = a.road_id AND b.seq = a.seq + 1
         WHERE a.point_id = ? AND b.point_id = ?
         LIMIT 1
    """
    if conn.execute(sql, (low, high)).fetchone() is not None:
        return True
    return conn.execute(sql, (high, low)).fetchone() is not None


def _regions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM region ORDER BY id").fetchall())


def _rect(region: sqlite3.Row) -> Rect:
    return (region["xmin"], region["ymin"], region["xmax"], region["ymax"])


def _upsert_region_total(
    conn: sqlite3.Connection, region_id: str, total_m: float, batch_no: int
) -> None:
    conn.execute(
        """
        INSERT INTO region_measure(region_id, total_m, batch_no) VALUES (?, ?, ?)
        ON CONFLICT(region_id) DO UPDATE SET total_m = excluded.total_m,
                                            batch_no = excluded.batch_no
        """,
        (region_id, total_m, batch_no),
    )


def _refresh_region_total(
    conn: sqlite3.Connection, region_id: str, batch_no: int
) -> None:
    """从明细 SUM 回算区域总数，避免长期累加的浮点漂移。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(length_m), 0.0) AS total "
        "FROM segment_measure WHERE region_id = ?",
        (region_id,),
    ).fetchone()
    _upsert_region_total(conn, region_id, row["total"], batch_no)


def _load_existing(
    conn: sqlite3.Connection, pairs: set[Pair]
) -> list[sqlite3.Row]:
    """读取给定点号对已有的里程明细行（分块查询，避免 SQL 过长）。"""
    rows: list[sqlite3.Row] = []
    ordered = sorted(pairs)
    chunk_size = 200
    for start in range(0, len(ordered), chunk_size):
        chunk = ordered[start : start + chunk_size]
        conditions = " OR ".join("(low_id = ? AND high_id = ?)" for _ in chunk)
        params: list[int] = []
        for low, high in chunk:
            params.extend((low, high))
        rows.extend(
            conn.execute(
                f"SELECT region_id, low_id, high_id, length_m FROM segment_measure "
                f"WHERE {conditions}",
                tuple(params),
            ).fetchall()
        )
    return rows


def rebuild_all(conn: sqlite3.Connection, batch_no: int) -> None:
    """全量重建汇总（底图导入、旧库一次性回填）。

    需在调用方的事务内执行。
    """
    conn.execute("DELETE FROM segment_measure")
    conn.execute("DELETE FROM region_measure")

    rows = conn.execute(
        "SELECT road_id, point_id FROM road_point ORDER BY road_id, seq"
    ).fetchall()
    pairs: set[Pair] = set()
    prev_road: Optional[int] = None
    prev_point: Optional[int] = None
    for row in rows:
        road_id = row["road_id"]
        point_id = row["point_id"]
        if road_id != prev_road:
            prev_road = road_id
            prev_point = None
        if prev_point is not None and prev_point != point_id:
            pairs.add(normalize_pair(prev_point, point_id))
        prev_point = point_id

    coords = _load_point_coords(conn, {pid for pair in pairs for pid in pair})
    for region in _regions(conn):
        rect = _rect(region)
        total = 0.0
        for low, high in sorted(pairs):
            a = coords.get(low)
            b = coords.get(high)
            if a is None or b is None:
                continue
            length = segment_clipped_length(a, b, rect)
            if length <= 0:
                continue
            conn.execute(
                "INSERT INTO segment_measure"
                "(region_id, low_id, high_id, length_m, batch_no) "
                "VALUES (?, ?, ?, ?, ?)",
                (region["id"], low, high, length, batch_no),
            )
            total += length
        _upsert_region_total(conn, region["id"], total, batch_no)


def recompute_pairs(
    conn: sqlite3.Connection, affected_pairs: set[Pair], batch_no: int
) -> None:
    """只重算给定点号对的区内长度，并刷新相关区域的总米数。

    需在调用方的事务内执行。先删后插，因此可安全处理“道路被删但线段
    仍被其他道路引用”的情形（still_referenced 为真时长度保留）。
    """
    if not affected_pairs:
        return
    regions = _regions(conn)
    if not regions:
        return
    coords = _load_point_coords(
        conn, {pid for pair in affected_pairs for pid in pair}
    )
    touched_regions: set[str] = set()

    old_rows = {
        (row["low_id"], row["high_id"], row["region_id"]): row["length_m"]
        for row in _load_existing(conn, affected_pairs)
    }

    for low, high in sorted(affected_pairs):
        a = coords.get(low)
        b = coords.get(high)
        # 点被删除，或已经没有任何道路引用这条线段 → 各区域长度归零。
        still_referenced = (
            a is not None
            and b is not None
            and _pair_still_referenced(conn, low, high)
        )
        for region in regions:
            rect = _rect(region)
            old_length = old_rows.get((low, high, region["id"]), 0.0)
            new_length = (
                segment_clipped_length(a, b, rect) if still_referenced else 0.0
            )
            if new_length <= 0 and old_length == 0.0:
                continue
            if (low, high, region["id"]) in old_rows:
                conn.execute(
                    "DELETE FROM segment_measure "
                    "WHERE region_id = ? AND low_id = ? AND high_id = ?",
                    (region["id"], low, high),
                )
            if new_length > 0:
                conn.execute(
                    "INSERT INTO segment_measure"
                    "(region_id, low_id, high_id, length_m, batch_no) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (region["id"], low, high, new_length, batch_no),
                )
            if new_length != old_length:
                touched_regions.add(region["id"])

    # 从明细 SUM 回算总数，避免长期累加误差；未触及的区域不重算。
    for region_id in sorted(touched_regions):
        _refresh_region_total(conn, region_id, batch_no)


def stamp_batch(conn: sqlite3.Connection, batch_no: int) -> None:
    """让每条区域汇总都带上当前数据批号（总米数可能未变）。"""
    conn.execute("UPDATE region_measure SET batch_no = ?", (batch_no,))


def total_for_region(conn: sqlite3.Connection, region_id: str) -> Optional[float]:
    row = conn.execute(
        "SELECT total_m FROM region_measure WHERE region_id = ?", (region_id,)
    ).fetchone()
    return row["total_m"] if row is not None else None


def measurements_ready(conn: sqlite3.Connection) -> bool:
    """汇总是否已建立且覆盖现有区域。"""
    version = conn.execute(
        "SELECT value FROM metadata WHERE key = 'measure_version'"
    ).fetchone()
    if version is None or version["value"] != MEASURE_VERSION:
        return False
    missing = conn.execute(
        """
        SELECT 1 FROM region r
         WHERE NOT EXISTS (
               SELECT 1 FROM region_measure m WHERE m.region_id = r.id)
         LIMIT 1
        """
    ).fetchone()
    return missing is None


def ensure_measurements(conn: sqlite3.Connection, batch_no: int) -> None:
    """若当前库尚未建立里程汇总（旧版本底图），就地全量回填一次。

    需在调用方的事务内执行。``metadata.measure_version`` 是完成标记。
    """
    if measurements_ready(conn):
        return
    rebuild_all(conn, batch_no)
    conn.execute(
        "INSERT INTO metadata(key, value) VALUES ('measure_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (MEASURE_VERSION,),
    )


def bootstrap(db_path: Optional[str] = None) -> None:
    """启动时调用：已有底图的旧库补齐里程汇总，无需重新导入。"""
    with _bootstrap_lock, session(db_path) as conn:
        initialized = conn.execute(
            "SELECT value FROM metadata WHERE key = 'initialized'"
        ).fetchone()
        if initialized is None:
            return
        if measurements_ready(conn):
            return
        last = conn.execute(
            "SELECT value FROM metadata WHERE key = 'last_batch_no'"
        ).fetchone()
        batch_no = int(last["value"]) if last is not None else 0
        try:
            conn.execute("BEGIN IMMEDIATE")
            # 抢到写锁后重查，避免多进程重复回填。
            ensure_measurements(conn, batch_no)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        logger.info("已为旧库建立里程汇总（当前批号 %s）", batch_no)


def ensure_ready_committed(db_path: Optional[str] = None) -> None:
    """请求路径上的兜底回填（独立写事务，避免与读快照混用）。"""
    with _bootstrap_lock, session(db_path) as conn:
        initialized = conn.execute(
            "SELECT value FROM metadata WHERE key = 'initialized'"
        ).fetchone()
        if initialized is None or measurements_ready(conn):
            return
        last = conn.execute(
            "SELECT value FROM metadata WHERE key = 'last_batch_no'"
        ).fetchone()
        batch_no = int(last["value"]) if last is not None else 0
        try:
            conn.execute("BEGIN IMMEDIATE")
            ensure_measurements(conn, batch_no)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
