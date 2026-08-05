# QA 验证报告 —— T5 终态收尾回归（接口契约 + 小程序 v2）

- **QA**：严过关（software-qa-engineer）
- **日期**：2026-08-05
- **范围**：T5 终态快照 —— 双路径注册、PDD 错误码映射、camera_view、legacy 回滚、小程序 v2 静态自查
- **前置**：T1~T4 验证见 `docs/QA-VALIDATION-BC.md`（当时 main.py 正被 T5 实时修改，本报告为终态复测）

---

## 1. 测试执行结果

| 项目 | 结果 |
|---|---|
| 全量测试（终态） | **355 passed / 0 failed**，9.24s（与 T5 声称一致，无回归、无 flaky） |
| 独立 API 验证（自建服务 + 真实视频） | **33 PASS / 0 FAIL**（`backend/_probe_out/qa_t5_verify.py`） |
| 补充验证（20002 + legacy 回滚） | **6 PASS / 0 FAIL**（`qa_t5_extra.py` + `qa_t5_legacy.py`，修正自测缺陷后全绿） |
| 工程师冒烟脚本 `smoke_t5_full.py` | 工程师记录四项硬要求全过；**我方复跑在步骤④超时 —— 已定位为脚本 PIPE 缓冲死锁（见 §4.1），非后端缺陷** |

> ⚠️ **验证方法说明**：工程师的 `smoke_t5_full.py` 用 `subprocess.PIPE` 捕获服务输出但从不读取，服务日志量一大就会把 64KB 管道缓冲写满 → 服务端写阻塞 → 后续请求假死（我方复跑在步骤④超时 90s）。**我方改用「输出重定向文件」自建服务，全部通过** —— 证明后端无此问题，是探针脚本缺陷。

---

## 2. 接口契约审查（代码级）

### 2.1 双路径注册（main.py，装饰器计数 = 7）

| 路径 | 类型 | 对应函数 |
|---|---|---|
| `GET /api/v1/health` | 健康检查 | health |
| `POST /api/v1/task/create` | PDD 主路径 | create_task |
| `POST /api/v1/tasks` | 旧路径别名 | create_task |
| `GET /api/v1/task/status/{task_id}` | PDD 主路径 | get_task |
| `GET /api/v1/tasks/{task_id}` | 旧路径别名 | get_task |
| `GET /api/v1/task/result/{task_id}` | PDD 主路径 | get_result |
| `GET /api/v1/tasks/{task_id}/result` | 旧路径别名 | get_result |

- ✅ 3 条 PDD 主路径 + 3 条旧路径别名 + 1 条 health，**全部真实挂载**。
- ✅ **无路由吞并**：`/task/status/{id}`、`/task/result/{id}` 与 `/tasks/{id}`、`/tasks/{id}/result` 路径形态互斥，实测各自正确返回 20001（不误 404、不 500）。
- ✅ 空 task_id → 404/400（不 500）；超长 id（200 字符）→ 20001（不 500）。

### 2.2 PDD 错误码映射（对照架构 §6.3）

| 内部语义码 | HTTP | PDD 对外码 | 触发场景（实测） |
|---|---|---|---|
| 4001 | 400 | **10001**（文件过大） | >20MB 上传 → 实测 10001 ✅ |
| 4001 | 400 | **10002**（格式不支持） | .avi → 10002 ✅、未知扩展名 .xyz → 10002 ✅、缺文件字段 → 10002 ✅、空文件 → 10002 ✅ |
| 4001 | 400 | **10003**（时长） | 时长校验 handler（test_api 覆盖） |
| 4004 | 404 | **20001**（任务不存在） | 未知 task_id → 实测 20001 ✅ |
| 4009 | 409 | **20002**（任务未完成） | result 未就绪 → 实测 20002 + HTTP 409 ✅ |
| 5000 | 500 | **10004**（内部错误） | fallback handler（代码 + test_api 覆盖） |

- ✅ config.py 常量值：10001/10002/10003/10004/20001/20002，与设计 §6 完全一致。

### 2.3 camera_view（实测）

| 场景 | 结果 |
|---|---|
| 缺省不传 | 201，落 face_on（不硬拒）✅ |
| 显式 down_the_line | 201，结果 `camera_view=down_the_line` 透传 ✅，且 DTL 指标（swing_plane 或优雅剔除告警）生效 ✅ |
| 非法值 bogus | 201，回退 face_on（不硬拒）✅ |
| auto | 内部可接受（test_api 覆盖） |

### 2.4 video / file 双字段

- ✅ `_pick_upload` 优先取 `video`（PDD 主），`file` 兼容；双字段都传实测 201（video 胜出）。
- ✅ 都不传 → 10002「缺少视频文件」。
- ✅ 新路径 create 落盘文件名按扩展名生成（upload.mp4 / upload.mov，config.upload_filename）。

### 2.5 legacy 回滚开关（独立进程实测）

以「先切 `config.API_CODE_STYLE="legacy"` 再导入 app」方式起真实 uvicorn（端口 8014）：

| 场景 | 期望旧码 | 实测 |
|---|---|---|
| 未知任务 | 4004 | **4004** ✅ |
| 坏格式 | 4001 | **4001** ✅ |
| 旧路径 + file 字段成功上传 | 0 | **201 / code=0** ✅ |

