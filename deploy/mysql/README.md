# golf 库数据库说明

> 实例：`39.102.63.30:3306` · MySQL 8.0.46 · 库 `golf` · 字符集 `utf8mb4 / utf8mb4_0900_ai_ci`
> 已执行迁移：`001_init`（任务与结果）· `002_auth`（登录与操作记录）—— 均于 2026-09-05

## 一、连接方式与凭据

**密码一律走环境变量或项目根 `.env`，禁止写进任何脚本、文档或 git。**

### 凭据来源（优先级从高到低）

| 优先级 | 来源 | 适用 |
|--------|------|------|
| 1 | 系统环境变量（`export` / systemd `Environment=`） | **生产环境** |
| 2 | 项目根 `.env`（模板见 `.env.example`） | **本地开发** |

`.env` 由 `backend/app/env.py` 在 `config.py` 导入时自动加载（零第三方依赖）。
已存在于 `os.environ` 的键**不会被 `.env` 覆盖**，因此生产用 systemd 注入的值始终优先。

```bash
# 本地开发：把 .env.example 复制成 .env 并填真实值（.env 已在 .gitignore 中）
cp .env.example .env

# 自检 .env 是否被正确读取（值自动脱敏，显示被环境变量覆盖的键）
python backend/app/env.py
```

### mysql 客户端

```bash
export MYSQL_PWD='<password>'
mysql -h 39.102.63.30 -P 3306 -u geo --default-character-set=utf8mb4 golf

# 执行脚本
mysql -h 39.102.63.30 -P 3306 -u geo --default-character-set=utf8mb4 golf < 002_auth.sql
```

Python：项目 `.tools/python312` 已装 `pymysql` + `dbutils`，**统一走 `app.db`，不要自己建连接**：

```python
from app import db

db.ping()                                                  # 连通性探测
db.fetchone("SELECT * FROM users WHERE openid=%s", (openid,))
db.insert("INSERT INTO users (openid) VALUES (%s)", (openid,))  # 返回自增 id
```

> ⚠️ **`app.db` 的所有函数都不抛异常** —— DB 未配置 / 连接失败 / 查询报错
> 一律返回空值（`None` / `[]` / `0`）并记日志。这是「MySQL 不可用不影响分析
> 主链路」的保障（方案验收标准 8）。调用方**不要**写 `try/except`，
> 但也**不要**假设拿到的结果一定非空。

部署自检（凭据从环境变量或 `.env` 自动读取，命令行**不需要**出现密码）：

```bash
python deploy/mysql/check_connection.py            # 只读：连通性 + 表结构 + 迁移版本
python deploy/mysql/check_connection.py --smoke     # 额外做一轮 写→读→清理
```

> ⚠️ 自检脚本必须走 `from app import config`，**不要直接读 `os.environ`** ——
> 否则会绕过 `.env` 加载，导致明明配了 `.env` 却报「GOLF_DB_PASSWORD 未设置」。

### 连真库才暴露的两个坑（mock 测试覆盖不到，改动时注意）

1. **`information_schema` 列名是大写** —— `SELECT table_name` 返回的 dict key 是
   `TABLE_NAME`，必须起别名：`SELECT table_name AS tbl ...`
2. **`JSON_EXTRACT` 返回 JSON 类型** —— pymysql 转成字符串 `'true'`（带引号），
   不能直接 `int()`。用 `CAST(JSON_EXTRACT(col, '$.k') AS UNSIGNED)`

## 二、表清单

