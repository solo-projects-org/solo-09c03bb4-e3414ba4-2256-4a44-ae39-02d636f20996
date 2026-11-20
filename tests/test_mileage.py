"""里程查询与增量汇总自测。

口径：路点坐标按米、相邻点成段、无向点号对每区只计一次（反向/同路重复合并）、
只计矩形内长度（贴边也算）。汇总随批次同事务提交，查询只读总数。
"""
from __future__ import annotations

import math
import os
import sqlite3
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import database  # noqa: E402
from app.geometry import segment_clipped_length  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, "test.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    database.init_db(db_path)
    with TestClient(app) as c:
        yield c, db_path


REGION_R1 = {"region_id": "R1", "xmin": 0, "ymin": 0, "xmax": 10, "ymax": 10}


def _import(c, regions=None, waypoints=None, roads=None):
    payload = {
        "regions": regions or [REGION_R1],
        "waypoints": waypoints or [],
        "roads": roads or [],
    }
    r = c.post("/import", json=payload)
    assert r.status_code == 201, r.text
    return payload


def _mileage(c, region="R1"):
    r = c.get(f"/regions/{region}/mileage")
    assert r.status_code == 200, r.text
    return r.json()


def _expected(*pairs):
    return sum(segment_clipped_length(a, b, (0, 0, 10, 10)) for a, b in pairs)


# ---------------------------------------------------------------------------
# 基础口径
# ---------------------------------------------------------------------------


def test_straight_inside_segment(client):
    c, _ = client
    _import(
        c,
        waypoints=[{"id": 1, "x": 2, "y": 2}, {"id": 2, "x": 8, "y": 2}],
        roads=[{"id": 1, "point_ids": [1, 2]}],
    )
    body = _mileage(c)
    assert body["region_id"] == "R1"
    assert body["batch_no"] == 0
    assert body["total_m"] == pytest.approx(6.0)


def test_only_length_inside_rectangle_is_counted(client):
    c, _ = client
    # 从 (-10,5) 到 (30,5)：只有 x∈[0,10] 的 10 米在区内。
    _import(
        c,
        waypoints=[{"id": 1, "x": -10, "y": 5}, {"id": 2, "x": 30, "y": 5}],
        roads=[{"id": 1, "point_ids": [1, 2]}],
    )
    assert _mileage(c)["total_m"] == pytest.approx(10.0)


def test_diagonal_clipping(client):
    c, _ = client
    # 对角线从 (-5,-5) 到 (15,15)，区内部分 (0,0)-(10,10)，长 10*sqrt(2)。
    _import(
        c,
        waypoints=[{"id": 1, "x": -5, "y": -5}, {"id": 2, "x": 15, "y": 15}],
        roads=[{"id": 1, "point_ids": [1, 2]}],
    )
    assert _mileage(c)["total_m"] == pytest.approx(10 * math.sqrt(2))


def test_segment_along_boundary_counts_in_full(client):
    c, _ = client
    # 沿顶边 y=10 从 x=-5 到 x=15：区内部分 (0,10)-(10,10)，长 10。
    _import(
        c,
        waypoints=[{"id": 1, "x": -5, "y": 10}, {"id": 2, "x": 15, "y": 10}],
        roads=[{"id": 1, "point_ids": [1, 2]}],
    )
    assert _mileage(c)["total_m"] == pytest.approx(10.0)


def test_segment_fully_outside_region_contributes_zero(client):
    c, _ = client
    _import(
        c,
        waypoints=[{"id": 1, "x": 20, "y": 20}, {"id": 2, "x": 30, "y": 30}],
        roads=[{"id": 1, "point_ids": [1, 2]}],
    )
    assert _mileage(c)["total_m"] == pytest.approx(0.0)


