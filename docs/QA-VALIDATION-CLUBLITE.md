# QA-VALIDATION-CLUBLITE：击球帧校正（impact_refiner）独立复验报告

> QA 独立复验 · 严过关（Yan）· 复验日期 2026-08-17（第 1 轮）+ 2026-08-17（第 2 轮回归）
> 环境：`E:\project\golf\.tools\python312\python.exe`（Python 3.12.9）/ MediaPipe 0.10.14 legacy
> 结论先行（第 1 轮）：**工程师声称的"9/9 校正有效、face-on 3/3、369 测试全绿、delta 表"全部复现属实；但代码审查 + 端到端实测发现 1 个 P1 用户可见缺陷（reanchor 后送杆截图错帧，6/9 校正段受影响），路由给 Engineer。**
> 路由判定（第 1 轮）：**Engineer（software-engineer）** —— P1 需修复后回归。
> 结论先行（第 2 轮回归）：**P1 修复验证通过** —— 送杆图==真实送杆帧（正面3/DTL-4e8d0d7e/合成 pipeline 实测逐字节 True）、opens=1、delta 表与 v1 逐位一致、371 测试全绿（连跑 2 遍）、并集解码 max=26 帧无内存风险。**最终路由：NoOne（全部通过，可交付）**，附 4 条 P2 建议。详见文末「第 2 轮回归」小节。

---

## 1. 测试结果

| 项目 | 独立复验结果 | 工程师声称 | 判定 |
|---|---|---|---|
| 全量 pytest | **369 passed / 0 failed**（连跑 2 遍：16.86s / 19.91s，稳定） | 369 passed | ✅ 属实 |
| 新增 20 用例 | 连跑 3 遍：20/20 通过（12.85~14.12s），无 flaky | 20 新增 | ✅ 属实 |
| 渲染逐字节断言 | `TestRendererMarker::test_marker_off_byte_identical` PASSED（单独跑取证） | DRAW_MARKER=False 逐字节一致 | ✅ 属实 |
| 真实视频探针（11 段） | delta 表与工程师声称**逐段完全一致**（见 §3） | 9/9 G1，delta∈[+1,+8]，mean +5.56 | ✅ 属实 |
| face-on 专项 | 正面1/2/3 = +1/+8/+4，3/3 校正有效且后移 | 3/3 且方向正确 | ✅ 属实 |
| 解码趟数 | 11 段全部 opens=1（probe 每段 reset stats）；窗口 8~11 帧 ≤12 | opens=1 | ✅ 属实 |
| 墙钟增量 | 最差 c6f67f38 refine 块 0.714s（含 8 事件帧+窗口 grab）；基线 grab≈0.33s → 增量≈0.38s < 0.5s | +0.433s | ✅ 属实（同量级，机器方差内） |
| 残缺视频 | 087d40a0 / 707fb04a 仍 NO_SWING（与 VALIDATION-A 一致，非 regression） | 同 | ✅ 属实 |
| 边界/错误路径 | 独立脚本 9/9 通过（全黑/静止/超短/异常分辨率/开关/缺帧/非法机位/窗口钳制/reanchor 非法输入） | — | ✅ 新增覆盖 |
| CLUBLITE_ENABLED=False 全链路 | 独立 e2e：8 阶段 success，opens=1，无校正 warning | 回退开关 | ✅ 属实 |
| 端到端 metrics+risk+render | 正面1 指标与声称逐项一致（hip_open 18.4 / shoulder_square 22.0 / pelvis_shift 24.5；RISK-006/017）；DTL-4e8d0d7e spine_tilt 20.7 critical_high、RISK-006/014、WARN_IMPACT_REFINED 触发 | 同 | ✅ 属实 |

---

## 2. 代码审查发现（P0/P1/P2）

### P0：无

无崩溃、无数据丢失、无主链路阻断。模块级 try/except（`impact_refiner.py:797-799`）确实兜住全部异常（G0 不冒泡）；`refine_impact` 入口 `CLUBLITE_ENABLED` 检查在 try 块内（`impact_refiner.py:547-549`）。

### P1：reanchor 后 follow_through 帧不在共享解码集 → 送杆截图内容错帧

