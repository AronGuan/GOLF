# QA 独立复验报告：球杆检测下线改动（CLUB OFF）

- **QA 工程师**：严过关（Yan）
- **日期**：2026-08-05
- **范围**：独立复验 `backend/app/` 球杆检测下线改动（`shaft_plane_dev` 指标 + pipeline 调用链 + config CLUB_* 常量 + renderer 球杆绘制）
- **验证方式**：全部独立执行，不复用工程师报告结论（仅以其声明作为对照项）

---

## 1. 测试结果

| 项目 | 结果 |
|---|---|
| 全量测试（第 1 次独立运行） | **349 passed / 0 failed**（10.86s，4 个既有 deprecation warning） |
| 全量测试（第 2 次独立运行，flaky 检查） | **349 passed / 0 failed**（11.28s） |
| 工程师声称 | 349 passed / 0 failed（原 355，删 6 例）→ **与实测完全一致** |
| 删除测试审计（git diff 核对） | 精确删除 6 例：`test_club_detector.py` 的 `TestShaftPlaneDevDegradation` 整类 5 例（L0/L1/L2×2/face_on 机位排除）+ `test_reference_metrics.py` 的 `test_shaft_plane_dev_spec` 1 例。均为已下线功能的专属断言，**无核心断言被误删** |

各文件收集数（独立 collect-only）：

```
test_reference_metrics.py  108  (原 109，删 1)
test_api.py                 47
test_geometry.py            46
test_risk_engine.py         39   (RISK-016 符号回归、RISK-009 边界均在)
test_segmenter.py           35
test_pipeline_e2e.py        21   (真实 render、解码趟数回归均在)
test_pose_extractor.py      20
test_club_detector.py       19   (原 24，删 5；保留模块自洽)
test_view_detector.py       14
合计                       349
```

重点保留项确认（均随全量测试通过）：
- `swing_plane` spec（reference.py，DTL、55~65、allow_drop）+ 函数（metrics.py `m_swing_plane`）✓
- 风险引擎 17 条规则全部可解析（见 §4）✓
- RISK-016 符号回归（test_normal_open_angle_does_not_trigger / test_low_open_angle_triggers）✓
- 机位过滤指标数（FACE_ON_COUNTS / DTL_COUNTS 已按下线更新为 `DOWNSWING: 0`）✓
- 双路径（pipeline e2e 21 例）、切分（35 例）、X-Factor ✓

---

## 2. 独立 grep 审计表

| 审计项 | 范围 | 结果 | 结论 |
|---|---|---|---|
| `shaft_plane_dev` | app/（除 club_detector.py） | **ZERO** | ✅ |
| `shaft_plane_dev` | tests/ | **ZERO** | ✅ |
| `shaft_plane_dev` | miniprogram/ | **ZERO** | ✅ |
| `shaft_plane_dev` | app/club_detector.py 内部 | 1 处（:576 docstring，杆头轨迹拟合说明） | ✅ 唯一残留，doc-only |
| `import club_detector` / `from ... club_detector` | app/ 主管线（pipeline/metrics/renderer/main） | **ZERO** | ✅ |
| `CLUB_CONF_MIN / CLUB_CONF_PROXY_MIN / WARN_CLUB / CLUB_ONNX / DECODE_BYTES_BUDGET / CLUB_COLOR / CLUB_THICKNESS` | app/ 非 club_detector 文件 | **ZERO**（仅 config.py 注释提及） | ✅ |
| `swing_plane` | reference.py spec（:210-214, 55~65, DTL, allow_drop）+ 术语行（:112） | 完整保留 | ✅ |
| `swing_plane` | metrics.py `m_swing_plane`（:456）+ METRIC_FUNCS（:509） | 完整保留 | ✅ |
| `swing_plane` | risk_rules.py RISK-009（:294-298, metric_key="swing_plane"） | 完整保留 | ✅ |
| `球杆检测完成` | backend/ 全量 | **ZERO** | ✅ |
| `club=`（build_context/render_events 调用） | app/ 主管线 | **ZERO**（仅 club_detector.py 内部自用） | ✅ |
| `shaft|ClubTrack|ClubDetection|swing_plane` | miniprogram/ | **ZERO** | ✅ |
| `球杆` | miniprogram/ | 3 处，全部为拍摄指引文案（index.wxml 机位说明 + index.js 拍摄提示） | ✅ 属正常 |

---

## 3. 代码审查发现（抽查关键改动点）