def test_line_with_several_segments_partially_inside(client):
    c, _ = client
    # (-2,5) -> (5,5) -> (5,15) -> (20,15)
    # 区内：(-2,5)-(5,5) 的 5 米； (5,5)-(5,15) 的 5 米；第三段在区外。
    _import(
        c,
        waypoints=[
            {"id": 1, "x": -2, "y": 5},
            {"id": 2, "x": 5, "y": 5},
            {"id": 3, "x": 5, "y": 15},
            {"id": 4, "x": 20, "y": 15},
        ],
        roads=[{"id": 1, "point_ids": [1, 2, 3, 4]}],
    )
    assert _mileage(c)["total_m"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# 去重规则
# ---------------------------------------------------------------------------


def test_reverse_reference_is_counted_once(client):
    c, _ = client
    _import(
        c,
        waypoints=[{"id": 1, "x": 2, "y": 2}, {"id": 2, "x": 8, "y": 2}],
        roads=[
            {"id": 1, "point_ids": [1, 2]},
            {"id": 2, "point_ids": [2, 1]},  # 反向引用同一线段
        ],
    )
    assert _mileage(c)["total_m"] == pytest.approx(6.0)


def test_repeated_reference_within_same_road_merged(client):
    c, _ = client
    # 同一对点在一路内重复与反向出现，只算一次。
    _import(
        c,
        waypoints=[{"id": 1, "x": 2, "y": 2}, {"id": 2, "x": 8, "y": 2}],
        roads=[{"id": 1, "point_ids": [1, 2, 1, 2, 1]}],
    )
    assert _mileage(c)["total_m"] == pytest.approx(6.0)


def test_shared_segment_across_roads_counted_once(client):
    c, _ = client
    _import(
        c,
        waypoints=[
            {"id": 1, "x": 2, "y": 2},
            {"id": 2, "x": 8, "y": 2},
            {"id": 3, "x": 2, "y": 8},
        ],
        roads=[
            {"id": 1, "point_ids": [1, 2]},
            {"id": 2, "point_ids": [1, 2]},
            {"id": 3, "point_ids": [3, 1, 2]},
        ],
    )
    # 线段 {1,2} 计一次 6 米，线段 {1,3} 计 6 米。
    assert _mileage(c)["total_m"] == pytest.approx(12.0)


def test_same_point_pair_in_two_regions_counted_per_region(client):
    c, _ = client
    _import(
        c,
        regions=[
            {"region_id": "L", "xmin": 0, "ymin": 0, "xmax": 5, "ymax": 10},
            {"region_id": "R", "xmin": 5, "ymin": 0, "xmax": 10, "ymax": 10},
        ],
        waypoints=[{"id": 1, "x": 0, "y": 5}, {"id": 2, "x": 10, "y": 5}],
        roads=[{"id": 1, "point_ids": [1, 2]}],
    )
    assert _mileage(c, "L")["total_m"] == pytest.approx(5.0)
    assert _mileage(c, "R")["total_m"] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# 增量与汇总
# ---------------------------------------------------------------------------


def test_mileage_batch_no_matches_latest_batch(client):
    c, _ = client
    _import(
        c,
        waypoints=[
            {"id": 1, "x": 2, "y": 2},
            {"id": 2, "x": 8, "y": 2},
            {"id": 3, "x": 2, "y": 8},
        ],
        roads=[{"id": 1, "point_ids": [1, 2]}],
    )
    r = c.post(
        "/batches",
        json={
            "batch_no": 1,
            "waypoint_ops": [],
            "road_ops": [{"op": "upsert", "id": 2, "point_ids": [1, 2, 3]}],
        },
    )
    assert r.status_code == 200, r.text
    body = _mileage(c)
    assert body["batch_no"] == 1
    # (1,2) 水平 6 米 + (2,3) 斜线 hypot(6,6)
    assert body["total_m"] == pytest.approx(6.0 + math.hypot(6, 6))


def test_waypoint_move_updates_mileage_even_if_road_list_unchanged(client):
    c, _ = client
    _import(
        c,
        waypoints=[{"id": 1, "x": 2, "y": 2}, {"id": 2, "x": 8, "y": 2}],
        roads=[{"id": 1, "point_ids": [1, 2]}],
    )
    assert _mileage(c)["total_m"] == pytest.approx(6.0)

    before = c.get("/regions/R1/map").json()
    r = c.post(
        "/batches",
        json={
            "batch_no": 1,
            "waypoint_ops": [{"op": "upsert", "id": 1, "x": 2, "y": 6}],
            "road_ops": [],
        },
    )
    assert r.status_code == 200
    after = c.get("/regions/R1/map").json()
    # 区域道路名单未变（路线与成员都未变）
    assert [rd["id"] for rd in before["roads"]] == [rd["id"] for rd in after["roads"]]
    assert after["roads"][0]["point_ids"] == before["roads"][0]["point_ids"]
    # 长度按新坐标： (2,6)-(8,2) = sqrt(36+16)
    assert _mileage(c)["total_m"] == pytest.approx(math.hypot(6, 4))
    assert _mileage(c)["batch_no"] == 1


def test_waypoint_move_changes_only_recompute_incrementally(client):
    c, db_path = client
    _import(
        c,
        waypoints=[
            {"id": 1, "x": 2, "y": 2},
            {"id": 2, "x": 8, "y": 2},
            {"id": 3, "x": 2, "y": 8},
            {"id": 4, "x": 8, "y": 8},
        ],
        roads=[
            {"id": 1, "point_ids": [1, 2]},
            {"id": 2, "point_ids": [3, 4]},
        ],
    )
    conn = sqlite3.connect(db_path)
    before = {
        (row[0], row[1]): row[2]
        for row in conn.execute(
            "SELECT low_id, high_id, batch_no FROM segment_measure"
        )
    }
    conn.close()

    c.post(
        "/batches",
        json={
            "batch_no": 1,
            "waypoint_ops": [{"op": "upsert", "id": 1, "x": 2, "y": 4}],
            "road_ops": [],
        },
    )
    conn = sqlite3.connect(db_path)
    after = {
        (row[0], row[1]): row[2]
        for row in conn.execute(
            "SELECT low_id, high_id, batch_no FROM segment_measure"
        )
    }
    conn.close()
    assert set(before) == set(after) == {(1, 2), (3, 4)}
    # 只有包含点 1 的线段被重算；另一条道路的线段批号保持 0。
    assert after[(1, 2)] == 1
    assert after[(3, 4)] == 0


def test_deleting_road_keeps_shared_segment_referenced_by_other_road(client):
    c, _ = client
    _import(
        c,
        waypoints=[
            {"id": 1, "x": 2, "y": 2},
            {"id": 2, "x": 8, "y": 2},
            {"id": 3, "x": 8, "y": 8},
        ],
        roads=[
            {"id": 1, "point_ids": [1, 2]},
            {"id": 2, "point_ids": [1, 2, 3]},
        ],
    )
    assert _mileage(c)["total_m"] == pytest.approx(6.0 + 6.0)

    r = c.post(
        "/batches",
        json={
            "batch_no": 1,
            "waypoint_ops": [],
            "road_ops": [{"op": "delete", "id": 1}],
        },
    )
    assert r.status_code == 200, r.text
    # 道路 1 删除后，线段 {1,2} 仍被道路 2 引用 → 继续计入 6 米。
    body = _mileage(c)
    assert body["batch_no"] == 1
    assert body["total_m"] == pytest.approx(12.0)


def test_deleting_last_reference_removes_segment_length(client):
    c, db_path = client
    _import(
        c,
        waypoints=[
            {"id": 1, "x": 2, "y": 2},
            {"id": 2, "x": 8, "y": 2},
            {"id": 3, "x": 8, "y": 8},
        ],
        roads=[
            {"id": 1, "point_ids": [1, 2]},
            {"id": 2, "point_ids": [2, 3]},
        ],
    )
    assert _mileage(c)["total_m"] == pytest.approx(12.0)

    c.post(
        "/batches",
        json={
            "batch_no": 1,
            "waypoint_ops": [],
            "road_ops": [{"op": "delete", "id": 1}],
        },
    )
    assert _mileage(c)["total_m"] == pytest.approx(6.0)
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT low_id, high_id FROM segment_measure ORDER BY low_id, high_id"
    ).fetchall()
    conn.close()
    assert rows == [(2, 3)]


