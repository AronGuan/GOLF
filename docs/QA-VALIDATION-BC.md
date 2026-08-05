# QA 验证报告 —— T1~T4 后端核心改动独立验证（BC 增量）

- **QA**：严过关（software-qa-engineer）
- **日期**：2026-08-05
- **范围**：T1 schemas/config · T2 risk_rules/risk_engine · T3 reference/metrics/view_detector · T4 pipeline/club_detector/renderer
- **验证方式**：不依赖工程师测试，独立审查 + 独立脚本 + 真实视频端到端复跑 + 与工程师声称逐项对照

---

## 1. 测试执行结果

| 项目 | 结果 |
|---|---|
| 全量测试（第 1 次，09:14） | **331 passed / 0 failed**，8.83s（与工程师声称一致） |
| 全量测试（第 2 次，09:23） | **355 passed / 0 failed**，9.60s |
| 有无 flaky | **未观察到**。两次全量均绿；真实视频探针的 2 段 NO_SWING 失败为确定性失败（多次复跑结果一致），非 flaky |
| 测试数量变化说明 | ⚠️ 验证期间 software-engineer-2 正在做 T5（main.py），第 2 次跑时 test_api.py 新增 24 条用例（331→355），**无回归** |

> ⚠️ **并行状态提示**：`backend/app/main.py` 在本次验证期间被 T5 实时修改（git diff 152 行）。凡涉及 main.py / test_api.py 的结论均以「当前快照」为准，T5 完成后需复测。本报告的验证重心（风险引擎/指标/机位/球杆降级）与 main.py 无关，不受影响。

---

## 2. 代码审查发现（按文件）

### 2.1 `risk_rules.py` —— P0/P1：无

- ✅ 17 条规则齐全，rule_id 唯一，conditions 非空，operator/logic 白名单自检（导入期 RuntimeError）。
- ✅ **enabled=True 恰 10 条**（001/002/005/006/007/010/011/014/016/017），**enabled=False 恰 7 条**（003/004/008/009/012/013/015）。
- ✅ **缺文案 7 条的 trigger_template / suggestions / manual_excerpt 经程序化断言确实为空/None，非研发编造**。
- ✅ 全部 17 条 metric_key 命中 `reference.METRIC_SPECS[trigger_phase]`；每条规则 views ⊆ spec views（自检第 2、3 项通过）。
- ✅ 与 PRD §3.3 逐条抽查一致（详见 §4.3），含：
  - RISK-009 双区间 `value<50 or value>70`、DTL 专属 ✓
  - RISK-011 三分支文案（<156 弯曲过度 / ≥156 过于伸直）✓
  - RISK-016 metric_key=`shoulder_turn` 但语义为开放角（数据层 fn_key 拆除）✓
- 🟡 P2：RISK-002 `manual_page` PRD 自身冲突（§5.2.2 写 P6 / §7.3 写 5），代码取 "6"，需 PM 确认。
- 🟡 P2：RISK-014 manual_excerpt（肋部骨折/脊柱侧屈）与风险名（Early Extension）语义弱相关——PRD §3.3 已标注疑似引错，代码忠实抄录，非代码缺陷，待用户确认（架构 §10 #B4）。

### 2.2 `risk_engine.py` —— P0/P1：无

- ✅ **`_SafeDict` 无 eval**：全源码 inspect 无 `eval(` / `exec(` / `compile(`；未知占位符原样保留；恶意模板（`__import__('os').system(...)`）被原样返回、未执行。
- ✅ **引擎零特判**：源码中无 `shoulder_open` 等任何 RISK-016 符号特判，符号陷阱在数据层 `fn_key` 拆除（架构 §4.6 三层防线第 1 层落实）。
- ✅ `self_check()` 四类检查导入期执行：enabled=True 必须有 trigger_template + suggestions；metric_key 必须在 spec；views ⊆ spec views；任一失败即 RuntimeError。
- ✅ 单条规则求值失败 `logger.exception` 吞掉，绝不让引擎异常冒泡到 pipeline。
- ✅ 开关链路：`RISK_ENGINE_ENABLED=False` 一键关停、`RISK_RULES_FORCE_ENABLE` 灰度强开（当前为空集）。
- ✅ 空 phases / 空 metrics / 指标缺失 / NaN 值均不抛异常、不误触发。