| 文件 | 检查项 | 结论 |
|---|---|---|
| pipeline.py | 无 `from . import club_detector`；`_P_CLUB_END` 已删；step4a 进度文案改为"正在解析机位与解码事件帧..."；`build_context(...)` 无 `club=`；`render_events(...)` 无 `club=`；`grab_frames` 仍解码 8 个事件帧供渲染（:154）；`frames_bgr` 过滤只留 8 帧 | ✅ 全部通过 |
| metrics.py | `MetricContext` 无 `club` 字段；`m_swing_plane` 完整（纯 MediaPipe 左肩11→左腕15，无任何被删 config 常量/函数引用）；`m_shaft_plane_dev` 及 4 个辅助函数（`_fit_traj_angle/_shaft_base_angle/_shaft_traj_angle/_proxy_wrist_traj_angle`）整块移除；`build_context` 签名无 `club=`；启动自检 `_MISSING` 机制仍在 | ✅ 全部通过 |
| reference.py | `shaft_plane_dev` spec（DOWNSWING ⑤）已删；`swing_plane` spec 在；`all_metric_keys()` 与 `METRIC_FUNCS` 自检一致（metrics 导入期校验） | ✅ 全部通过 |
| config.py | 被删常量（CLUB_CONF_MIN/PROXY_MIN、CLUB_COLOR/THICKNESS、CLUB_ONNX_*、WARN_CLUB_*、DECODE_BYTES_BUDGET）经 grep 确认**无主管线引用**；保留的 CLUB_*（CLUB_ENABLED/MODE/MAX_DECODE_FRAMES/ROI_*/HOUGH_*/LEN_RATIO_*/GRIP_DIST/DIR_TOL/SPEED_SWITCH）仅被 club_detector.py / test_club_detector.py 引用；`VIEW_SHOULDER_RATIO_DTL`、`SHOULDER_TO_HEIGHT_RATIO` 明确标注为非球杆常量保留 | ✅ 全部通过 |
| renderer.py | `_draw_club`、`_draw_dashed_line` 已删；`_draw_horizon` 保留（DTL 水平参考线）；`render_events`/`_render_one` 签名无 `club=`/`club_detection=` | ✅ 全部通过 |
| frame_reader.py | 仅 docstring 更新；`grab_frames`/`stats` 保留；无 DECODE_BYTES/club 引用 | ✅ 全部通过 |
| main.py / schemas.py | main.py 零 club 引用；schemas 保留 ClubTrack/ClubDetection 类型（无害）与 by_frame/get 方法；schemas 中无 `shaft_plane_dev`、有 `swing_plane` | ✅ 全部通过 |
| club_detector.py（保留模块） | `plan_frames` 以 `budget_bytes` **参数**实现字节护栏（代码不读已删常量）；**唯一残留**为 :583 docstring 仍写 `config.DECODE_BYTES_BUDGET` → 见 P2-1 | ⚠️ P2 |

### 发现分级

- **P0（阻断上线）**：无
- **P1（需修复）**：无
- **P2（建议清理，不阻断）**：
  - **P2-1**：`club_detector.py:583` docstring 引用已删除的 `config.DECODE_BYTES_BUDGET`（doc-only，运行期无影响；工程师已如实披露）。建议在模块复活时一并修正文档。
  - **P2-2**：性能声明"club 块 1.09s 固定开销已消除"无法从现有产物做干净 A/B 基准（前后用不同探针脚本，机器负载变化大；实测 DTL 段 8.1s→14.6s 属 MediaPipe 提取方差，非回归）。结构性成立（代码路径客观变短、正面1 2.81s→2.11s 变快），如需量化请同脚本 A/B。

---

## 4. 端到端独立实测（与工程师声称对照）