- **文件+行号**：`backend/app/pipeline.py:160-200`（step4a：先 decode(event ∪ window)，再 reanchor，再 trim）；`backend/app/renderer.py:218-243`（缺失帧静默 fallback 到最近已解码帧）。
- **机理**：pipeline 在 reanchor **之前**用「原 8 事件帧 ∪ 校正窗口」解码。`reanchor_impact` 重跑 `locate_intermediate` 后，⑦ follow_through 可能移到窗口之外的新帧（⑤ 下杆未变、①②③ 锚定 top/addr 不变）。trim 后该帧缺失，renderer 的兜底（本为"视频提前结束"设计）用 `last_bgr`（= 最后成功解码帧，实测为 **finish 帧**）渲染，**标注帧号却为 f<new_ft>** → 送杆截图显示收杆画面。
- **实测证据**（`backend/_probe_out/qa_p1_ft_render.py` / `qa_p1_ft_content.py`，正面3）：
  - `render fallback for frames: [83]`（FT 83 不在解码集）
  - FT pipeline 图 == render(frame101=finish) **逐字节 True**；≠ frame83 / frame74。
- **影响面**：6/9 校正段（正面3、DTL-470057ac、DTL-4e8d0d7e、DTL-c6f67f38、VID-1446d1b9、VID-a4fba3d2）。不影响指标/风险/impact 帧号（metrics 用 frames+events 而非解码图）；仅送杆（⑦）截图内容错误，用户可见。
- **为何测试没抓住**：合成视频无杆/球，refine 不触发 reanchor（实测合成 pipeline impact 保持 51）；既有测试只断言"8 张文件存在 + 体积 > 1024"，不校验内容正确性。
- **修复建议**（opens ≤2 预算内均可）：① reanchor（纯函数，无 IO）可在 grab 前先跑一次确定最终事件帧集，再统一 decode（event_frames_final ∪ window），保持 opens=1；或 ② reanchor 后对缺失事件帧补一次 `grab_frames`（第 2 趟，opens≤2 达标），merge 后再 trim。推荐 ①。

### P2-1：M2 tie-breaker 与注释/设计文档矛盾（逻辑方向可疑）

- **文件+行号**：`backend/app/impact_refiner.py:723-734` vs docstring `:713` 与 `:410`。
- **内容**：设计写「平票时优先 M2 杆头**最低点**」（y 最大=最贴地），代码 `shaft_ys[cand] < shaft_ys[best_offset]` 却选 **y 更小**（杆头更高/更不贴地）的候选。
- **影响**：仅分数完全相等（<1e-9）时生效，误差 ≤1 帧，不破坏 delta 预算；但方向与文档相反，需工程师确认意图（改代码选 max，或改注释）。另 `:719-722` 首循环为冗余死代码（与 `max` 结果重复），建议删除。

### P2-2：config §8b 常量数口径不符

- 任务/报告声称「15 常量」，实际 `CLUBLITE_*` 13 个 + `WARN_IMPACT_REFINED` 1 个 = **14 个**（`config.py:395-432`）。非功能缺陷，文档口径问题。

### P2-3：probe_clublite_full.py 的 opens 输出为累计值（探针产物问题）

- `backend/_probe_out/probe_clublite_full.py` 未在每段前 `frame_reader.reset_stats()` → DTL 段打印 opens=2 实为「正面1 的 1 + DTL 的 1」累计。
- 影响 `docs/VALIDATION-CLUBLITE.md §11.3` 的归因错误：报告称 DTL opens=2 是「渲染 fallback 开视频」，**实际每段 opens=1**（renderer 用共享 frames_bgr 不再开视频；fallback 仅用 last_bgr，不开 VideoCapture）。主链路解码趟数=1 的结论正确，但原因描述不实。

### P2-4：VALIDATION 报告盲区

- `docs/VALIDATION-CLUBLITE.md §5/§11` 只验证「8 张文件存在」，未校验截图**内容**帧正确性 —— 正是 P1 的藏身处。建议 VALIDATION 增补「reanchor 后所有事件帧 ∈ 解码集」断言。

---

## 3. 独立 delta 表（与工程师声称对照）

复跑 `probe_clublite.py --tag qa_round1`（11 段真实视频，每段独立 reset stats）：