### 2.3 `reference.py` —— P0/P1：无

- ✅ **⑦ fn_key 映射正确**：⑦ spec `shoulder_turn` 的 `fn_key="shoulder_open"`；行为验证：开放角 45° 不触发 RISK-016、20° 触发（见 §4.2）。
- ✅ `judge5` 五态判定用区间宽度倍数（`CRITICAL_SPAN_RATIO=1.0`），`ref_min≤0` 数学安全（独立断言通过）。
- ✅ 新增 spec：`swing_plane`（55~65°，DTL，allow_drop=True）、`shaft_plane_dev`（−5~+10°，DTL，allow_drop=True，proxy_ref_pad=5.0，critical=False）——符合用户决策 1。
- ✅ 20 条 description 抄录，缺失 3 条（hip_toward_target 等）留空 `""` 不编造。
- ✅ `all_metric_keys()` 与 `METRIC_FUNCS` 覆盖自检（metrics 导入期）。

### 2.4 `metrics.py` —— P0/P1：无

- ✅ **`m_swing_plane` 只用 11→15**（L_SHOULDER→L_WRIST，图像像素坐标），可见度守卫 `visibility<0.5 → NaN`；NaN → `allow_drop` **整项剔除**（不填 ref_mid 造假绿值）。独立验证：低可见度时 swing_plane 从指标列表消失 + 告警。
- ✅ `m_shaft_plane_dev` 三级降级：conf≥0.55 → L0 MEASURED；0.25≤conf<0.55 → L1 PROXY（参考区间双向放宽 5°→−10~15）；conf<0.25 / club 不可用 → L2 整项剔除 + 告警。独立构造 0.8/0.4/0.1 三档全部符合。
- ✅ `_sanitize` allow_drop 语义：`None` 整项剔除；非 allow_drop 保持 ref_mid 兜底（23 个既有指标零变化）。
- ✅ 机位过滤唯一实现点 `_specs_for`（架构 §9.5 铁律 2）。

### 2.5 `view_detector.py` —— P0/P1：无

- ✅ 双特征投票：强特征（Address 帧「图像肩宽/身高」<0.13 → DTL）优先，冲突以强特征为准；强特征不可用回退画幅先验；模块级硬约束不抛异常。
- ✅ **阈值非拍脑袋**：实测 3 段正面 0.2486/0.2674/0.2706（全部 >0.13）、6 段 DTL 0.07~0.1265（全部 <0.13），**9/9 分离正确**，与阈值 0.13 零冲突。
- ✅ `resolve(AUTO)` 采信判定；显式机位不一致只给 `WARN_VIEW_MISMATCH` 不阻断。

### 2.6 `pipeline.py` —— P0/P1：无

- ✅ **共享解码只解一次**：所有真实视频探针 `decode_opens=1`（grab_frames 打开 1 次），renderer 复用 `frames_bgr`；解码趟数锁 2 趟（第 1 趟 pose_extractor，第 2 趟共享 grab_frames）。
- ✅ 球杆检测失败不冒泡：club 不可用/异常 → `ClubTrack(available=False)` → shaft_plane_dev L2 剔除，主链路 23 指标 + 风险引擎照常。
- ✅ **CLUB_ENABLED=False 链路完整**：独立实测正面1 → face_on 各阶段指标 3/4/4/4/4/3/4/4、风险 2 条照常产出、无 shaft_plane_dev、无异常。
- ✅ 风险引擎 `evaluate_all` 装入 `phases[].risks`；`camera_view` / `disclaimer`（DTL 追加投影角说明）/ `total_frames=frame_count` 均落值。
- ✅ 进度分段与 `STEP_TEXTS` 一致（step4 = 「计算姿态指标与风险」）。

### 2.7 `club_detector.py` / `renderer.py` —— P0/P1：无

