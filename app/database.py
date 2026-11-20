"""SQLite 连接与表结构。

所有请求使用独立的短连接（WAL 模式下读写并发互不阻塞），
写操作在 service 层通过应用级写锁串行化。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

# 允许通过环境变量覆盖数据库文件位置，便于测试与容器挂载。
DB_PATH = os.environ.get("ROADMAP_DB_PATH", "/data/roadmap.db")

# 进程内写操作串行化锁（SQLite 单文件 + WAL，读不阻塞写）。
WRITE_LOCK = threading.Lock()

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS waypoint (
    id        INTEGER PRIMARY KEY,
    x         REAL NOT NULL,
    y         REAL NOT NULL,
    batch_no  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS road (
    id        INTEGER PRIMARY KEY,
    name      TEXT,
    batch_no  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS road_point (
    road_id  INTEGER NOT NULL REFERENCES road(id) ON DELETE CASCADE,
    seq      INTEGER NOT NULL,
    point_id INTEGER NOT NULL REFERENCES waypoint(id),
    PRIMARY KEY (road_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_road_point_point ON road_point(point_id);
CREATE INDEX IF NOT EXISTS idx_road_point_road  ON road_point(road_id);

CREATE TABLE IF NOT EXISTS region (
    id     TEXT PRIMARY KEY,
    xmin   REAL NOT NULL,
    ymin   REAL NOT NULL,
    xmax   REAL NOT NULL,
    ymax   REAL NOT NULL
);

-- 区域-道路关联表即“同一次提交”的成员快照：
-- 每次提交只重写本批受影响道路的成员行，取图在单个事务内读，
-- 因此区域道路、引用路点、批号必然对应同一批。
CREATE TABLE IF NOT EXISTS region_road (
    region_id TEXT NOT NULL REFERENCES region(id),
    road_id   INTEGER NOT NULL REFERENCES road(id) ON DELETE CASCADE,
    batch_no  INTEGER NOT NULL,
    PRIMARY KEY (region_id, road_id)
);
CREATE INDEX IF NOT EXISTS idx_region_road_road ON region_road(road_id);

CREATE TABLE IF NOT EXISTS batch (
    batch_no      INTEGER PRIMARY KEY,
    payload_hash  TEXT NOT NULL,
    receipt_json  TEXT NOT NULL,
    applied_at    TEXT NOT NULL
);

-- 里程增量汇总：每个 (区域, 无向点号对) 一条，值为该线段落在区域内的长度。
-- 点号对按无向规范化：low_id < high_id，反向引用/同路重复引用天然合并为同一行；
-- 主键保证多条道路共用同一线段时每区只计一次。
-- 每次提交只删除并重算“引用或端点坐标变化”的点号对，查询直接读区域总数。
CREATE TABLE IF NOT EXISTS segment_measure (
    region_id  TEXT NOT NULL REFERENCES region(id),
    low_id     INTEGER NOT NULL,
    high_id    INTEGER NOT NULL,
    length_m   REAL NOT NULL,
    batch_no   INTEGER NOT NULL,
    PRIMARY KEY (region_id, low_id, high_id)
);
-- 增量重算按点号对定位（跨区域），因此单独建点号对索引。
CREATE INDEX IF NOT EXISTS idx_segment_measure_pair
    ON segment_measure(low_id, high_id);

CREATE TABLE IF NOT EXISTS region_measure (
    region_id  TEXT PRIMARY KEY REFERENCES region(id),
    total_m    REAL NOT NULL,
    batch_no   INTEGER NOT NULL
);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """打开一个开启外键约束的连接。"""
    conn = sqlite3.connect(db_path or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def session(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str | None = None) -> None:
    """创建表结构（幂等）。"""
    with session(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()
