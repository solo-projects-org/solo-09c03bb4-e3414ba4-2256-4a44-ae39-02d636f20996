# 区域路网离线取图 API

面向市政巡检终端的区域路网服务：导入固定矩形与简化 JSON 底图（平面路点坐标、
道路按有序点号引用路点），以连续批号接收路点/道路的增删改，并为每个矩形区域
输出可离线使用的路网快照（区域道路 + 全部引用路点，含区外路点）。

## 能力与规则

| 需求 | 实现 |
| --- | --- |
| 固定矩形 + JSON 底图导入 | `POST /import`（路点 `x/y`，道路 `point_ids` 有序点号），导入即批号 0 |
| 与矩形相交的道路整条纳入 | 折线-矩形相交判定（点在矩形内、线段穿边、边界/角点接触均算相交），整条道路及其**全部引用点**（含区外点）随图返回 |
| 连续批号增删改 | `POST /batches`，首更新批必须为 1，之后严格 `last+1`；断档整批拒绝（422） |
| 只重判改动道路与引用变动点的道路 | 受影响集合 = 道路操作 ID ∪ 经 `road_point(point_id)` **索引反查**的道路，不扫描全图 |
| 缺失引用点整批拒绝 | 静态预检（批后点集合、删点引用阻塞）+ 事务内仅针对受影响道路的缺失点断言；任何失败回滚 |
| 同号同内容重送幂等 | 请求体规范化 SHA-256 入库；同哈希返回原回执并标记 `replayed` |
| 同号改内容冲突 | 返回 409，不覆盖历史批次 |
| 取图同次提交一致性 | `region_road` 成员表随提交增量重写；取图在单事务快照内完成，道路、引用点、批号严格同批 |
| 更新失败仍可取原图 | 全程单事务，失败即回滚；WAL 模式下读写互不阻塞 |
| Swagger 可操作 | `/docs` 提供导入、更新、取图全部接口的交互界面 |

## 目录结构

```
app/
  main.py        FastAPI 路由与异常处理
  schemas.py     Pydantic 请求/响应模型与校验
  service.py     导入、批次应用、取图（事务、幂等、成员增量重算）
  geometry.py    平面折线-矩形相交判定
  database.py    SQLite 连接、WAL、表结构
sample_data/     可直接导入 Swagger/curl 的样例底图与批次
tests/           pytest 自测（13 项 API + 10 项几何）
Dockerfile
docker-compose.yml
```

### 数据表要点

- `waypoint(id,x,y,batch_no)`、`road(id,name,batch_no)`、`road_point(road_id,seq,point_id)` 保留有序点号；
- `region_road(region_id,road_id,batch_no)` 是区域成员快照，每次提交只删写受影响道路的行；
- `batch(batch_no,payload_hash,receipt_json,applied_at)` 保存幂等指纹与回执；
- 外键级联保证删路/删点不产生悬挂引用。

## 用 Docker Compose 启动

```bash
docker compose up --build
# API:   http://localhost:8000
# Swagger: http://localhost:8000/docs
# 数据持久化在命名卷 roadmap-data (/data/roadmap.db)
```

## 本地直接运行（无 Docker 时）

```bash
pip install -r requirements.txt
export ROADMAP_DB_PATH=./data/roadmap.db
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口与典型流程

### 1. 导入底图（批号 0）

```bash
curl -X POST http://localhost:8000/import \
  -H 'Content-Type: application/json' \
  -d @sample_data/base_map.json
```

重复导入返回 409；确需重置用 `POST /import?force=true`（清空库）。

### 2. 提交增量批次

```bash
curl -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d @sample_data/batch_1.json
```

回执示例：

```json
{
  "batch_no": 1,
  "status": "applied",
  "payload_hash": "4298a851…",
  "affected_roads": [101, 102, 105],
  "regions_changed": { "R-CENTER": [101, 102, 105], "R-EAST": [101] }
}
```

- 同号同内容重发 → 200 且 `status="replayed"`，回执内容（含 `applied_at`）不变；
- 同号不同内容 → 409；
- 批号断档、引用缺失、删除仍被引用的点 → 422，整批回滚；
- 历史回执：`GET /batches/{batch_no}/receipt`。

### 3. 离线取图

```bash
curl http://localhost:8000/regions/R-CENTER/map
```

返回 `batch_no`（当前提交号）、矩形、成员道路（带有序 `point_ids`）以及这些道路
引用的全部路点坐标（含区外点）。

## 自测

```bash
python -m pytest tests/ -v
```

覆盖：导入成员判定与区外点随图、断档拒绝、缺失引用整批回滚、增量只重判受影响道路
（含 `EXPLAIN QUERY PLAN` 断言走索引而非全表扫描）、路点移动经反查联动、幂等重放、
内容冲突、删点/删路语义、快照一致性、边界擦边纳入、并发同批号写。

## 错误码约定

| HTTP | 场景 |
| --- | --- |
| 404 | 区域/回执不存在 |
| 409 | 重复导入、未导入即更新、同号不同内容冲突 |
| 422 | 批号断档、引用缺失、删除被引用点、请求体结构/矩形非法 |
