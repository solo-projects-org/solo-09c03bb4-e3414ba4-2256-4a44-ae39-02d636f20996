# 区域路网离线取图与管养里程 API

面向市政巡检终端的区域路网服务：导入固定矩形与简化 JSON 底图（平面路点坐标、
道路按有序点号引用路点，**坐标单位为米**），以连续批号接收路点/道路的增删改，
为每个矩形区域输出可离线使用的路网快照，并核定区域内的管养里程。

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
| 区域管养里程 | `GET /regions/{region_id}/mileage`，返回区内总米数与批号 |
| 相邻点成段、无向去重 | 无向点号对 `(low,high)` 为计量单元，反向引用、同路重复引用、多路共用线段每区只计一次 |
| 只计区内长度 | Liang-Barsky 裁剪线段到矩形，贴边部分计入，区外部分不计 |
| 里程汇总增量维护 | `segment_measure`（区域×点号对→区内米数）+ `region_measure`（区域总数）；查询只读总数，不重新量算 |
| 只重算变化的线段 | 道路操作取新旧拓扑对称差；路点移动/删除取受影响道路中包含该点的线段 |
| 共用线段不因删路丢失 | 删路后按“是否仍有道路引用该点号对”判定保留，而非随道路级联清零 |
| 旧 SQLite 底图免重导 | 启动时检测 `metadata.measure_version`，缺失则就地一次性补建汇总 |
| 里程与区域图同批 | 汇总在批次事务内更新并写入 `batch_no`，查询与区域图同一最新批号；拒收/重送不改统计 |
| Swagger 可操作 | `/docs` 提供导入、增量、取图、查里程全部接口的交互界面 |

## 目录结构

```
app/
  main.py        FastAPI 路由与异常处理
  schemas.py     Pydantic 请求/响应模型与校验
  service.py     导入、批次应用、取图、里程查询（事务、幂等、增量维护）
  measure.py     里程汇总：无向点号对去重、区内裁剪、增量重算与旧库回填
  geometry.py    平面折线-矩形相交判定、线段-矩形裁剪长度
  database.py    SQLite 连接、WAL、表结构
sample_data/     可直接导入 Swagger/curl 的样例底图与批次
tests/           pytest 自测（API、几何、里程）
Dockerfile
docker-compose.yml
```

### 数据表要点

- `waypoint(id,x,y,batch_no)`、`road(id,name,batch_no)`、`road_point(road_id,seq,point_id)` 保留有序点号；
- `region_road(region_id,road_id,batch_no)` 是区域成员快照，每次提交只删写受影响道路的行；
- `segment_measure(region_id,low_id,high_id,length_m,batch_no)` 是里程明细，
  主键 `(region_id, low_id, high_id)` 保证每个区域每个无向点号对只有一行；
- `region_measure(region_id,total_m,batch_no)` 是区域里程总数，查询直接读取；
- `batch(batch_no,payload_hash,receipt_json,applied_at)` 保存幂等指纹与回执；
- `metadata.measure_version` 标记里程汇总已建立，旧库首次启动时据此回填；
- 外键级联保证删路/删点不产生悬挂引用；里程明细不挂 road 外键，
  删路后是否保留改由“线段是否仍被引用”判定（见下）。

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

### 4. 查询管养里程

```bash
curl http://localhost:8000/regions/R-CENTER/mileage
```

```json
{ "region_id": "R-CENTER", "batch_no": 2, "total_m": 29.21901127804955 }
```

口径：

- 路点坐标以米计，相邻路点构成线段，长度按欧氏距离；
- 计量单元是无向点号对 `(low_id, high_id)`：**反向引用**（A→B 与 B→A）、
  **同一条路内的重复引用**、**多条道路共用同一线段**，在每个区域都只计一次；
- 只累计线段落在矩形内的部分（Liang-Barsky 裁剪）；沿边界走/贴边的部分计入，
  仅在角点相切的线段长度为 0；
- 某条道路删除后，若同一线段仍被其他道路引用，长度继续计入；
- 批次被拒收（422/409）或同内容重送（replayed）都不改变统计。

增量维护：汇总随批次事务一并提交。每次提交只重算“引用变化（道路增删改的新旧
拓扑对称差）或端点坐标变化（受影响道路中包含该点的线段）”的点号对，
其余线段与区域总数不动；查询只读 `region_measure` 的总数。

旧版本 SQLite 底图无需重新导入：服务启动时若发现未建立里程汇总，会按现有
路网就地一次性补建（`metadata.measure_version` 作为完成标记，可重复安全执行）。

## 自测

```bash
python -m pytest tests/ -v
```

覆盖：导入成员判定与区外点随图、断档拒绝、缺失引用整批回滚、增量只重判受影响道路
（含 `EXPLAIN QUERY PLAN` 断言走索引而非全表扫描）、路点移动经反查联动、幂等重放、
内容冲突、删点/删路语义、快照一致性、边界擦边纳入、并发同批号写；里程口径
（区内裁剪、贴边、反向/重复/跨路共用去重、分区域）、增量重算范围（未触及线段批号
不变）、删路后共用线段保留、拒收/重送不改统计、旧库免重导回填幂等。

## 错误码约定

| HTTP | 场景 |
| --- | --- |
| 404 | 区域/回执不存在 |
| 409 | 重复导入、未导入即更新、同号不同内容冲突 |
| 422 | 批号断档、引用缺失、删除被引用点、请求体结构/矩形非法 |