| # | 视频 | 机位 | old | new | **delta（独立）** | 工程师声称 | method | conf | ball | opens | 单调性 |
|---|---|---|----:|---:|---:|---:|---|---|---:|---:|---|
| 1 | 正面1 | face_on | 37 | 38 | **+1** | +1 ✅ | motion+shaft | 1.00 | False | 1 | True |
| 2 | 正面2 | face_on | 284 | 292 | **+8** | +8 ✅ | motion+shaft | 1.00 | False | 1 | True |
| 3 | 正面3 | face_on | 70 | 74 | **+4** | +4 ✅ | motion+shaft | 0.93 | False | 1 | True |
| 4 | DTL-087d40a0 | dtl | — | — | NO_SWING | NO_SWING ✅ | — | — | — | — | — |
| 5 | DTL-0bb16a97 | dtl | 189 | 197 | **+8** | +8 ✅ | motion+shaft | 1.00 | False | 1 | True |
| 6 | DTL-470057ac | dtl | 98 | 104 | **+6** | +6 ✅ | motion+shaft | 0.99 | False | 1 | True |
| 7 | DTL-4e8d0d7e | dtl | 235 | 243 | **+8** | +8 ✅ | motion+shaft | 1.00 | False | 1 | True |
| 8 | DTL-707fb04a | dtl | — | — | NO_SWING | NO_SWING ✅ | — | — | — | — | — |
| 9 | DTL-c6f67f38 | dtl | 177 | 180 | **+3** | +3 ✅ | motion+shaft | 0.88 | False | 1 | True |
| 10 | VID-1446d1b9 | dtl | 33 | 40 | **+7** | +7 ✅ | motion+shaft | 1.00 | False | 1 | True |
| 11 | VID-a4fba3d2 | dtl | 163 | 168 | **+5** | +5 ✅ | motion+shaft | 0.92 | False | 1 | True |

- **切分成功 9/11；G1 校正有效 9/9；delta 全部 ∈ [+1,+8]，mean=+5.56，正移 9/9，∈[-2,+12] 比例 100%** —— 与工程师声称**逐段一致**。
- 窗口帧数 8~11（≤12 设计线 ✅）；解码帧 10~13；reanchor 后事件单调性全部 True（阶段顺序未破坏 ✅）。
- 全部 method=motion+shaft（M2 杆身验证在真实视频稳定命中 ✅）。

---

## 4. 边界与错误路径（独立脚本 `backend/_probe_out/qa_boundary.py`，9/9 通过）

| 场景 | 结果 |
|---|---|
| 全黑帧视频 → refine | G0 available=False，不崩 ✅ |
| 静止无运动视频 → refine | G0 ✅ |
| 超短视频（3 帧） | 不崩，返回 ImpactRefineResult ✅ |
| 异常分辨率（5×5） | 不崩，G0 ✅ |
| CLUBLITE_ENABLED=False | refine G0 且 `frame_reader.stats()["opens"]==0` ✅ |
| 共享解码缺候选帧（frames_bgr 部分缺失） | G0 不崩 ✅ |
| 非法机位（AUTO） | G0 ✅ |
| 窗口规划 back/fwd 极大/极小 | 钳制合法，不越界 ✅ |
| reanchor_impact 非法输入（空 frames/events、越界） | 返回 None，不崩 ✅ |

另：CLUBLITE_ENABLED=False 全链路 e2e（独立脚本 `qa_disabled_e2e.py`）：8 阶段 success、opens=1、无校正 warning ✅。

---

## 5. 对照设计文档验收标准（ARCHITECTURE-v3-clublite.md §6.2 / §7）

| 验收标准 | 结果 |
|---|---|
| #1 ≥5/9 段 delta>0 且截图确认杆头在球/地面线 | 9/9 delta>0 ✅；截图内容正确性受 P1 影响（送杆图），**其余 7 张（含 impact）正确** ⚠️ |
| #2 全部 delta ∈ [-2,+12] | ✅ 实测 ∈ [+1,+8] |
| #3 无 AnalysisError / 阶段顺序破坏 | ✅ 无 AnalysisError，monotonic_after 全 True |
| #4 pytest 349+ / opens ≤2 / 墙钟增量 <0.5s | ✅ 369 passed（2 遍）；opens=1；增量≈0.38s |
| #5 抽查 impact 指标量纲 | ✅ 正面1 三项正常；DTL spine_tilt 20.7 仍 critical_high，量纲合理 |
| §7.1 R1~R6 缓解 | ✅ 全部有对应机制（运动峰→贴地/球点约束；face-on→ROI 收窄+M2 权重；腿脚→ROI 上边界；低光→G0；指标变化→回归抽查；网笼→唯一高置信） |
| §7.2 Q1~Q5 | ✅ Q1 face-on 3/3；Q2 球不可见按设计（9/9 ball=False 属预期，报告 §7 有交代）；Q3 顶盖 12；Q4 WARN_IMPACT_REFINED 已加且实测触发；Q5 前端展示留 PM |