| 表名 | 列数 | 粒度 | 说明 | 版本 |
|------|------|------|------|------|
| `tasks` | 31 | 1 行 = 1 次分析 | 任务主表，含状态机 + VideoMeta + GlobalMetrics 固定字段 | 001 |
| `task_phases` | 12 | 1 任务 = 8 行 | 8 阶段结果（address…finish） | 001 |
| `task_metrics` | 17 | 1 任务 ≈ 190 行 | 阶段指标（`scope=phase`）+ 全程指标（`scope=global`） | 001 |
| `task_risks` | 17 | 1 任务 = 0~N 行 | 损伤风险项（RISK-001~017） | 001 |
| `users` | 18 | 1 行 = 1 个微信用户 | `openid` 唯一；昵称/头像留空 = 未设置 | 002 |
| `user_tokens` | 10 | 1 行 = 1 个有效 token | **只存 SHA-256，不存明文** | 002 |
| `operation_logs` | 12 | 1 行 = 1 次动作 | 匿名操作也记（`openid` 可为 NULL） | 002 |
| `schema_migrations` | 4 | 1 行 = 1 个版本 | DDL 迁移版本记录 | 001 |

## 三、ER 关系

```
tasks (task_id PK 业务键)
  │
  ├──< task_phases    (task_id, phase_key)        1 : 8
  │        │
  │        └──< task_metrics  scope='phase'       1 : ~23   (phase_key 关联)
  │
  ├──< task_metrics   scope='global'              1 : ~23   (phase_key IS NULL)
  │
  └──< task_risks     (task_id, trigger_phase, rule_id)     (trigger_phase 逻辑关联)
                                                            (rule_id 唯一)

users (openid UK)
  │
  ├──< user_tokens      (openid)                  1 : N   (可有多个活跃 token)
  │
  ├──< operation_logs   (openid, 可为 NULL)       1 : N   (未登录的匿名操作也记录)
  │
  └──< tasks            (openid, 可为 NULL)       1 : N   (任务归属, M1 起回填)
```

**全部为逻辑关联，无物理外键**（见下方设计决策 2）。

## 四、关键设计决策

| # | 决策 | 理由 |
|---|------|------|
| 1 | 全小写下划线命名 | 实例 `lower_case_table_names=0`（Linux，表名区分大小写） |
| 2 | **不加物理外键** | MVP 阶段「按 task_id 清理过期任务」是高频操作，级联约束会让它变成高风险动作；改为应用层保证顺序 |
| 3 | `VideoMeta` / `GlobalMetrics` 固定字段**扁平化进 `tasks`** | 1:1 关系单开表只会带来无谓 JOIN |
| 4 | 指标用 `scope` 区分 phase / global，**共用一张表** | 指标结构完全一致；分表会导致同一指标的跨任务对比要 UNION |
| 5 | `tasks.result_json` 存完整响应快照 | 结构演进时旧数据仍可完整回溯，不必为每个字段变更写迁移 |
| 6 | `task_phases.source` / `confidence` **预留 NULL** | 对应 DTL per-event 混合策略（SwingNet vs 规则引擎），当前 pipeline 尚未回填 |
| 7 | `tasks.openid` 预留 NULL | MVP 不做微信登录，但为后续多用户预留 |
| 8 | 时间统一 `DATETIME(3)` | 原 `TaskState.created_at` 是 float epoch，落库转毫秒级时间；`updated_at` 用 `ON UPDATE CURRENT_TIMESTAMP(3)` 自动维护 |
| 9 | `user_tokens` **只存 `token_hash`** | 库被拖走也无法冒用 token。`session_key` 也只存不下发（可解密微信敏感数据） |
| 10 | **不用 JWT 做登录态** | JWT 无法主动失效，而 MVP 需要「禁用用户立即生效」；服务端 token + `revoked` 标记更简单可控 |
| 11 | `users.nickname` / `avatar_url` **空串 = 未设置** | 默认值（"球手 0007" / 首字色块）由 `GET /auth/me` 兜底返回，**不入库**。理由：①空值本身是有效信息 ②换默认规则无需洗数据 ③`nickname_updated_at IS NULL` 即可判断是否用户自设，无需加列 |
| 12 | 三张新表同样**不加物理外键** | 与决策 2 一致；`openid` 逻辑关联，应用层保证删除顺序 |
| 13 | `operation_logs` 不记状态轮询 | `GET /task/status/{id}` 每 1.5s 一次，量大且无分析价值 |

## 五、字段来源映射

