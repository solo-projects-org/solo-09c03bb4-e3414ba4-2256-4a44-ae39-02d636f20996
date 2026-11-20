"""平面几何判定：矩形与折线是否相交（含边界接触）。"""
from __future__ import annotations

from typing import Sequence

Point = tuple[float, float]
Rect = tuple[float, float, float, float]  # xmin, ymin, xmax, ymax


def _segments_intersect(p1: Point, p2: Point, p3: Point, p4: Point) -> bool:
    """线段 p1p2 与 p3p4 是否相交（含端点落在另一线段上）。"""

    def cross(a: Point, b: Point, c: Point) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def on_segment(a: Point, b: Point, c: Point) -> bool:
        return (
            cross(a, b, c) == 0
            and min(a[0], b[0]) <= c[0] <= max(a[0], b[0])
            and min(a[1], b[1]) <= c[1] <= max(a[1], b[1])
        )

    d1 = cross(p3, p4, p1)
    d2 = cross(p3, p4, p2)
    d3 = cross(p1, p2, p3)
    d4 = cross(p1, p2, p4)
    if ((d1 > 0 > d2) or (d1 < 0 < d2)) and ((d3 > 0 > d4) or (d3 < 0 < d4)):
        return True
    # 退化情形：仅当端点真正落在另一线段上才算相交；
    # 共线但互不接触的线段四个叉积均为 0，必须排除。
    if d1 == 0 and on_segment(p3, p4, p1):
        return True
    if d2 == 0 and on_segment(p3, p4, p2):
        return True
    if d3 == 0 and on_segment(p1, p2, p3):
        return True
    if d4 == 0 and on_segment(p1, p2, p4):
        return True
    return False


def _point_in_rect(p: Point, rect: Rect) -> bool:
    xmin, ymin, xmax, ymax = rect
    return xmin <= p[0] <= xmax and ymin <= p[1] <= ymax


def polyline_intersects_rect(points: Sequence[Point], rect: Rect) -> bool:
    """折线（有序路点）与轴对齐矩形是否相交。

    规则：任一路点落在矩形内/边界上，或任一线段与矩形四条边相交，
    即判为相交——整条道路纳入区域。
    """
    if not points:
        return False
    xmin, ymin, xmax, ymax = rect
    if any(_point_in_rect(p, rect) for p in points):
        return True
    # 包围盒快速排除
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    if max(xs) < xmin or min(xs) > xmax or max(ys) < ymin or min(ys) > ymax:
        return False
    corners = [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)]
    rect_edges = [(corners[i], corners[(i + 1) % 4]) for i in range(4)]
    for a, b in zip(points, points[1:]):
        for c, d in rect_edges:
            if _segments_intersect(a, b, c, d):
                return True
    return False