---

## 6. 智能路由判定

**Send To: Engineer（software-engineer）** —— P1 为源码缺陷（pipeline step4a 集成点），非测试缺陷；需修复后做第 2 轮回归。

修复要求（回归验收标准）：
1. reanchor 后 8 个最终事件帧必须全部在 `frames_bgr` 中（opens 仍 ≤2）；
2. 正面3 / DTL-4e8d0d7e 送杆图内容 == 帧 <new_ft>（逐字节对照真实帧渲染）；
3. 全量 pytest 仍 369 全绿；delta 表不回归；
4. （建议）新增回归测试：构造 reanchor 后 ⑦ 移出窗口的用例，断言 FT 图内容正确。

---

## 7. 遗留清单（修复后仍建议跟进）

1. **P2-1** tie-breaker 方向与文档矛盾（impact_refiner.py:728-731），请工程师确认意图后改代码或注释。
2. **P2-2** config §8b 常量数口径（14 vs 声称 15），文档/汇报口径修正。
3. **P2-3** probe_clublite_full.py 每段前补 `frame_reader.reset_stats()`；VALIDATION §11.3 的 opens=2 归因更正（实为探针累计，每段=1）。
4. **P2-4** VALIDATION 增补「reanchor 后事件帧 ∈ 解码集」断言，防 P1 类回归。
5. 球检测 9/9 False：设计 Q2 容忍，非 bug；如需提升可调 `CLUBLITE_BALL_PARAM2`/半径范围（本期不调）。

---

## 8. 复现命令

```bash
PY=E:/project/golf/.tools/python312/python.exe
cd E:/project/golf/backend
$PY -m pytest tests -q                                   # 369 passed（2 遍复验）
$PY _probe_out/probe_clublite.py --tag qa_round1         # 独立 delta 表（与报告 §3 一致）
$PY _probe_out/probe_clublite_full.py                    # metrics+risk+render e2e
$PY _probe_out/qa_boundary.py                            # 边界/错误路径 9/9
$PY _probe_out/qa_disabled_e2e.py                        # 回退开关全链路
$PY _probe_out/qa_p1_ft_render.py / qa_p1_ft_content.py  # P1 错帧实证（正面3）
```

---

## 9. 第 2 轮回归（P1 修复验证，2026-08-17）

> 修复方案：**前置预计算 `plan_reanchor_frames`**（`impact_refiner.py:165-203`）——对窗口内每个候选下标跑一遍纯函数 `reanchor_impact`，收集全部可能产出的事件帧并入解码集，grab 一次（opens=1）。pipeline.py:164-173 接入。

### 9.1 回归结果总表

| 验收点 | 独立复验结果 | 判定 |
|---|---|---|
| 送杆图==真实送杆帧 | 正面3：FT 83，送杆图==render(真帧 83) **逐字节 True**，≠收杆帧；DTL-4e8d0d7e：FT 269，**True**；合成 pipeline._run 集成：FT 59，**True** | ✅ P1 已修复 |
| 无渲染 fallback | 9/9 校正段 `all_events_decoded=True`，`missing from frames_bgr: []` | ✅ |
| opens ≤ 2 | 11 段探针 opens 全=1；pipeline e2e opens=1 | ✅ |
| delta 表不回归 | 与 v1 **逐位一致**：正面1 +1 / 正面2 +8 / 正面3 +4 / 0bb16a97 +8 / 470057ac +6 / 4e8d0d7e +8 / c6f67f38 +3 / 1446d1b9 +7 / a4fba3d2 +5（9/9 G1，mean +5.56，∈[+1,+8]） | ✅ |
| pytest | **371 passed / 0 failed**（连跑 2 遍：16.80s / 23.39s，RC=0，无 flaky） | ✅ |
| 并集解码内存 | union 19~26 帧（max=26，与声称一致）；720×1280×3 ≈ 72MB 峰值，refine 后立即裁剪回 8 帧 | ✅ 无内存风险 |
| tie-breaker 语义 | 已改为 `shaft_ys[cand] > shaft_ys[best_offset]`（选 y 更大=杆头更贴地，与注释一致，impact_refiner.py:776）；11 段 delta 与 v1 完全一致 → 语义修正未引入回归 | ✅ |
| renderer last_bgr | 未改动；fallback 仅在「事件帧缺失于 decoded」触发，现解码集已全覆盖 → 仅剩「视频提前结束」合法路径 | ✅ |

