"""SQLite 连接与表结构。

所有请求使用独立的短连接（WAL 模式下读写并发互不阻塞），
写操作在 service 层通过应用级写锁串行化。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

# 允许通过环境变量覆盖数据库文件位置，便于测试与容器挂载。
DB_PATH = os.environ.get("ROADMAP_DB_PATH", "/data/roadmap.db")

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