- ✅ `plan_frames` 两道预算护栏：锚点预算（`CLUB_MAX_DECODE_FRAMES//2`）+ 字节预算（192MiB），**8 个事件帧恒保留**（4K 合成用例验证通过；真实视频解码帧数 18~28 ≤ 28）。
- ✅ `detect()` 永不抛异常：缺失视频/纯黑视频/无关键点 → `available=False` 空轨迹。
- ✅ `_draw_club`（高置信实线/低置信虚线）+ `_draw_horizon`（DTL 水平参考线）实现；真实 DTL 视频渲染 8 张全出。
- 🟡 P2：`cv2.imwrite` 在**非 ASCII 路径**（探针用中文「正面2」作目录名）失败——仅探针脚本产物，生产 task_id 为 ASCII UUID 不受影响。

### 2.8 其他改动

- `segmenter.py`/`geometry.py`/`task_store.py` 属 MVP 校准与球杆辅助函数（aspect 校正、fit_line_2d、skeleton_polygon_mask、step_text 字段），与本次增量一致，未发现回归。

---

## 3. 边界与错误路径测试结果（独立脚本 `backend/_probe_out/qa_verify.py`，140 项全过）

| 类别 | 结果 |
|---|---|
| 机位过滤后每阶段指标数 | face_on **3/4/4/4/4/3/4/4**、dtl **2/2/2/2/1/1/1/1**（架构 §3.3 期望值，逐阶段断言） |
| 机位门控 | 正面不触发 DTL 专属（009/014），DTL 不触发正面专属（001/002/005/007/010/012/013/015/016）；实证式门控（指标不存在即跳过）通过 |
| 空状态 | 空 metrics→[]；缺指标→规则跳过；空 phases→{}；NaN 值→不触发 |
| 低置信度降级 | conf 0.8→L0、0.4→L1（ref 放宽 −10~15）、0.1/不可用→L2 剔除+告警 |
| 风险引擎容错 | 单规则异常被吞；恶意/未知占位符不执行不抛 |
| 规则数据完整性 | 17 条、10/7 enabled 分布、metric_key 全部命中、views ⊆ spec、disabled 文案真实为空 |
| plan_frames 护栏 | 4K 下字节预算生效且 8 事件帧保留 |
| judge5 五态 | 5 态边界 + critical=False 三态 + 负区间数学安全 |

---

## 4. 端到端实测数据（真实视频，与工程师声称对照）

> 方法：独立复跑 `backend/_probe_out/probe_v2.py`（9 段全部覆盖：3 正面 + 6 DTL，分三批执行）+ 独立 CLUB_ENABLED=False 冒烟。

| 视频 | 工程师声称 | QA 独立复跑 | 结论 |
|---|---|---|---|
| 正面1 | 2 条风险 | **2 条**（top:RISK-006 107.2° / finish:RISK-017 0.0s） | ✅ 一致 |
| 正面2 | 4 条风险 | **4 条**（backswing:007 47.8° / top:006 127.0° / follow_through:**016 19.5°** / finish:017 0.1s） | ✅ 一致 |
| 正面3 | 3 条风险 | **3 条**（backswing:007 41.0° / top:006 131.0° / finish:017 0.3s） | ✅ 一致 |
| DTL-0bb16a97 | 3 条风险 | **3 条**（top:006 / impact:**014 18.3°** / finish:017） | ✅ 一致 |
| DTL-470057ac | 1 条风险 | **1 条**（finish:017；spine_delta=3.7<10 不触发 014，无假阳性） | ✅ 一致 |
| DTL-4e8d0d7e | 2 条风险 | **2 条**（top:006 / impact:**014 13.2°**） | ✅ 一致 |
| DTL-c6f67f38 | 1 条风险 | **1 条**（finish:017；spine_delta=6.6<10 不触发 014，无假阳性） | ✅ 一致 |
| DTL-087d40a0 | NO_SWING | **NO_SWING**（address→top 4<14，确定性） | ✅ 一致 |
| DTL-707fb04a | NO_SWING | **NO_SWING**（address→top 4<14，确定性） | ✅ 一致 |

