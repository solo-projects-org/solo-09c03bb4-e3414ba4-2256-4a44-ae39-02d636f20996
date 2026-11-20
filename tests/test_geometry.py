"""几何判定单元测试：覆盖共线、擦边、包围盒排除等退化情形。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.geometry import _segments_intersect, polyline_intersects_rect  # noqa: E402

RECT = (0.0, 0.0, 10.0, 10.0)


def test_general_crossing():
    assert _segments_intersect((-1, 5), (11, 5), (5, -1), (5, 11))
    assert polyline_intersects_rect([(-5, 5), (5, 5), (15, 5)], RECT)


def test_disjoint_segments():
    assert not _segments_intersect((0, 0), (1, 1), (5, 5), (6, 6))
    assert not polyline_intersects_rect([(20, 20), (30, 30)], RECT)


def test_collinear_but_separated_must_not_intersect():
    # 共线且互不接触：四个叉积均为 0，不能误判
    assert not _segments_intersect((0, 0), (1, 1), (5, 5), (6, 6))
    assert not polyline_intersects_rect([(-8, -8), (-2, -2)], RECT)


def test_collinear_touching_endpoint():
    assert _segments_intersect((0, 0), (5, 5), (5, 5), (10, 10))


def test_parallel_separate():
    assert not _segments_intersect((0, 1), (10, 1), (0, 9), (10, 9))


def test_polyline_along_rectangle_edge_counts_as_intersection():
    # 沿矩形顶边 y=10 擦过：边界接触即纳入
    assert polyline_intersects_rect([(-5, 10), (15, 10)], RECT)


def test_polyline_corner_touch():
    assert polyline_intersects_rect([(-5, -5), (0, 0)], RECT)  # 端点即角点
    assert polyline_intersects_rect([(-5, 5), (0, 5)], RECT)   # 端点落在左边上


def test_polyline_fully_inside():
    assert polyline_intersects_rect([(2, 2), (8, 8)], RECT)


def test_bbox_quick_reject():
    # 整体在矩形右上方，包围盒不相交
    assert not polyline_intersects_rect([(11, 11), (20, 20)], RECT)


def test_empty_polyline():
    assert not polyline_intersects_rect([], RECT)