| 表 | 对应 `app/schemas.py` 结构 |
|----|---------------------------|
| `tasks` | `TaskState` + `VideoMeta` + `GlobalMetrics`（3 固定字段）+ `AnalysisResult.warnings/disclaimer` |
| `task_phases` | `PhaseResult`（除 `metrics` / `risks`）+ `SwingEvent` |
| `task_metrics` | `StageMetric`（11 字段 1:1） |
| `task_risks` | `RiskItem`（13 字段 1:1，`suggestions` → JSON） |
| `users` | 微信用户（非 `schemas.py` 结构，方案 §4.1 新定义） |
| `user_tokens` | 登录态（方案 §4.2） |
| `operation_logs` | 操作记录（方案 §4.3） |

枚举落库为字符串，与 Python 侧 `.value` 一致：

- `status`: `pending` / `processing` / `success` / `failed`
- `camera_view`: `face_on` / `down_the_line`
- `MetricStatus`: `low` / `normal` / `high` / `critical_low` / `critical_high`
- `MetricSource`: `measured` / `proxy` / `reference`
- `RiskLevel`: `high` / `medium` / `low`

## 六、冒烟验证

两次建表各跑过一轮，数据均已清理，当前 **7 张业务表均为 0 行**。

已验证：JSON 列读写（`JSON_EXTRACT` / `JSON_LENGTH`）、中文存储、JOIN 聚合、
唯一索引冲突检测、`STRICT_TRANS_TABLES` 下类型合规、自增 id 返回。

后续要复验直接跑自检脚本（凭据自动从 `.env` 读取，不需要手写 SQL 或 export）：

```bash
python deploy/mysql/check_connection.py --smoke
```

## 七、接入进度

### ✅ M0 已完成（2026-09-05）

- [x] 凭据管理 `app/env.py`：项目根 `.env` 加载器（零依赖，env 优先于 .env）
- [x] 单元测试 `tests/test_env.py`（45 例：解析边界 / 优先级 / 脱敏 / 降级）
- [x] `.gitignore` 忽略 `.env`，保留模板 `.env.example`
- [x] `config.py` 增加 `DB_*` 与 `WX_*`，密码/AppSecret 均从 `.env` 或环境变量读
- [x] 依赖：给 `.tools/python312` 装 `pymysql` + `dbutils`
- [x] 新增 `app/db.py`：连接池 + `execute` / `executemany` / `insert` / `fetchone` / `fetchall` / `ping`
- [x] 连接池 `ping=1`（等价 `pool_pre_ping=True`）—— 云数据库会主动断开长空闲连接
- [x] **建池失败退避 30s** —— 保证 DB 宕机不会让每个请求都卡在连接超时上
- [x] 单元测试 `tests/test_db.py`（20 例：降级语义 / 退避 / 返回类型 / 连接归还）
- [x] 部署自检脚本 `deploy/mysql/check_connection.py`
- [x] **真库验证通过**（2026-09-05）：8 张表列数全对、迁移版本 `001_init`/`002_auth`、
      冒烟写→读→清理正常。此前只在 mock 下测过，真库暴露了列名大小写与
      `JSON_EXTRACT` 类型两个 bug（见 §一）

### ⬜ 待办（M1 起）

- [ ] `task_store.py` 改为「内存态 + 落库」双写；**读取仍走内存**，保证现有接口零延迟退化
- [ ] 落库时机：仅在 `succeed()` / `fail()` 两个终态写入（避免处理中频繁 I/O）
- [ ] `tasks.openid` 回填（M1 登录接入后）
- [ ] `task_phases.source` / `confidence` 回填（需 pipeline 透出 per-event 来源）
- [ ] 过期清理：`sweep()` 删文件的同时软删 / 硬删库记录
- [ ] `operation_logs` 归档：删除 90 天前数据（随任务 TTL 一起做）
- [ ] ⚠️ 部署到服务器时，systemd 需注入 `GOLF_DB_PASSWORD` 环境变量（**不写进任何文件**）