**端到端成功 7/9，与工程师声称完全一致，且为确定性结果。**

### 4.1 专项结论

- **RISK-016 不恒真误报** ✅：正面2 触发（开放角 19.5°<30），正面1（122.0°）/正面3（123.5°）**不触发**。只有真正释放不完整（开放角<30°）才触发；合成挥杆端到端用例亦验证取值恒为正开放角（28.1°）。
- **RISK-014 触发正确性** ✅：spine_tilt_change 18.3/13.2 → 触发；3.7/6.6 → 不触发。非恒真，且正面机位因指标不存在天然不参与（机位门控）。
- **机位判定 9/9 命中** ✅：3 正面 ratio 0.2486~0.2706、6 DTL ratio 0.07~0.1265，与阈值 0.13 完全分离；`SHOULDER_TO_HEIGHT_RATIO=0.26` 校准成立（实测均值 0.262、中位 0.267）。
- **球杆降级优雅** ✅：真实视频 conf 0.206~0.462，走 L1 proxy（如 0bb16a97 shaft_plane_dev=3.1 proxy）或 L2 剔除（如 4e8d0d7e conf=0.206 → None + 告警），主链路零中断。
- **共享解码** ✅：9 段视频 decode_opens 恒为 1。

### 4.2 RISK-016 符号陷阱真实数据验证

⑦ 送杆 `shoulder_turn`（对外 key）实测取值：正面1=122.0、正面2=19.5、正面3=123.5——**均为正开放角**（fn_key=shoulder_open=−肩转 生效），不是带符号的负肩转。若按旧 `m_shoulder_turn` 直连，`<30` 会在 3/3 正面视频恒真（122/19.5/123.5 带符号为负 → 全触发）——**符号陷阱确实被数据层 fn_key 拆除，引擎零特判**。

### 4.3 规则与 PRD §3.3 抽查对照（engineer 声称的阈值一致性）

| 规则 | PRD §3.3 | risk_rules.py | 一致 |
|---|---|---|---|
| RISK-001 | hip_turn>62，ref 45~60，face | `Condition(">",62.0)`，views=_F | ✅ |
| RISK-005 | x_factor<18，ref 20~35，face | `Condition("<",18.0)`，views=_F | ✅ |
| RISK-009 | 双区间 <50 or >70，ref 55~65，**DTL** | `(<50,>70)` logic=or，views=_D | ✅ |
| RISK-011 | 双区间 <156 or >174，ref 160~172，全部，三分支 | `(<156,>174)` logic=or + Branch 三分支 | ✅ |
| RISK-014 | spine_tilt_change>=10，ref<8，**DTL** | `Condition(">=",10.0)`，views=_D | ✅ |
| RISK-016 | shoulder_turn<30，ref 35~60，face，映射 shoulder_open | `Condition("<",30.0)`，views=_F，fn_key 拆除 | ✅ |

---

## 5. 架构 §11 偏差声明落实核对（9 条）

| # | 偏差 | 落实位置 | 状态 |
|---|---|---|---|
| 1 | step 保持 int + 并列 step_text | `schemas.TaskStatusView` / `config.STEP_TEXTS` | ✅ |
| 2 | 旧接口路径兼容别名双活 | main.py 双路径注册（**T5 进行中**，diff 已见，待 T5 完成后复测） | ⏳ |
| 3 | judge5 用区间宽度倍数 | `reference.judge5` + `CRITICAL_SPAN_RATIO` | ✅ |
| 4 | frame_count 与 total_frames 并存 | `schemas.VideoMeta` | ✅ |
| 5 | manual_page 类型 Optional[str] | `schemas.RiskItem.manual_page` | ✅ |
| 6 | swing_plane 改名 shaft_plane_dev 并移 P1 | `reference.METRIC_SPECS`（两 spec 并存） | ✅ |
| 7 | spine_side_bend / lead_hand_position 不实现 | 确认不在 METRIC_SPECS | ✅ |
| 8 | 侧面 ⑤⑥⑦⑧ 指标数 0/1/1/1 | 现为 **1/1/1/1**（⑤ 因 shaft_plane_dev 补上 1 个，优于偏差表所写） | ✅ |
| 9 | CLUB_MAX_DECODE_FRAMES 48→28 + 字节预算 | `config.CLUB_MAX_DECODE_FRAMES=28` + `DECODE_BYTES_BUDGET` + `plan_frames` | ✅ |