### 9.2 plan_reanchor_frames 正确性审查

- **数学保证成立**：refine 最终采纳的 `new_array_index = cand_indices[best_offset]`，`best_offset ∈ candidates`，`candidates` 是 `cand_frames` 的下标；`plan_reanchor_frames` 遍历**同一个** `cand_frames` 并对每个候选跑 `reanchor_impact`，其 8 事件帧并入并集 → 实际命中候选的 reanchor 输出必在并集内。reanchor 是纯函数（无 IO），同一输入同一下标结果确定。
- 边界：候选下标命中 `new_idx == impact.array_index` 时 `reanchor_impact` 返回原 8 事件帧（已在解码集）；冲突候选（返回 None）被跳过，不影响（实际采纳候选必然 reanchor 成功）。
- 实测佐证：9/9 段 `all_events_decoded=True`；正面3 union=22、4e8d0d7e union=26 均覆盖 FT 真帧。

### 9.3 新回归用例有效性评估

- `test_plan_reanchor_frames_covers_all_candidates`（test_impact_refiner.py:475）：对每个候选下标 reanchor 并断言其 8 事件帧 ∈ 解码并集 —— **强断言**，直接锁住"解码集覆盖 reanchor 全部可能输出"这一数学不变量。
- `test_pipeline_flow_no_render_fallback`（test_impact_refiner.py:503）：模拟 pipeline 解码顺序（event∪window∪possible）→ refine → reanchor → 断言 8 事件帧全在 frames_bgr —— **强断言**，能抓住"事件帧未解码"类 bug。
- ⚠️ 局限（P2 建议）：合成杆+球视频上 reanchor 只移 impact（51→57），**FT 未移动**（57→59→74），故该用例的 FT 专用分支（`if new_ft != old_ft`）在本 fixture 上是死代码；真正触发 P1 的是真实视频（FT 后移）。建议后续补一个强制 FT 移动的 fixture，或把本 QA 的 `qa_r2_pipeline_integration.py`（走真实 pipeline._run + 带杆视频）沉淀进测试套件。

### 9.4 第 2 轮新增 P2（不阻塞交付）

1. `impact_refiner.py:788` `shaft_lowest_index` 诊断字段仍取 `min(shaft_ys)`（y 最小=杆头最高），与决策 tie-break（取 max）语义相反；仅诊断字段，不影响任何决策，建议统一。
2. 无单测守卫「pipeline.py 的 grab_frames 调用确实包含 possible 帧」——若将来误删 `| set(_possible_frames)`，现有单测仍绿（单测自己构造并集）。已用探针 `qa_r2_pipeline_integration.py` 覆盖，但建议沉淀为正式回归。
3. VALIDATION-CLUBLITE.md §5 仍只断言「8 张文件存在」；本报告 §2 的 P1 已证伪该盲区，建议增补内容帧断言。
4. config §8b 常量数口径（14 vs 声称 15）未修，纯文档问题。

### 9.5 第 2 轮结论

**P1 修复验证通过，全部验收点达标，最终路由判定：NoOne（可交付）。**

---

*QA 复验完毕（第 1 轮 + 第 2 轮回归）。最终结论：P1（reanchor 后送杆截图错帧）已由 `plan_reanchor_frames` 并集解码修复并经独立实测验证；371 测试全绿、delta 表与 v1 逐位一致、opens=1、送杆图==真实送杆帧。4 条 P2 建议（诊断字段语义、pipeline 集成回归测试沉淀、VALIDATION 断言增强、常量口径）列入遗留清单，不阻塞本次交付。*
