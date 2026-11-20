"""请求与响应模型。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

# ---- 导入用模型 ---------------------------------------------------------


class Rectangle(BaseModel):
    region_id: str = Field(..., min_length=1, description="区域编号，唯一")
    xmin: float
    ymin: float
    xmax: float
    ymax: float

    @field_validator("region_id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("region_id 不能为空")
        return v

    def validate_bounds(self) -> "Rectangle":
        if not (self.xmin < self.xmax and self.ymin < self.ymax):
            raise ValueError("矩形要求 xmin < xmax 且 ymin < ymax")
        return self


class BaseWaypoint(BaseModel):
    id: int = Field(..., ge=0)
    x: float
    y: float


class BaseRoad(BaseModel):
    id: int = Field(..., ge=0)
    name: Optional[str] = None
    point_ids: list[int] = Field(..., min_length=1, description="有序路点编号")


class ImportRequest(BaseModel):
    regions: list[Rectangle] = Field(..., min_length=1)
    waypoints: list[BaseWaypoint]
    roads: list[BaseRoad]

    @field_validator("regions")
    @classmethod
    def _validate_rects(cls, v: list[Rectangle]) -> list[Rectangle]:
        seen: set[str] = set()
        for r in v:
            r.validate_bounds()
            if r.region_id in seen:
                raise ValueError(f"区域编号重复: {r.region_id}")
            seen.add(r.region_id)
        return v


# ---- 增量批次模型 -------------------------------------------------------

WaypointOpType = Literal["upsert", "delete"]
RoadOpType = Literal["upsert", "delete"]


class WaypointOperation(BaseModel):
    op: WaypointOpType
    id: int = Field(..., ge=0)
    x: Optional[float] = None
    y: Optional[float] = None


class RoadOperation(BaseModel):
    op: RoadOpType
    id: int = Field(..., ge=0)
    name: Optional[str] = None
    point_ids: Optional[list[int]] = Field(
        default=None, description="upsert 必填，有序路点编号"
    )


class BatchRequest(BaseModel):
    batch_no: int = Field(..., ge=1, description="连续批号，首更新批必须为 1")
    waypoint_ops: list[WaypointOperation] = Field(default_factory=list)
    road_ops: list[RoadOperation] = Field(default_factory=list)

    @field_validator("waypoint_ops")
    @classmethod
    def _wp_unique(cls, v: list[WaypointOperation]) -> list[WaypointOperation]:
        ids = [op.id for op in v]
        if len(ids) != len(set(ids)):
            raise ValueError("同批中路点编号不可重复操作")
        return v

    @field_validator("road_ops")
    @classmethod
    def _road_unique(cls, v: list[RoadOperation]) -> list[RoadOperation]:
        ids = [op.id for op in v]
        if len(ids) != len(set(ids)):
            raise ValueError("同批中道路编号不可重复操作")
        return v


# ---- 响应模型 -----------------------------------------------------------


class WaypointOut(BaseModel):
    id: int
    x: float
    y: float


class RoadOut(BaseModel):
    id: int
    name: Optional[str] = None
    point_ids: list[int]


class RectangleOut(BaseModel):
    region_id: str
    xmin: float
    ymin: float
    xmax: float
    ymax: float


class MapResponse(BaseModel):
    region_id: str
    batch_no: int
    rectangle: RectangleOut
    roads: list[RoadOut]
    referenced_waypoints: list[WaypointOut]


class ReceiptResponse(BaseModel):
    batch_no: int
    status: Literal["applied", "replayed"]
    payload_hash: str
    applied_at: str
    affected_roads: list[int]
    regions_changed: dict[str, list[int]]


class ImportResponse(BaseModel):
    status: Literal["imported"]
    batch_no: int
    regions: int
    waypoints: int
    roads: int


class HealthResponse(BaseModel):
    status: str
    initialized: bool
    last_batch_no: Optional[int] = None