另核对任务指定附加项：judge5 宽度倍数 ✅、allow_drop 整项剔除 ✅、字节预算护栏 ✅、step_text ✅ —— **全部落实，未漏项（#2 属 T5 范围）**。

---

## 6. 智能路由判定

**路由判定：NoOne（T1~T4 全通过）**

- 工程师声称的 331 passed、真实视频端到端 7/9、正面 2/4/3 条风险、RISK-016 不恒真、机位 9/9、SHOULDER_TO_HEIGHT_RATIO 0.26 校准——**全部经独立复跑证实，无水分**。
- T1~T4 核心（风险引擎/指标/机位/球杆降级/渲染）未发现 P0/P1 缺陷。
- 验证期间发现的 3 处测试失败均为**我方测试脚本自身缺陷**（RISK-009 默认停用、恶意模板字面子串误判、L0 合成夹具缺 Address 帧/窗口点），修正后 140 项独立断言全绿——按流程归 QA 自修，非源码问题。

**注意**：#2（main.py 双路径/错误码）属 T5，本次验证中 main.py 被实时修改（331→355 用例），T5 完成后需对 test_api.py / main.py 相关项做一次回归复测，但不构成对 T1~T4 的打回。

---

## 7. 遗留问题清单（P2，不阻塞）

| # | 问题 | 级别 | 说明 |
|---|---|---|---|
| 1 | `swing_plane` 数值合理性：DTL-470057ac = **29.8°**，超出声称的 45~75 容忍带（另两段 51.6/64.9 正常） | P2 | 计算本身正确（11→15 与水平线夹角，TOP 帧腕仅高于肩 56px），大概率是该样本真实扁平挥杆/TOP 定位；RISK-009 停用故无用户风险。建议人工复核该样本顶点定位，或接受为真实值 |
| 2 | RISK-002 manual_page PRD 冲突（P6 vs 5） | P2 | 代码取 "6"，需 PM 确认 |
| 3 | renderer `cv2.imwrite` 非 ASCII 路径失败 | P2 | 仅探针脚本用中文目录名触发；生产 task_id 为 ASCII UUID，不受影响 |
| 4 | RISK-014 manual_excerpt 与风险名语义弱关联 | P2 | PRD 已标注疑似引错，代码忠实抄录，待用户确认（#B4） |

---

## 8. 验证产物

- 独立验证脚本：`backend/_probe_out/qa_verify.py`（140 项断言，可复跑）
- CLUB 关停冒烟：`backend/_probe_out/qa_club_off.py`
- 探针复跑输出：`backend/_probe_out/probe_qa_face.json` / `probe_qa_dtl.json` / `probe_qa_fail.json` / `probe_qa_render.json`
- 渲染产物（DTL-470057ac 8 张）：`backend/_probe_out/render_v2/DTL-470057ac/`

**结论：T1~T4 可验收；T5 完成后建议对 main.py/test_api.py 补一次回归。**

---

## 9. 追加：T5 终态回归（2026-08-05）

T5（接口契约 + 小程序 v2）已完成并独立验证，详见 **`docs/QA-VALIDATION-T5.md`**。

- 终态全量测试 **355 passed / 0 failed**（此前 331 系 T5 中间态）。
- 独立自建服务 + 真实视频 API 验证 **33 PASS / 0 FAIL**；legacy 回滚独立进程实测通过。
- 工程师冒烟脚本复跑步骤④超时 —— 已定位为脚本 `subprocess.PIPE` 缓冲死锁（探针缺陷），后端无此问题。
- **T1~T5 全部路由 NoOne，无 P0/P1。**

