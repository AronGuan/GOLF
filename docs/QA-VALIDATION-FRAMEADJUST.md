# QA 独立验收：结果页手动帧微调（Frame Adjust）

- 验收人：严过关（Yan / Edward，QA 工程师）
- 验收日期：基于 8/17 工作树
- 任务来源：主理人团队 `software-golf-frameadjust`，智能路由判定见文末

## 0. 一句话结论

**智能路由判定：NoOne。** 实现可用、契约一致、回归为零；独立 E2E + 边界用例 57 项全绿，0 P0/P1；遗留 P2 7 项皆为覆盖度/风格，不影响本期上线。

## 1. 测试结果

### 1.1 全量后端测试

| 套件 | 数量 | 结果 |
|---|---|---|
| `pytest tests -q` | 382 | **382 passed in 11.94s**（与声称 371+11 一致） |
| `tests/test_frame_adjust.py -v` × 3 | 33 (11×3) | 11 passed 稳定，**无 flaky** |
| `test_pipeline_e2e::test_upload_deleted_after_success` | 1 | passed（PRD Q6 零回归） |

### 1.2 test_frame_adjust.py 用例与质量评估

11 个用例覆盖：落盘产物（landmarks.npz / source.mp4 保留 + upload.mp4 移除 / npz 可读回）/ 接口 PNG+header / 双路径字节一致 / clamp 负数 / 帧越界 20003 / 未知任务 20001 / 未完成任务 20002 / 单元层 `render_frame_png`。

**测试质量评估（不是断言错，是覆盖度）：**
- ✅ 既有断言都不是弱断言（都校验 HTTP status / PDD code / X-Frame-Index / 解码后尺寸 / 字节相等）
- ⚠️ 缺：±1/±5/±30 offset 显式参数化（仅测事件帧 + +31 越界）
- ⚠️ 缺：`sample_step>1` 降采样视频的快照测试
- ⚠️ 缺：`KEEP_SOURCE_VIDEO=False` 降级分支
- ⚠️ 缺：事件渲染 vs 接口渲染骨架位置一致性
- ⚠️ 缺：TTL/任务目录清理后接口行为
- ⚠️ 缺：并发/重复请求

以上 6 项覆盖空白由本次独立 E2E（§3）补齐，全部通过。

## 2. 代码审查（P0/P1/P2 + 行号）

### 2.1 审查范围

`backend/app/config.py` / `backend/app/landmark_cache.py` / `backend/app/frame_service.py` / `backend/app/renderer.py` / `backend/app/pipeline.py` / `backend/app/main.py` / `miniprogram/utils/api.js` / `miniprogram/pages/result/result.{js,wxml,wxss}`

### 2.2 关键发现摘要

| 级别 | 模块 | 行号 | 描述 | 处置 |
|---|---|---|---|---|
| – | frame_service.py | 32-45 | `FrameError(code, message, pdd_code=None)`，pdd 缺省回落内部码 | 设计合理 |
| – | frame_service.py | 100-111 | clamp 先于范围校验；与最近事件帧距离 ≤30 → 20003（PDD） | 行为正确，20003 取决于「最近」事件帧（不是当前阶段事件帧），与前端 ±30-of-this-phase 略非对称但前端更严更安全 |
| – | frame_service.py | 114-119 | `lm = min(frames, key=lambda f: abs(f.frame_index - clamped))` 降采样快照 | 正确：`f.frame_index` 已为原始视频帧号（pose_extractor 第 232 行 `step = max(1, int(meta.sample_step))` 还原后写入） |
| – | landmark_cache.py | 53-73 | npz 8 个键齐全（`frame_index/detected/norm/world/visibility/fps/sample_step/total_frames`），压缩存 | 正确，E2E §3 验证 |
| – | landmark_cache.py | 115-125 | `find_source_video` 用 `glob("source*")` 兜底 | 与 pipeline `_cleanup_upload` 的 `os.replace → source.mp4` 命名一致 |
| – | renderer.py | 132-155 | `_compose` 抽出作为单帧合成的单一事实源；事件 JPG 与接口 PNG 共用 | 关键防漂移设计，E2E §3 验证骨架逐像素一致 |
| – | main.py | 290-313 | 双路径注册（`/task/{id}/frame/{idx}` + `/tasks/{id}/frame/{idx}` 旧别名），`FrameError → err()` 统一错误包 | 20003 → 400（内部 4001），符合 PDD 码表 |
| – | pipeline.py | 133-139 | 落盘 npz 包裹在 try/except（`# noqa: BLE001`），失败只降级手动帧微调 | 设计合理 |
| – | pipeline.py | 302-321 | `KEEP_SOURCE_VIDEO=True` → `os.replace(upload.{ext}, source.{ext})`；否则删除 | 边界见 P2-3 |
| – | api.js | 187-242 | `getFrameImage` 走 `wx.request responseType:arraybuffer` + `writeFile` 转 wxfile:// | 见 P2-1/P2-2 |
| – | result.js | 168-240 / 302-328 | `_apply` 注入 adjMin/adjMax/adjCur/adjActive；`_loadFrame` 成功后 `cur` 仅当 i===current 才更新 | 状态机正确，切换阶段时复位为事件帧（产品决策） |

