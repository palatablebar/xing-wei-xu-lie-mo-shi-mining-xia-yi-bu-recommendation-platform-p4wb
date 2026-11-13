# nextbest · 行为序列模式挖掘与下一步推荐台

织云 SaaS 增长团队的工作台：把 12 个功能模块的埋点会话变成**可解释的下一步推荐**。
链路是「埋点事件 → 会话序列 → PrefixSpan 模式库 → 下一步推荐 → 运营复核名单 → CSV 导出」。

- 客户场景：增长团队 5 人、12 个功能模块；原来靠看事件表猜下一步、配运营弹窗。
- 解决的三个问题：序列靠猜（改成频繁序列挖掘）、覆盖率低（按用户最近 3 个事件做后缀匹配）、推荐没有置信度（每条推荐带支持度/置信度/提升度）。

## 1. 技术栈与 5 服务

| 服务 | 内容 | 端口（.env） |
| --- | --- | --- |
| `frontend` | Nginx 静态站，原生 HTML/CSS/JS（ES Modules，无框架无构建） | `FRONTEND_PORT=18209` |
| `backend` | FastAPI + SQLAlchemy 2.0，全部计算/校验/持久化在后端 | `BACKEND_PORT=19209` |
| `worker` | Celery worker + beat：心跳与每日可恢复重挖（候选版，不自动切换） | 内网 |
| `db` | MySQL 8.0（utf8mb4） | `DB_PORT=17709` |
| `redis` | 限流计数 + worker 心跳 | 内网 |

```bash
docker compose config --quiet
docker compose up -d --build      # 5/5 healthy 后访问 http://localhost:18209
docker compose exec -T backend python -m scripts.seed_demo   # 幂等，可重复执行
docker compose exec -T backend python -m pytest tests -q
```

## 2. 账号与角色（密码统一 `Demo@2026`）

| 账号 | 角色 | 能做什么 |
| --- | --- | --- |
| `admin@nextbest.io` | 增长平台管理员 | 维护事件字典、发起挖掘、复核推荐、导出 CSV、看审计 |
| `analyst@nextbest.io` | 行为分析专员 | 发起挖掘、复核推荐、导出 CSV；不能改事件字典 |
| `viewer@nextbest.io` | 运营只读 | 只读模式/推荐（用户 ID 脱敏）；不能挖掘、不能复核、不能导出、看不到审计 |

权限不是前端隐藏：低权调用高权接口返回**真实 403**（`src/middlewares/rbac.py`），无 token 401，非法参数 422，
事件码重复 409。视图层再加一层按钮收敛，`site/js/config.js` 的 `PERMISSIONS` 与后端
`src/constants/roles.py` 一一对应。

## 3. 领域实体（8 张表）

| 实体 | 表 | 关键字段 |
| --- | --- | --- |
| `EventDef` 事件口径 | `event_defs` | `code`、`module`、`label`、`is_key`、`weight`、`sample_count` |
| `EventSeq` 会话序列 | `event_seqs` | `session_id`、`user_id`、`events`(JSON)、`event_times`(JSON,对齐事件时间)、`event_count`、`started_at`、`converted` |
| `IngestCursor` 采集水位 | `ingest_cursors` | `source`、`last_event_id`、`last_event_at`、`ingested_events`、`duplicate_events`、`late_events` |
| `IngestedEvent` 已并入事件台账 | `ingested_events` | `event_id`(主键,幂等屏障)、`source`、`session_id`、`event_code`、`event_at` |
| `AppState` 应用状态 | `app_state` | `state_key`、`state_value`（正式版运行指针 `active_mine_run_id`） |
| `MineRun` 挖掘运行 | `mine_runs` | `run_no`、`status`、`stage`、`is_active`、`watermark_at`、`ingested_events`、`carried_recs`、`reset_recs`、`promoted_at`、`metrics`、`coverage_pct` |
| `Pattern` 频繁序列 | `patterns` | `items`(JSON)、`length`、`support_count`、`support_pct`、`confidence`、`lift`、`rank` |
| `Recommendation` 下一步推荐 | `recommendations` | `user_id`、`suffix`(JSON)、`next_event`、`confidence`、`lift`、`score`、`priority`、`status`、`carry_state` |