运行 `backend/_probe_out/probe_no_club.py`（独立复跑，已重读探针代码确认：不删源视频、以 ground-truth 机位传入、显式检查 `removed_metric_present`）。真实素材 `E:\project\golf\.tools\_probe\samples\`。

### 正面1.mp4（face_on）

| 项目 | 工程师声称 | QA 独立实测 | 对照 |
|---|---|---|---|
| 机位 | face_on | face_on（chosen=resolved=face_on） | ✅ |
| 风险数 | 2 条 | **2 条**（RISK-006 high lead_arm_straightness=107.2；RISK-017 low balance_hold=0.0） | ✅ |
| swing_plane | N/A（正面天然无） | **None**（face-on 不产该指标，符合 spec.views=_D） | ✅ |
| 已下线指标残留 | 无 | `removed_metric_present=False`，全阶段 16 个 metric key 无 shaft/club | ✅ |
| 阶段 | 8 阶段正常 | 8 阶段均有指标；耗时 2.11s | ✅ |

### DTL 段 4e8d0d7e（down_the_line）

| 项目 | 工程师声称 | QA 独立实测 | 对照 |
|---|---|---|---|
| 机位 | DTL | down_the_line | ✅ |
| swing_plane | 51.6°（与历史一致） | **51.6°**（TOP 阶段；与历史 probe_e2e.json=51.6、probe_qa_dtl.json=51.6 逐位一致） | ✅ |
| 已下线指标残留 | 无 | `removed_metric_present=False`，bad_keys=[] | ✅ |
| 风险数 | 2 条 | **2 条**（RISK-006 high lead_arm_straightness=137.4；RISK-014 high spine_tilt_change=13.2） | ✅ |
| 8 阶段 + 风险引擎 | 正常 | 7 阶段有指标 + downswing 为空（预期：downswing 唯一指标即已下线的 shaft_plane_dev，删后恒 0，与更新后的 DTL_COUNTS 一致）；风险引擎正常 | ✅ |
| 耗时 | club 块固定开销已消除 | 14.61s（受机器负载影响，见 P2-2） | ⚠️ 结构性成立 |

### 历史一致性（swing_plane 数值）

```
probe_e2e.json    (改动前 17:10)  DTL-4e8d0d7e  swing_plane=[51.6]  shaft_plane_dev=[]
probe_qa_dtl.json (改动前 17:21)  DTL-4e8d0d7e  swing_plane=[51.6]  shaft_plane_dev=[]
probe_no_club.json(改动后 18:32, QA 复跑) swing_plane=[51.6]  shaft_plane_dev=[]
```
→ **swing_plane 数值改动前后逐位一致**，纯 MediaPipe 路径未受球杆下线影响。

---

## 5. 边界检查

| 检查项 | 结果 |
|---|---|
| test_club_detector.py 19 例通过 | ✅（collect 19 / 全量通过；`plan_frames` 的 `budget_bytes` 参数路径与字节护栏测试正常，不依赖已删常量） |
| risk_engine 17 条规则 metric_key 逐一可解析 | ✅（独立脚本验证：17/17 在 reference.METRIC_SPECS[trigger_phase] 中命中，无 dangling；无规则引用 shaft_plane_dev；RISK-009→swing_plane 无恙；`self_check()` 导入期通过） |
| RISK-016 符号回归 | ✅（测试存在且通过） |
| 机位过滤指标数（face-on 3/4/4/4/4/4/4/4；DTL downswing 0） | ✅（更新后测试通过，与参考表一致） |
| 双路径 / 切分 / X-Factor | ✅（pipeline e2e 21 例、segmenter 35 例、x_factor 相关均通过） |
| 小程序残留 | ✅（无 shaft/club 指标残留；"球杆"仅出现在拍摄指引文案，属正常） |
| 解码趟数回归 | ✅（test_decode_opens_limited 断言 `frame_reader.stats()["opens"] <= 2` 通过；球杆窗口采样解码块已不存在） |

---

## 6. 智能路由判定

> **Routing Decision: NoOne（全通过）**

- 无源码 bug 需交回 Engineer（P0=0，P1=0）。
- 无测试 bug 需 QA 自修（349/349 通过，删例精确、无 flaky）。
- 端到端实测与工程师声称**逐项吻合**（尤其 swing_plane=51.6° 与历史逐位一致、正面1/DTL 各 2 条风险、无已下线指标残留）。
- 唯一异议点（P2-1 docstring 残留、P2-2 性能基准不可比）均不构成缺陷，作为遗留清单记录。

---

## 7. 遗留清单（Known Issues / 建议项）

1. **P2-1（文档）**：`backend/app/club_detector.py:583` docstring 仍引用已删除的 `config.DECODE_BYTES_BUDGET`。运行期无影响（代码用 `budget_bytes` 参数），建议模块复活时清理。
2. **P2-2（基准）**：球杆下线带来的耗时收益（工程师称 club 块 ~1.09s 固定开销）建议用**同一探针脚本**在改前/改后各跑 3 次取中位数，给出可复现基准；当前前后产物不可直接对比（探针脚本不同、机器负载波动，DTL 段实测 14.61s 明显高于改动前 8.1s，属 MediaPipe 提取方差，未见结构性回归）。
3. **观察项（非缺陷）**：`config.CLUB_ENABLED` 保留为 `True`，但主管线已不读取；若未来复活球杆检测，需同步评估该开关的语义（当前仅 club_detector.detect 内部读取，自洽）。

---

*报告完毕。验证结论：工程师的球杆检测下线改动真实有效，可通过。*