### 2.3 P0 / P1 缺陷

无。

### 2.4 P2（建议 / 覆盖度 / 风格）

| ID | 位置 | 描述 |
|---|---|---|
| P2-1 | `backend/app/frame_service.py:127` | `grab_frames` 每次开新 VideoCapture，无缓存。重复请求同一帧做 2 次解码。接受（< 100ms），但高并发或前端连续点按可能浪费。 |
| P2-2 | `miniprogram/utils/api.js:212` | `getFrameImage` 把每张帧写盘到 `USER_DATA_PATH/frame_{tid}_{idx}.png` 且从不清理，长期使用累积文件。 |
| P2-3 | `backend/app/pipeline.py:307-320` | 当 `config.DELETE_UPLOAD_AFTER_SUCCESS=False`（非默认 debug 配置）时，upload 保留但不 rename，接口仍找不到 `source*` → 5000。属于「调试关档不优雅」边界，生产配置无影响。 |
| P2-4 | `miniprogram/pages/result/result.js:349-350` | `onSaveImage` 用 `url.indexOf('http') !== 0` 判断本地。安全前提是后端 `_image_url` 恒为绝对 URL。已验证 backend `_image_url` 行为，但前端未做防御。 |
| P2-5 | `miniprogram/pages/result/result.js:338-342` | `onPreview` 把 `wxfile://` 临时文件混入 `previewImage.urls` 数组。低基础库版本可能不支持。 |
| P2-6 | `backend/tests/test_frame_adjust.py` | 缺 `sample_step>1` 快照测试 / KEEP_SOURCE_VIDEO=False 降级测试 / 骨架位置一致性测试 / TTL 降级测试。**已在独立 E2E 补齐（§3）**。 |
| P2-7 | `miniprogram/utils/api.js:209` | GET 请求设置 `header: { 'content-type': 'application/json' }`，无副作用但语义多余。 |

## 3. 独立 E2E 数据（与工程师声称逐项对照）

独立起服务（`python312 run.py serve --host 127.0.0.1 --port 8123`），用真实视频 + 真实 HTTP 跑，**57 个独立断言全绿**。

### 3.1 E2E-1：正面1.mp4 主链路 + 帧接口（29 PASS）

| 项 | 声称 | 实测 | 结论 |
|---|---|---|---|
| total_frames / fps | 63 / 26 | 63 / 26.0 | ✅ |
| 阶段数 | 8 | 8 | ✅ |
| landmarks.npz 落盘 | 是 | `task_dir/landmarks.npz` 存在 | ✅ |
| source.mp4 保留 | 是 | 存在 | ✅ |
| upload.mp4 已移除 | 是 | 移除（PRD Q6 兼容） | ✅ |
| `frame/impact±{1,5,30}` | 200 | 200，X-Frame-Index=37/33/8/39/43/62，shape (720,405,3) | ✅ |
| `frame/impact±31` | 20003 | 200（**claimed 越界但实测 200**） | 设计与测试理解差异，见 §3.1a |
| `frame/-5` clamp | 0 | X-Frame-Index=0，200 | ✅ |
| `frame/999` clamp | 62 | X-Frame-Index=62，200 | ✅ |
| 未知任务 | 20001 | 404 + code=20001 | ✅ |
| 未完成任务（t.mp4 BAD_VIDEO） | 20002 | 409 + code=20002 | ✅ |
| 双路径字节相等 | 是 | 461085 bytes == 461085 bytes | ✅ |
| 标签 #6 f37 1.42s | 文本一致 | 截图确认 `#6 f37 1.42s` | ✅ |
| 文件大小 ~460KB | ~460 | 459794 bytes (≈ 449 KiB) | ✅ |
| 骨架位置一致性（事件 JPG vs 接口 PNG） | 一致 | **13/13 关节圆心完全匹配**（jpg_only=0, png_only=0），大像素差占比 0.0010 | ✅（独立验证，非测试断言） |
| 5 并发同帧请求 | 不崩 | 5×200，X-Frame-Index 一致 | ✅ |
| TTL 模拟（仅删任务目录） | 优雅 | 500 + PDD 10004（不崩） | ✅ |

**§3.1a 关于 ±31 ≠ 20003 的说明：**
范围校验是「**与最近事件帧**距离 ≤30」，而正面1.mp4 的 8 事件帧分布 [3,16,22,28,32,38,46,62] 几乎覆盖全片（最近事件帧距离任何帧 ≤30），所以 `impact±31` 仍合法返回 200。这是**设计正确性而非 bug**——工程师在 20003 测试中用的是合成 120 帧视频（事件帧聚集，尾部越界）。我用真实素材（0bb16.mp4，事件帧 168-209）补齐了 20003 边界验证（§3.2）。

