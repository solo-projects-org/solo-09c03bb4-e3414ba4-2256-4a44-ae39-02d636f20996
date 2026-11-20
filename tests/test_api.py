"""关键功能自测。

每个测试使用独立临时数据库，通过 ROADMAP_DB_PATH 注入服务层。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import database  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, "test.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    database.init_db(db_path)
    with TestClient(app) as c:
        yield c, db_path


BASE_IMPORT = {
    "regions": [
        {"region_id": "R1", "xmin": 0, "ymin": 0, "xmax": 10, "ymax": 10},
        {"region_id": "R2", "xmin": 10, "ymin": 0, "xmax": 20, "ymax": 10},
    ],
    "waypoints": [
        {"id": 1, "x": -2, "y": 5},   # R1 外
        {"id": 2, "x": 5, "y": 5},    # R1 内
        {"id": 3, "x": 9, "y": 8},    # R1 内（靠近边界）
        {"id": 4, "x": 15, "y": 5},   # R2 内
        {"id": 5, "x": 22, "y": 5},   # R2 外
        {"id": 6, "x": 5, "y": -3},   # R1 外（下）
        {"id": 7, "x": 5, "y": 12},   # R1 外（上）
        {"id": 8, "x": 30, "y": 15},  # 两区外，与 5 构成完全区外线
        {"id": 9, "x": -8, "y": -8},  # 两区外，与 1、6 构成完全区外线
    ],
    "roads": [
        {"id": 101, "name": "横贯路", "point_ids": [1, 2, 3, 4, 5]},
        {"id": 102, "name": "中心纵路", "point_ids": [6, 2, 7]},
        {"id": 103, "name": "东纵路", "point_ids": [8, 5]},
        {"id": 104, "name": "完全区外路", "point_ids": [1, 9, 6]},
    ],
}


def _import(client, payload=None, **params):
    c, _ = client
    return c.post("/import", json=payload or BASE_IMPORT, params=params)


# ---------------------------------------------------------------------------


def test_health_before_import(client):
    c, _ = client
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["initialized"] is False


def test_import_and_region_membership(client):
    r = _import(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["batch_no"] == 0
    assert body["regions"] == 2 and body["roads"] == 4

    c, _ = client
    m1 = c.get("/regions/R1/map").json()
    m2 = c.get("/regions/R2/map").json()

    # 101 横贯两区；102 穿过 R1；103 全部在 R2 下方、仅延伸到区外，不相交；
    # 104 完全在两区之外。
    assert sorted(rd["id"] for rd in m1["roads"]) == [101, 102]
    assert sorted(rd["id"] for rd in m2["roads"]) == [101]

    # 与矩形相交的道路整条纳入，区外引用点随图提供。
    r1_point_ids = {w["id"] for w in m1["referenced_waypoints"]}
    assert {1, 5}.issubset(r1_point_ids)  # 101 的区外端点
    assert {6, 7}.issubset(r1_point_ids)  # 102 的区外端点
    # 不包含未被成员道路引用的点（8 只属于未入选的 103）
    assert 8 not in r1_point_ids

    r2_point_ids = {w["id"] for w in m2["referenced_waypoints"]}
    assert {1, 2, 3} <= r2_point_ids  # 101 在 R2 外但整条纳入
    assert 8 not in r2_point_ids      # 103 未入选，其私有点不提供

    # 道路点序保持
    road101 = next(rd for rd in m1["roads"] if rd["id"] == 101)
    assert road101["point_ids"] == [1, 2, 3, 4, 5]
    assert m1["batch_no"] == 0


def test_import_rejects_bad_reference(client):
    bad = {
        "regions": BASE_IMPORT["regions"],
        "waypoints": [{"id": 1, "x": 0, "y": 0}],
        "roads": [{"id": 1, "point_ids": [1, 99]}],
    }
    r = _import(client, bad)
    assert r.status_code == 422
    assert "99" in r.json()["message"]


def test_double_import_conflicts_and_force(client):
    assert _import(client).status_code == 201
    r = _import(client)
    assert r.status_code == 409
    r = _import(client, force=True)
    assert r.status_code == 201
    c, _ = client
    assert c.get("/health").json()["last_batch_no"] == 0


def test_batch_gap_rejected(client):
    _import(client)
    c, _ = client
    r = c.post("/batches", json={"batch_no": 2, "waypoint_ops": [], "road_ops": []})
    assert r.status_code == 422
    assert "断档" in r.json()["message"]
    # 拒绝后原图仍可取，批号不变
    assert c.get("/regions/R1/map").json()["batch_no"] == 0


def test_batch_missing_reference_rejected_and_rolled_back(client):
    _import(client)
    c, db_path = client
    payload = {
        "batch_no": 1,
        "waypoint_ops": [],
        "road_ops": [{"op": "upsert", "id": 102, "point_ids": [6, 999]}],
    }
    r = c.post("/batches", json=payload)
    assert r.status_code == 422
    assert "999" in r.json()["message"]

    # 整批回滚：道路 102 内容与成员关系保持原样
    m1 = c.get("/regions/R1/map").json()
    road102 = next(rd for rd in m1["roads"] if rd["id"] == 102)
    assert road102["point_ids"] == [6, 2, 7]
    assert c.get("/health").json()["last_batch_no"] == 0

    # 数据库层确认没有任何半写入痕迹
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM batch").fetchone()[0] == 0
        assert conn.execute(
            "SELECT name FROM road WHERE id = 102"
        ).fetchone()[0] == "中心纵路"
    finally:
        conn.close()


def test_incremental_recompute_only_affected(client):
    _import(client)
    c, db_path = client

    # 批次1：新增路点 9，并把道路 102 改线到一个完全离开 R1 的位置；
    # 同时新增一条进入 R1 的支路 105。
    r = c.post(
        "/batches",
        json={
            "batch_no": 1,
            "waypoint_ops": [{"op": "upsert", "id": 20, "x": -30, "y": -30}],
            "road_ops": [
                {"op": "upsert", "id": 102, "point_ids": [6, 20]},
                {"op": "upsert", "id": 105, "point_ids": [20, 2]},
            ],
        },
    )
    assert r.status_code == 200, r.text
    receipt = r.json()
    assert receipt["status"] == "applied"
    # 受影响道路：直接改动的 102、105，加上引用被改路点（本批无既有引用点改动）
    assert sorted(receipt["affected_roads"]) == [102, 105]

    m1 = c.get("/regions/R1/map").json()
    member_ids = sorted(rd["id"] for rd in m1["roads"])
    # 102 整条离开 R1（6->9 线段不经过矩形），105 进入 R1
    assert member_ids == [101, 105]
    assert m1["batch_no"] == 1

    # 验证定位受影响道路走的是索引而不是全表扫描
    conn = sqlite3.connect(db_path)
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT DISTINCT road_id FROM road_point "
        "WHERE point_id IN (2, 9)"
    ).fetchall()
    conn.close()
    detail = " ".join(row[3] for row in plan)
    assert "SCAN road_point" not in detail, detail
    assert "SEARCH" in detail, detail


def test_waypoint_change_retouches_referencing_roads_without_full_scan(client):
    _import(client)
    c, _ = client
    # 仅移动路点 1（道路 101、104 引用它）。104 原本完全区外，
    # 移动后若与 R1 相交则应自动进入 R1 成员集——不需要提交任何道路操作。
    r = c.post(
        "/batches",
        json={
            "batch_no": 1,
            "waypoint_ops": [{"op": "upsert", "id": 1, "x": 2, "y": 2}],
            "road_ops": [],
        },
    )
    assert r.status_code == 200
    receipt = r.json()
    assert sorted(receipt["affected_roads"]) == [101, 104]
    m1 = c.get("/regions/R1/map").json()
    assert 104 in [rd["id"] for rd in m1["roads"]]
    # 路点新坐标随图提供
    wp1 = next(w for w in m1["referenced_waypoints"] if w["id"] == 1)
    assert wp1 == {"id": 1, "x": 2.0, "y": 2.0}


def test_idempotent_replay_and_content_conflict(client):
    _import(client)
    c, _ = client
    payload = {
        "batch_no": 1,
        "waypoint_ops": [{"op": "upsert", "id": 20, "x": 1, "y": 1}],
        "road_ops": [{"op": "upsert", "id": 105, "point_ids": [20, 2]}],
    }
    r1 = c.post("/batches", json=payload)
    assert r1.status_code == 200
    first = r1.json()
    assert first["status"] == "applied"

    # 同号同内容重送：返回原回执，标记 replayed
    r2 = c.post("/batches", json=payload)
    assert r2.status_code == 200
    replay = r2.json()
    assert replay["status"] == "replayed"
    assert replay["payload_hash"] == first["payload_hash"]
    assert replay["applied_at"] == first["applied_at"]

    # 同号改内容：冲突
    bad = dict(payload)
    bad["waypoint_ops"] = [{"op": "upsert", "id": 20, "x": 2, "y": 2}]
    r3 = c.post("/batches", json=bad)
    assert r3.status_code == 409
    assert "不同内容" in r3.json()["message"]

    # 回执查询接口返回同一份记录
    r4 = c.get("/batches/1/receipt")
    assert r4.status_code == 200
    assert r4.json()["payload_hash"] == first["payload_hash"]


def test_consecutive_batches_and_delete_semantics(client):
    _import(client)
    c, _ = client
    # 批次1
    r = c.post(
        "/batches",
        json={
            "batch_no": 1,
            "waypoint_ops": [{"op": "upsert", "id": 20, "x": 3, "y": 3}],
            "road_ops": [{"op": "upsert", "id": 105, "point_ids": [20, 3]}],
        },
    )
    assert r.status_code == 200

    # 批次2：删除被引用的点 8 —— 103 同批删除、104 同批改线去掉 8，应成功
    r = c.post(
        "/batches",
        json={
            "batch_no": 2,
            "waypoint_ops": [{"op": "delete", "id": 8}],
            "road_ops": [
                {"op": "delete", "id": 103},
                {"op": "upsert", "id": 104, "point_ids": [5, 4]},
            ],
        },
    )
    assert r.status_code == 200, r.text
    m2 = c.get("/regions/R2/map").json()
    assert 103 not in [rd["id"] for rd in m2["roads"]]
    assert all(w["id"] != 8 for w in m2["referenced_waypoints"])
    assert m2["batch_no"] == 2

    # 批次3：删除仍被道路引用的点（2 被 101/102/105 引用），整批拒绝
    r = c.post(
        "/batches",
        json={"batch_no": 3, "waypoint_ops": [{"op": "delete", "id": 2}], "road_ops": []},
    )
    assert r.status_code == 422
    assert c.get("/health").json()["last_batch_no"] == 2


def test_snapshot_consistency_between_roads_waypoints_and_batch(client):
    _import(client)
    c, _ = client
    for b in (1, 2, 3):
        r = c.post(
            "/batches",
            json={
                "batch_no": b,
                "waypoint_ops": [{"op": "upsert", "id": 100 + b, "x": b, "y": b}],
                "road_ops": [
                    {"op": "upsert", "id": 105, "point_ids": [100 + b, 2, 3]}
                ],
            },
        )
        assert r.status_code == 200

    m = c.get("/regions/R1/map").json()
    assert m["batch_no"] == 3
    road105 = next(rd for rd in m["roads"] if rd["id"] == 105)
    assert road105["point_ids"] == [103, 2, 3]
    wp_ids = {w["id"] for w in m["referenced_waypoints"]}
    # 快照中每个引用点都能找到对应坐标，且没有多余的点
    assert {pid for rd in m["roads"] for pid in rd["point_ids"]} == wp_ids


def test_region_not_found_and_validation(client):
    _import(client)
    c, _ = client
    assert c.get("/regions/NOPE/map").status_code == 404

    bad_rect = {
        "regions": [{"region_id": "X", "xmin": 10, "ymin": 10, "xmax": 0, "ymax": 0}],
        "waypoints": [],
        "roads": [],
    }
    assert c.post("/import?force=true", json=bad_rect).status_code == 422


def test_boundary_touch_counts_as_intersection(client):
    payload = {
        "regions": [{"region_id": "RB", "xmin": 0, "ymin": 0, "xmax": 10, "ymax": 10}],
        "waypoints": [
            {"id": 1, "x": -5, "y": 10},
            {"id": 2, "x": 15, "y": 10},
        ],
        "roads": [{"id": 1, "point_ids": [1, 2]}],  # 沿线 y=10 擦过矩形顶边
    }
    r = _import(client, payload)
    assert r.status_code == 201
    c, _ = client
    m = c.get("/regions/RB/map").json()
    assert [rd["id"] for rd in m["roads"]] == [1]