- ✅ **legacy 回滚确实能让旧小程序继续工作**（旧包 code===0 判成功，失败包回落 4001/4004/4009 语义与旧版一致；HTTP 状态码不变）。
- 🟡 P2 操作提示：`API_CODE_STYLE` 是**硬编码 `Final[str]` 常量**（架构 §6.3 原文即「config 常量回滚开关」），非环境变量可配 —— 与设计一致，但线上回滚需改代码重新部署而非仅切环境变量。

### 2.6 小程序 api.js 与后端对齐

- ✅ 三条 URL 全部切到 PDD 主路径：`/task/create`、`/task/status/{id}`、`/task/result/{id}`（git diff 确认旧版用 `/tasks`、`/tasks/{id}`、`/tasks/{id}/result`，与后端双注册互为镜像）。
- ✅ `uploadVideo` 传参 `name: 'video'` + `formData: { camera_view }` —— 与后端 `video: UploadFile = File(None)` + `camera_view: str = Form("face_on")` 完全对齐。
- ✅ PDD 错误码文案表（10001~10004/20001/20002）与后端常量一致；`messageOf` 兼容 number（PDD 码）与 string（旧 ErrorCode）。
- ✅ 旧小程序兼容路径：后端 legacy 模式返回旧码，旧包 request() 以 `code===0` 判成功（不受 `message:"ok"→"success"` 影响）。

---

## 3. 端到端实测（独立自建服务 + 真实视频 正面1.mp4）

路径：`POST /api/v1/task/create`（camera_view=face_on）→ 轮询 `/task/status/{id}` → `GET /task/result/{id}`

| 断言 | 结果 |
|---|---|
| create 201 + task_id | ✅ |
| 轮询至 success，step_text 非空（=「计算姿态指标与风险」） | ✅ |
| result.phases == 8 | ✅ |
| result.camera_view == face_on | ✅ |
| risks 产出 ≥1（正面1 实测 2 条） | ✅ |
| 指标带 description 字段 | ✅ |
| disclaimer 非空 | ✅ |
| video_meta.total_frames == frame_count | ✅ |
| DTL 显式机位：camera_view 透传 + swing_plane（或剔除告警） | ✅ |

---

## 4. 小程序静态自查（无自动测试框架，抽核心）

| 检查项 | 文件 | 结果 |
|---|---|---|
| 机位二选一 UI 与 js 状态一致（cameraView / onSelectView / requirements 联动） | index.wxml ↔ index.js | ✅ 一致 |
| 机位样式类 view-option--on / view-dot / phone--landscape 均定义 | index.wxss | ✅ |
| 风险区配色类名 rcard--high/medium/low（红/琥珀/蓝 #3B82F6，符合 §6.5） | result.wxss | ✅ |
| 风险空状态分支 risk-ok（「本阶段动作良好，无高风险项」） | result.wxml | ✅ |
| 指标空状态 empty-metric（机位过滤后 0 项） | result.wxml + result.js (emptyMetrics) | ✅ |
| `cur` 驱动缩略图/大图/指标/风险联动（onSelect→_select→setData cur） | result.js + result.wxml | ✅ |
| 手册原文弹窗仅在有 manual_excerpt 时出现 | result.wxml/result.js | ✅ |
| 分析中页 step_text 优先展示 + 20001/4004 双码兼容 | analyzing.js | ✅ |
| app.js globalData.cameraView 默认/读写一致 | app.js ↔ index.js | ✅ |

---

## 5. 智能路由判定

**路由判定：NoOne（T5 终态全通过）**

- 全量 355 绿、独立 33+6 项 API/端到端断言全绿、legacy 回滚独立进程实测通过、小程序静态自查通过。
- **未发现 P0/P1 缺陷**。工程师声称的「355 全绿、冒烟四项硬要求全过、IS_PASS=YES」经独立复跑证实成立。

---

## 6. 遗留问题清单（P2，不阻塞）

| # | 问题 | 级别 | 说明 |
|---|---|---|---|
| 1 | `smoke_t5_full.py` 用 `subprocess.PIPE` 捕获服务输出但从不读取，日志量大时管道写满会**假死服务**（我方复跑步骤④超时 90s 即此因） | P2 | 探针脚本缺陷，非后端缺陷。建议改为输出重定向文件（我方已验证此法全通过） |
| 2 | `API_CODE_STYLE` 为硬编码常量、非环境变量可配 | P2 | 与架构 §6.3 设计一致（config 常量回滚开关）；线上回滚需改代码重部署，操作提示非缺陷 |
| 3 | `ok()` 的 message 由 "ok" 改为 "success"（对齐 PDD） | P2 | 旧小程序按 `code===0` 判成功，不受影响；如外部有按 message 字面量判断的调用方需注意 |

---

## 7. 验证产物

- 独立 API 验证：`backend/_probe_out/qa_t5_verify.py`（33 项，端口 8011）
- 补充验证：`backend/_probe_out/qa_t5_extra.py`（20002 实测）、`qa_t5_legacy.py`（legacy 回滚实测，端口 8014）
- 结果文件：`_qa_t5_verify.txt` / `_qa_t5_extra.txt` / `_qa_t5_legacy.txt`
- 全量测试记录：`_qa_t5_full.txt`（355 passed）

**结论：T5 可验收；T1~T4（BC 报告）+ T5（本报告）全部通过，路由 NoOne。**
