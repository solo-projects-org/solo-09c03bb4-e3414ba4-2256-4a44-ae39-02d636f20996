"""FastAPI 入口：市政巡检终端区域路网离线取图 API。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from . import __version__
from .database import DB_PATH, init_db
from .schemas import (
    BatchRequest,
    HealthResponse,
    ImportRequest,
    ImportResponse,
    MapResponse,
    ReceiptResponse,
)
from .service import (
    ServiceError,
    apply_batch,
    get_map,
    get_receipt,
    health,
    import_base_map,
    reset_storage,
)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="区域路网离线取图 API",
    version=__version__,
    lifespan=lifespan,
    description=(
        "固定矩形区域 + 简化 JSON 底图（平面路点、道路引用有序点号）。\n\n"
        "工作流程：\n"
        "1. `POST /import` 一次性导入底图与矩形（批号 0）；\n"
        "2. `POST /batches` 按连续批号提交路点/道路增删改，"
        "断档或缺失引用点整批拒绝，同号同内容重送幂等返回原回执；\n"
        "3. `GET /regions/{region_id}/map` 获取与当前批号严格对应的"
        "区域道路、引用路点（含区外路点）快照。"
    ),
)


@app.exception_handler(ServiceError)
async def _service_error_handler(_request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.__class__.__name__, "message": exc.message},
    )


@app.get("/health", response_model=HealthResponse, tags=["运维"], summary="健康检查")
def health_endpoint() -> dict:
    return health()


@app.post(
    "/import",
    response_model=ImportResponse,
    tags=["数据导入"],
    summary="导入固定矩形与简化 JSON 底图",
    status_code=201,
)
def import_endpoint(
    req: ImportRequest,
    force: bool = Query(default=False, description="危险：清空库后重新导入"),
) -> dict:
    if force:
        reset_storage()
    return import_base_map(req)


@app.post(
    "/batches",
    response_model=ReceiptResponse,
    tags=["增量更新"],
    summary="按连续批号提交路点/道路增删改",
)
def batch_endpoint(req: BatchRequest) -> ReceiptResponse:
    return apply_batch(req)


@app.get(
    "/batches/{batch_no}/receipt",
    response_model=ReceiptResponse,
    tags=["增量更新"],
    summary="查询历史批次回执（用于重送核对）",
)
def receipt_endpoint(batch_no: int) -> ReceiptResponse:
    return get_receipt(batch_no)


@app.get(
    "/regions/{region_id}/map",
    response_model=MapResponse,
    tags=["离线取图"],
    summary="获取区域路网快照（道路 + 全部引用路点，含区外点）",
)
def map_endpoint(region_id: str) -> MapResponse:
    return get_map(region_id)


@app.get("/", tags=["运维"], summary="服务信息")
def root() -> dict:
    return {"service": "road-network-region-api", "version": __version__, "db": DB_PATH}