每个实体贯穿「表 → model → schema → service → controller → route → 前端 api → 页面」，
DDL 参考见 `backend/migrations/001_core_tables.sql`。

## 4. 算法口径（后端 Python 真算，前端只展示）

纯函数在 `backend/src/services/prefixspan.py` 与 `backend/src/services/recommender.py`，不碰 DB，可直接单测。

### 4.1 算法 A｜PrefixSpan 频繁序列挖掘

- 最小支持度 `min_support_pct=3%`：按会话数向上取整 → 600 条会话 ⇒ **18 条会话**；
- 最大长度 `max_len=5`；只收长度 ≥ 2 的序列（单个事件不构成行为路径）；
- 支持度 = 命中会话数 / 参与挖掘的会话数；模式库按支持度倒序保留 `top_k=50` 条；
- 置信度 = 命中会话数 / 去掉末位事件后的前缀命中会话数（P(下一步 | 前缀)）；
- 提升度 = 置信度 / 末位事件在全部会话中的占比。

### 4.2 算法 B｜下一步推荐

- 每位活跃用户取最近一条会话的最后 `k=3` 个事件作为后缀（不足 3 个按实际长度）；
- 候选模式：模式前缀尾部与后缀逐位相等（优先 3 位，其次 2 位、1 位），推荐事件 = 模式末位事件；
- 同一模式只出一条；分数 = **置信度 × 提升度**，按分数倒序；
- 同模块最多 2 条、每用户最多 6 条（`MAX_PER_MODULE` / `MAX_PER_USER`）；
- 优先级：分数 ≥1.2 高、≥0.7 中、其余低；
- 覆盖率 = 有推荐产出的用户数 / 活跃用户数。

> 结果全部落库（`patterns` / `recommendations`），前端只做展示与筛选，不参与任何计算。

### 4.3 可恢复重挖、版本切换与复核继承

采集侧重发迟到事件、同批事件重复到达，旧的「每日全量覆盖」会让复核意见和名单混杂。新口径：

- **幂等采集**（`services/ingest_service.py`）：`ingested_events.event_id` 是唯一台账，同批重跑/重复投递直接拦截不新增；
  迟到事件按 `occurred_at` 并入原会话（不新建会话），按时间重排并合并相邻重复，`ingest_cursors` 记录每路水位（时间水位 + 并入/重复/迟到计数）。
- **分阶段可恢复重挖**（`services/mining_service.py`）：每次重挖产出候选运行（`is_active=false`），四阶段
  `queued → patterns → recommend → coverage → done`，每阶段独立事务提交。中断/失败停在断点（`metrics.last_good_stage`），
  同 `seed_key` 重跑或调 `/resume` 从断点续跑，不新建运行、不重复落库。
- **一次性切换**（`services/version_service.py`）：模式、推荐、覆盖率全部 `done` 后才允许 `promote`；
  切换只改 `app_state` 指针与 `mine_runs.is_active`，各版本数据按 `run_id` 隔离。失败不触碰正式版，可 `rollback` 到任一历史版。
- **复核意见继承**：候选推荐按 `(user_id, suffix, next_event)` 与正式版比对——三者一致且正式版已复核则沿用结论
  （`carry_state=same`，保留状态/复核人/备注）；口径变化回待复核（`changed`）；全新推荐为 `new`。正式版复核意见永不覆盖、不丢失。
- 推荐/模式/导出在不指定 `run_id` 时默认读**正式版**，候选版未切换不会混入名单。