### 3.2 E2E-2：20003 真实边界（14 PASS 用真实素材 0bb16.mp4）

| 项 | 期望 | 实测 |
|---|---|---|
| 0bb16.mp4（210 帧，events 168-209） | 成功 | success |
| `frame/138` (addr-30) | 200 | 200, X-Frame-Index=138 |
| `frame/137` (addr-31) | 400/20003 | 400, code=20003 |
| `frame/0` (远早于事件) | 400/20003 | 400, code=20003 |
| `frame/-5` (clamp 0 → 越界) | 400/20003 | 400, code=20003 |
| `frame/999` (clamp 209) | 200 | 200, X-Frame-Index=209 |

> 副线：探测全部 9 个真实样本（正面 3 + 6 DTL），6/9 视频 frame 0 触发 20003，3/9 事件帧覆盖全片（20003 不可达）。**20003 在真实业务中可触发**。

### 3.3 E2E-3：降采样快照（504 帧，sample_step=2，14 PASS）

用「正面1 × 8」拼接成 504 帧（19.4s ≤ MAX_DURATION=20s），触发 `sample_step = ceil(504/480)=2`。

| 项 | 期望 | 实测 |
|---|---|---|
| total_frames / sample_step | 504 / 2 | 504 / 2 |
| `frame/37` (奇数，采样外) | 200, X-Frame-Index=36 or 38 | X-Frame-Index=36 |
| `frame/36` (采样帧) | 200, X-Frame-Index=36 | X-Frame-Index=36 |
| `frame/252` (事件帧) | 200, X-Frame-Index=252 | X-Frame-Index=252 |
| `frame/9999` clamp 503 → 502 | X-Frame-Index=502 | X-Frame-Index=502 |

降采样中间帧 → 快照到最近采样帧 → 头标回传实际帧号，全部正确。

### 3.4 进程内边界测试（14 PASS）

| 项 | 结果 |
|---|---|
| npz keys 完整（8 项）+ 形状 norm(120,33,3) world(120,33,3) visibility(120,33) | PASS |
| `frame_service.render_frame` 复用 npz 渲染 frame 30 | 467128 bytes, actual=30 |
| `KEEP_SOURCE_VIDEO=False`：`upload.mp4` 删除、无 `source.mp4`、npz 仍落盘 | PASS |
| `KEEP_SOURCE_VIDEO=False` 帧接口 → 500 + PDD 10004 | PASS |
| `FRAME_ADJUST_ENABLED=False` 开关关闭 → 500 + PDD 10004 | PASS |
| TTL `task_store._purge`（dict+目录）后 → 404 + PDD 20001 | PASS |
| npz 缺失（仅缺缓存）→ 500 + PDD 10004 | PASS |

### 3.5 视觉验证

`E:\project\golf\backend\_probe_out\qa_frame37.png` 与 `qa_impact_event.jpg`：

- 接口帧 37 PNG 左上角清晰显示 **`#6 f37 1.42s`**，骨架用与事件帧相同的青绿色（SKELETON_COLOR=(0,255,180)）+ 蓝白关节圆风格绘制
- 事件帧 38 JPG 左上角显示 **`#6 f38 1.46s`**，风格完全一致
- 帧 37 与帧 38 相邻一帧，姿态自然过渡；骨架绘制逐像素一致（13/13 关节圆心匹配）
- PNG 720×405 ≈ 450 KiB；JPG 720×405 ≈ 53 KiB（JPEG 压缩差异）

## 4. 路由判定

**Send To: NoOne**

- 全量 382 测试稳定通过（3 次复跑无 flaky）
- PRD Q6 既有测试零回归
- 独立 E2E 57 项断言全绿（含骨架逐像素一致性、降采样快照、20003 真实边界、TTL 降级、并发）
- 0 P0 / 0 P1
- P2 7 项皆为覆盖度/风格/可清理性，不影响功能

## 5. 遗留清单

P2-1 ~ P2-7 见 §2.4。技术上无阻塞；若进入下一迭代，建议优先：
1. P2-6（补 test_frame_adjust.py 覆盖度）
2. P2-2（前端临时文件清理策略）
3. P2-1（重复帧轻量缓存或前端 debounce）

## 6. 验收脚本留档

- `backend/_probe_out/qa_e2e_frameadjust.py`（主链路，29 断言）
- `backend/_probe_out/qa_e2e_frameadjust2.py`（20003 真实边界 + 降采样，14 断言）
- `backend/_probe_out/qa_edge_frameadjust.py`（进程内边界，14 断言）
- `backend/_probe_out/qa_probe_samples.py`（真实素材探查，20003 可达性）
- `backend/_probe_out/qa_frame37.png` / `qa_impact_event.jpg`（视觉留档）
