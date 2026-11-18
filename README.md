# 园区公共用电逐小时净电量 API

给分析库交付"每小时公共用电"：总表相邻整点差值减去**直接下级表**差值之和，
避免总表与租户分表重复计量。孙表及更深的表**不再扣减**（其电量已含在上级分表内）。

技术栈：FastAPI + SQLite（WAL 模式），Python 3.11。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

服务监听 `8000` 端口，交互式文档在 **http://localhost:8000/docs**。
SQLite 数据文件落在名为 `energy-data` 的卷里（容器内路径 `/data/energy.db`，
可用环境变量 `ENERGY_DB_PATH` 覆盖）。

本地开发（不用 Docker）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
pytest                 # 运行测试
```

## 在 /docs 上演示

1. **上传整版层级**：`POST /api/hierarchy`，选择 `examples/hierarchy.csv`
   （内容为 `M1 -> T1, T2` 且 `T1 -> S1`）。整版语义：每次上传**全量替换**旧层级。
2. **补传读数**：`POST /api/readings`，选择 `examples/readings.csv`
   （M1/T1/T2/S1 在 2026-01-15 全天 25 个整点的累计读数）。
3. **查询净电量**：`GET /api/meters/{meter_id}/daily-net`，
   填 `meter_id=M1`、`date=2026-01-15`。
   预期每小时 `net_kwh = 100 - 30 - 20 = 50`（孙表 S1 的 10 不再扣）。
4. **演示拒收**：用 `examples/readings_bad_conflict.csv`（批内同键异值）或
   `examples/hierarchy_cycle.csv`（层级成环）上传，返回 422 且已有数据不变；
   `GET /api/batches` 可看到被拒批次的留痕。
5. **演示改接核查**：上传 `examples/hierarchy_rewire.csv`（`M1 -> T1, T2`、
   `T1 -> S1`、`M2 -> U1`）和 `examples/readings_rewire.csv`
   （M1 每小时只走 40，却被 T1(30)+T2(20) 分摊，净 -10 全天为负）。
   然后在 `POST /api/rewire/check` 填 `examples/rewire_check.json`：
   `T1` 从 `M1` 改挂到 `M2`。返回 `confirmable` 与逐小时模拟：
   M1 由 -10 回升到 20（40-20），M2 由 250 降到 220（300-50-30）。
   取返回的 `suggestion_id` 调 `POST /api/rewire/{id}/confirm`，层级只改这一条边，
   再查 M1/M2 的日净值即按新层级重算。
   核查后若补传/更正了两边参与表读数，或整版替换改变了任一边的直属表集合，
   确认会返回 `409`（`status=expired`），**不会**按旧依据改接，新状态不被覆盖。

命令行等价演示：

```bash
curl -F "file=@examples/hierarchy.csv" http://localhost:8000/api/hierarchy
curl -F "file=@examples/readings.csv"  http://localhost:8000/api/readings
curl "http://localhost:8000/api/meters/M1/daily-net?date=2026-01-15"
```

## CSV 格式

### 整版层级（`POST /api/hierarchy`）

```csv
parent_meter,child_meter
M1,T1
M1,T2
T1,S1
```

- 表头别名：`parent/上级表`、`child/下级表` 也可；UTF-8（可带 BOM）。
- 每对 `(上级表, 下级表)` 一条边；一只表至多一个上级。
- **整版替换**：上传成功即全量替换旧层级，历史日期的查询结果随之按新关系重算。

### 一批读数（`POST /api/readings`）

```csv
meter_id,ts_utc,kwh
M1,2026-01-15T00:00:00Z,10000.0
M1,2026-01-15T01:00:00Z,10100.0
```

- 表头别名：`meter/表号`、`timestamp/UTC整点`、`读数` 也可。
- 时间必须是 **UTC 整点**（`Z` 或 `+00:00`，分秒为 0），其它时区/非整点整批拒收。
- 键 = `(表号, UTC整点)`。

## 业务规则

**逐小时净电量**（`GET /api/meters/{meter_id}/daily-net?date=YYYY-MM-DD`，UTC 日）：

- 小时 `h` 需要 `h` 与 `h+1` 两个整点读数（全天共 25 个点，含次日 00:00）。
- `净电量 = 总表差值 - Σ 直接下级表差值`；孙表不再扣。
- 以下情况该小时 `status=pending_review`，只给待核原因、不给数值：
  - 参与表（总表 + 全部直接下级）缺任一整点读数；
  - 任一参与表读数倒退（差值为负）；
  - 净电量为负。

**拒收规则**（整次拒收，HTTP 422，已有数据不受影响）：

- 层级：成环、自环、一表多上级；
- 读数：一批内同键异值、非 UTC 整点、非数值读数。

**更正与幂等**：

- 历史同键异值 = 更正，覆盖旧值，旧日期结果随之更新；
- 同值重传 = 幂等，结果与数据库 `updated_at` 都不变；
- 层级整版替换后，历史日期按新层级重算（结果实时计算，不物化缓存）。

**一致性**：

- 每个请求一个事务；上传先做完所有校验再 `BEGIN IMMEDIATE` 落库，
  任一步失败即回滚——失败后旧层级、旧读数、旧查询结果全部可用；
- 查询在单个读事务（WAL 快照）内取层级与读数，与交错进行的上传互不影响：
  一次查询看到的必是同一已提交状态，不会混杂新旧数据。

## 改接核查与确认

数据员发现某总表日公共用电为负时，可用现有读数模拟"把某只直属分表改挂到
另一个根总表"，只在模拟通过时生成可确认的建议。

### `POST /api/rewire/check` — 核查（生成建议）

请求体（见 `examples/rewire_check.json`，`from_parent` 可省略）：

```json
{"child_meter": "T1", "to_parent": "M2", "date": "2026-01-15"}
```

`from_parent` 可省略（按当前层级推断）；显式给出时必须等于当前层级中该分表的
直属总表（否则 409）。`to_parent` 不能是分表自身或其后代（否则 422）。

返回：`status=confirmable|not_confirmable`、两边的直属表集合（改接前/后）、
各边的负净值小时列表、`reasons`，以及 **24 小时逐时明细**：
`child_delta_kwh`（该分表差值）、`from_net_before/after_kwh`、
`to_net_before/after_kwh`。

同时满足以下四条才 `confirmable` 并生成 `suggestion_id`：

1. 原总表、目标总表两边的参与表（总表 + 直属）在全天 25 个整点每个小时齐全
   （改接前后都不缺整点，改接不会引入新缺口）；
2. 两边参与表全天读数均未倒退；
3. 原总表**至少一小时**改接前净值为负（挂错的迹象）；
4. 改接后两边 24 小时**均无负净值**。

不满足时不生成建议，`reasons` 逐条给出原因，并在 `/api/batches` 留 rejected 记录。

### `POST /api/rewire/{suggestion_id}/confirm` — 确认

在一个 `BEGIN IMMEDIATE` 写事务里重新读取当前层级与参与表读数，与核查时记录的
依据（两边直属表集合 + 参与表全天读数）逐项比对：

- 一致 → **只**执行 `UPDATE hierarchy SET parent_meter = to_parent WHERE child_meter = child`，
  其余边不动；返回 200，历史日期查询按新层级即时重算；
- 不一致（含该分表已被挂到别处）→ 返回 409 且 `status=expired`，建议状态置
  `expired`、写审计，**不写任何层级**，新状态不被旧依据覆盖；
- 非 `pending` 建议（已确认/已过期）→ 409；不存在的建议 → 404；
- 并发确认同一建议时，条件更新保证只有一方成功，另一方 409 回滚。

同一依据重复核查会复用仍为 `pending` 的建议，不会重复堆积。

"依据"按**当前状态**比较（两边直属表集合 + 参与表全天读数），不记录中间变更历史：
若状态被改走后又被改回完全相同的值，建议仍视为有效。

`GET /api/rewire/suggestions` 可查看全部建议（pending/confirmed/expired）及其核查结论。

## 项目结构

```
app/
  main.py     # FastAPI 路由与事务边界
  db.py       # SQLite 连接、schema、WAL
  parsing.py  # CSV 解析与纯函数校验（拒收规则）
  core.py     # 逐小时净电量计算（待核规则）
  rewire.py   # 改接核查的纯函数模拟（生成建议的判定）
tests/test_api.py     # 17 个场景测试
tests/test_rewire.py  # 18 个改接核查/确认、建议过期、交错场景测试
examples/             # 演示 CSV（含会被拒收的反面示例）与改接核查请求体
Dockerfile / docker-compose.yml
```

## 测试

```bash
pytest
```

覆盖：嵌套扣减（孙表不扣）、多直接下级、缺整点（含次日 00:00 边界）、
读数倒退、净值为负、批内同键异值整批拒收、成环/自环/多上级拒收、
历史更正更新旧结果、同值重传幂等、层级替换更新历史日、
失败上传后旧结果可用、上传与查询并发交错的快照一致性。
改接部分另覆盖：核查通过/四种否决条件、确认只改一条边且历史重算、
核查后读数更正/直属表集合变化/该边已被改挂导致建议过期、未知建议 404、
重复核查复用建议、并发确认仅一方成功、确认与查询/整版替换交错无半份状态。