## 5. API

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/api/auth/login` | 邮箱+密码换 JWT | 公开 |
| GET | `/api/auth/me` | 当前用户 | 登录 |
| GET | `/api/overview` | 会话数/覆盖率/模式数/信号瀑布 | 三角色 |
| GET/POST/PATCH | `/api/events`、`/api/events/{id}` | 事件字典 | 读三角色，写仅 admin |
| GET | `/api/sequences`、`/api/sequences/{id}`、`/api/sequences/stats` | 会话序列浏览 | 三角色（viewer 脱敏） |
| GET | `/api/sequences/watermark` | 采集水位（时间水位/并入/重复/迟到） | 三角色 |
| POST | `/api/sequences/ingest` | 事件幂等并入（迟到按时间归位） | admin+analyst（viewer 403） |
| GET/POST | `/api/mine-runs`、`/api/mine-runs/{id}` | 挖掘运行（POST 产出候选版） | 读三角色，发起 admin+analyst |
| GET | `/api/mine-runs/active`、`/api/mine-runs/{id}/diff` | 正式版指针 / 切换前后差异 | 三角色 |
| POST | `/api/mine-runs/{id}/resume`、`/promote`、`/rollback` | 断点续跑 / 切换正式版 / 回滚历史版 | admin+analyst（viewer 403） |
| GET | `/api/patterns`、`/api/patterns/distribution` | 模式库（默认正式版） | 三角色 |
| GET | `/api/recommendations`、`/api/recommendations/{id}` | 推荐名单（默认正式版，viewer 脱敏） | 三角色 |
| PATCH | `/api/recommendations/{id}/review` | 复核采纳/驳回 | admin+analyst（viewer 403） |
| GET | `/api/exports/recommendations.csv`、`patterns.csv`、`mine-runs.csv` | CSV 导出 | admin+analyst（viewer 403） |
| GET | `/api/audit` | 审计日志 | 仅 admin |
| GET | `/api/health` | 健康检查（db/redis/worker 心跳） | 公开 |

## 6. 页面（原生 ES Modules）

1. `#/login` 登录（信号 scope + 操作员终端版式）
2. `#/overview` 总览：仪器读数 + **本批采集水位** + 信号瀑布 + 模块事件量 + 正式版/候选版 + 切换前后差异
3. `#/events` 事件字典（列表 + 抽屉详情 + admin 改口径/登记）
4. `#/sequences` 序列浏览（采集水位 + 筛选 + 事件轨道 chip + 抽屉全文）
5. `#/mining` 模式挖掘（候选/正式/历史运行 + 发起重挖 + 断点续跑 + 切换/回滚 + 差异确认 + 模式库 + 导出）
6. `#/recommendations` 推荐名单（默认正式版 + 复核继承标记 + 筛选 + 抽屉复核 + 导出 CSV）
7. `#/audit` 审计日志（仅 admin 可见可访问）

共用组件：`site/js/ui.js` 的 `table` / `drawer` / `field` / `readouts` / `waterfall` / `track` / `ladder` / `badge`。

## 7. 共享枚举出现位置（改动需两侧同步）

| 枚举 | 后端 | 前端 |
| --- | --- | --- |
| 6 个模块 `account/integration/board/report/alert/billing` | `backend/src/constants/enums.py::Module`、`MODULE_LABELS` | `site/js/config.js::ENUMS.module` |
| 挖掘状态 `queued/running/success/partial/failed` | `enums.py::MineStatus`、`MINE_STATUS_LABELS` | `ENUMS.mineStatus`、`config.js::statusTone` |
| 运行阶段 `queued/patterns/recommend/coverage/done/failed` | `enums.py::RunStage`、`RUN_STAGE_LABELS` | `ENUMS.runStage` |
| 版本 `candidate/active/archived` | `enums.py::RunEdition`、`RUN_EDITION_LABELS` | `ENUMS.edition` |
| 复核继承 `new/same/changed` | `enums.py::CarryState`、`CARRY_STATE_LABELS` | `ENUMS.carryState` |
| 触发方式 `manual/schedule/seed` | `enums.py::TriggerKind`、`TRIGGER_LABELS` | `ENUMS.trigger` |
| 复核状态 `pending/confirmed/dismissed` | `enums.py::ReviewStatus`、`REVIEW_STATUS_LABELS` | `ENUMS.reviewStatus`、`reviewTone` |
| 优先级 `high/medium/low` | `enums.py::RecPriority`、`REC_PRIORITY_LABELS` | `ENUMS.priority`、`badgeTone` |
| 角色 `admin/analyst/viewer` | `enums.py::Role`、`constants/roles.py` | `config.js::ROLE_LABELS/PERMISSIONS` |

## 8. seed（幂等）