def test_road_rewire_updates_mileage(client):
    c, _ = client
    _import(
        c,
        waypoints=[
            {"id": 1, "x": 2, "y": 2},
            {"id": 2, "x": 8, "y": 2},
            {"id": 3, "x": 2, "y": 8},
            {"id": 4, "x": 8, "y": 8},
            {"id": 5, "x": 50, "y": 50},
        ],
        roads=[{"id": 1, "point_ids": [1, 2]}],
    )
    assert _mileage(c)["total_m"] == pytest.approx(6.0)
    c.post(
        "/batches",
        json={
            "batch_no": 1,
            "waypoint_ops": [],
            "road_ops": [{"op": "upsert", "id": 1, "point_ids": [3, 4]}],
        },
    )
    body = _mileage(c)
    assert body["batch_no"] == 1
    assert body["total_m"] == pytest.approx(6.0)


def test_road_deleted_with_waypoints_removes_all_its_segments(client):
    c, _ = client
    _import(
        c,
        waypoints=[
            {"id": 1, "x": 2, "y": 2},
            {"id": 2, "x": 8, "y": 2},
            {"id": 3, "x": 8, "y": 8},
        ],
        roads=[{"id": 1, "point_ids": [1, 2, 3]}],
    )
    assert _mileage(c)["total_m"] == pytest.approx(12.0)
    c.post(
        "/batches",
        json={
            "batch_no": 1,
            "waypoint_ops": [],
            "road_ops": [{"op": "delete", "id": 1}],
        },
    )
    assert _mileage(c)["total_m"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 与批次/区域图的一致性
# ---------------------------------------------------------------------------


def test_rejected_batch_does_not_change_mileage(client):
    c, _ = client
    _import(
        c,
        waypoints=[{"id": 1, "x": 2, "y": 2}, {"id": 2, "x": 8, "y": 2}],
        roads=[{"id": 1, "point_ids": [1, 2]}],
    )
    before = _mileage(c)
    r = c.post(
        "/batches",
        json={
            "batch_no": 2,  # 断档
            "waypoint_ops": [{"op": "upsert", "id": 2, "x": 1, "y": 1}],
            "road_ops": [],
        },
    )
    assert r.status_code == 422
    after = _mileage(c)
    assert after == before

    # 合法批号但引用缺失 → 整批拒绝，里程不变
    r = c.post(
        "/batches",
        json={
            "batch_no": 1,
            "waypoint_ops": [],
            "road_ops": [{"op": "upsert", "id": 1, "point_ids": [1, 999]}],
        },
    )
    assert r.status_code == 422
    assert _mileage(c) == before


def test_replay_does_not_change_mileage(client):
    c, _ = client
    _import(
        c,
        waypoints=[{"id": 1, "x": 2, "y": 2}, {"id": 2, "x": 8, "y": 2}],
        roads=[{"id": 1, "point_ids": [1, 2]}],
    )
    payload = {
        "batch_no": 1,
        "waypoint_ops": [{"op": "upsert", "id": 2, "x": 8, "y": 8}],
        "road_ops": [],
    }
    assert c.post("/batches", json=payload).status_code == 200
    first = _mileage(c)
    replay = c.post("/batches", json=payload)
    assert replay.status_code == 200
    assert replay.json()["status"] == "replayed"
    assert _mileage(c) == first


def test_mileage_unknown_region_404(client):
    c, _ = client
    _import(c)
    assert c.get("/regions/NOPE/mileage").status_code == 404


def test_mileage_before_import_returns_404_for_unknown_region(client):
    c, _ = client
    assert c.get("/regions/R1/mileage").status_code == 404


def test_mileage_swagger_route_present(client):
    c, _ = client
    spec = c.get("/openapi.json").json()
    assert "/regions/{region_id}/mileage" in spec["paths"]


# ---------------------------------------------------------------------------
# 旧库免重导回填
# ---------------------------------------------------------------------------


def test_existing_database_is_backfilled_without_reimport(monkeypatch, tmp_path):
    """模拟升级前的 SQLite 底图：只有旧表、无里程汇总。"""
    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE waypoint (id INTEGER PRIMARY KEY, x REAL NOT NULL,
                               y REAL NOT NULL, batch_no INTEGER NOT NULL);
        CREATE TABLE road (id INTEGER PRIMARY KEY, name TEXT, batch_no INTEGER NOT NULL);
        CREATE TABLE road_point (
            road_id INTEGER NOT NULL, seq INTEGER NOT NULL, point_id INTEGER NOT NULL,
            PRIMARY KEY (road_id, seq));
        CREATE TABLE region (id TEXT PRIMARY KEY, xmin REAL NOT NULL, ymin REAL NOT NULL,
                             xmax REAL NOT NULL, ymax REAL NOT NULL);
        CREATE TABLE region_road (region_id TEXT NOT NULL, road_id INTEGER NOT NULL,
                                  batch_no INTEGER NOT NULL, PRIMARY KEY (region_id, road_id));
        CREATE TABLE batch (batch_no INTEGER PRIMARY KEY, payload_hash TEXT NOT NULL,
                            receipt_json TEXT NOT NULL, applied_at TEXT NOT NULL);
        """
    )
    conn.executemany(
        "INSERT INTO waypoint VALUES (?,?,?,0)",
        [(1, 2.0, 2.0), (2, 8.0, 2.0)],
    )
    conn.execute("INSERT INTO road VALUES (1, 'old', 0)")
    conn.executemany("INSERT INTO road_point VALUES (1,?,?)", [(0, 1), (1, 2)])
    conn.execute("INSERT INTO region VALUES ('R1', 0, 0, 10, 10)")
    conn.commit()
    conn.execute("INSERT INTO metadata VALUES ('initialized', '2020-01-01')")
    conn.execute("INSERT INTO metadata VALUES ('last_batch_no', '0')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(database, "DB_PATH", db_path)
    database.init_db(db_path)  # 建新表（IF NOT EXISTS），不重导底图
    with TestClient(app) as c:
        body = c.get("/regions/R1/mileage").json()
        assert body["batch_no"] == 0
        assert body["total_m"] == pytest.approx(6.0)
        # 原有区域图仍可取
        assert c.get("/regions/R1/map").json()["batch_no"] == 0

    conn = sqlite3.connect(db_path)
    assert conn.execute(
        "SELECT value FROM metadata WHERE key='measure_version'"
    ).fetchone() is not None
    conn.close()


def test_backfill_is_idempotent(monkeypatch, tmp_path):
    db_path = str(tmp_path / "legacy2.db")
    database.init_db(db_path)
    monkeypatch.setattr(database, "DB_PATH", db_path)
    with TestClient(app) as c:
        c.post(
            "/import",
            json={
                "regions": [REGION_R1],
                "waypoints": [{"id": 1, "x": 2, "y": 2}, {"id": 2, "x": 8, "y": 2}],
                "roads": [{"id": 1, "point_ids": [1, 2]}],
            },
        )
        first = c.get("/regions/R1/mileage").json()
    from app.measure import bootstrap

    bootstrap(db_path)
    bootstrap(db_path)
    with TestClient(app) as c:
        assert c.get("/regions/R1/mileage").json() == first