`backend/scripts/demo_data.py` 用固定 seed + 会话序号生成确定性数据（无网络、无随机漂移）：
**12 个事件、600 条会话（均值约 8 事件）、90 条转化会话、1 次初始化挖掘**。
`backend/scripts/seed_demo.py` 按自然键 upsert：`users.email`、`event_defs.code`、`event_seqs.session_id`、
`mine_runs.seed_key`、`patterns(run_id, seq_key)`、`recommendations(run_id, user_id, pattern_id, next_event)`、
`audit_logs.seed_key`；重复执行不新增记录、不覆盖人工复核过的推荐状态。seed 还会回填历史会话的 `event_times`
与历史推荐的 `suffix`，并把首次初始化挖掘 `promote` 为第一个正式版（已存在正式版则不重复切换）。

结束时打印一行（两次执行逐字一致）：

```
SEED_SUMMARY brand=nextbest users=3 sources=<事件口径数> runs=<挖掘运行数> findings=<模式数> pending=<待复核推荐数>
```

`sources` 对应事件字典数量，`runs` 对应挖掘运行数，`findings` 对应模式库条数，`pending` 对应待复核推荐数。

## 9. 测试

```bash
docker compose exec -T backend python -m pytest tests -q     # 算法 + 安全工具单测
BASE_URL=http://localhost:18209 node ../test-artifacts/gb-610/tests/smoke.mjs
BASE_URL=http://localhost:18209 node ../test-artifacts/gb-610/tests/screenshots.cjs
```

- `backend/tests/test_prefixspan.py`：频次门槛、边界（等于最小计数入选 / 少一条落选）、长度上限、top_k 截断、置信度与提升度算术、单事件不计、空输入、确定性；
- `backend/tests/test_recommender.py`：后缀匹配、不推荐刚做过的动作、无匹配返回空、按分数排序、同模块 ≤2、每用户 ≤6、覆盖率、优先级分档；
- `backend/tests/test_security.py`：密码哈希、JWT 往返、非法 token；
- `backend/tests/test_ingest.py`：迟到事件按时间并入、相邻重复合并、同批重放 event_id 去重、批内重复只计一次、水位计数；
- `backend/tests/test_mining_version.py`：分阶段到 done、promote/rollback、复核 same/changed/new 继承、失败断点续跑不重复落库、同批重跑不新建运行；
- `backend/tests/smoke_e2e.py` / `smoke_http.py`：SQLite 全链路与 TestClient HTTP 冒烟（采集→重挖→差异→切换→回滚→总览→审计→403）。
- 真机结果（含 seed 两次原文、pytest、smoke、403、关键数值、截图清单）记录在
  `batch-20261113/test-artifacts/gb-610/TESTING.md`。

## 10. 横切能力

- **JWT 三角色**：`middlewares/auth.py` 解析 `Authorization: Bearer`，`rbac.py` 给每个路由挂角色依赖；
- **审计**：`middlewares/audit_logger.py` 记录所有写请求（layer=`http`），controller 再记领域动作（layer=`domain`），worker 记 `layer=worker`；
- **限流**：`middlewares/rate_limit.py` Redis 固定窗口，读 300/min、写 60/min（`/events`、`/mine-runs`、`/recommendations`、`/exports`）；
- **CSV 导出**：`utils/csv_export.py`（UTF-8 BOM），三个导出端点都写审计；
- **worker 心跳**：Celery beat 周期任务写 Redis，`/api/health` 与总览 ticker 读取，>120s 视为超时。

## 11. 视觉方向

时序信号台：深墨绿底（`oklch(0.185 0.026 167)`）+ 荧光橙读数（`oklch(0.755 0.185 55)`）+ 网格坐标 +
等宽字（`IBM Plex Mono`）+ 标题字（`Space Grotesk`）。总览用「信号瀑布」而非卡片墙，登录页用 scope/终端版式。
色板与字体定义在 `frontend/site/css/tokens.css`，离线时回退 `PingFang SC` / `SFMono-Regular`。

## 12. 已知边界

- 合成埋点数据（无真实客户埋点），会话时长与转化规则是演示口径；
- 模式库按支持度倒序截断 top_k（默认 50），更长的稀有路径不会进入推荐候选；
- 重挖仍是对当前全量会话重算 PrefixSpan（数据量在 600 会话量级），增量体现在**采集幂等并入**与**分阶段断点续跑**：
  失败可从断点恢复、同批重跑不新增不重复，但模式计算本身不做滑动窗口增量；
- 复核继承以 `(user_id, suffix, next_event)` 完全相等为「沿用」口径，后缀或下一步任一变化即回待复核。
