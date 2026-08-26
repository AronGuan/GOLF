# 高尔夫挥杆分析小程序 — 项目状态文档（STATUS）

> 更新日期：2026-08-18 · 维护人：齐活林（交付总监）
> 本文档是对当前进度的权威快照，与 `docs/` 下各专项文档配套使用。

---

## 一、项目是什么

**微信小程序 + Python 后端的高尔夫挥杆姿态分析工具。**

用户用手机拍摄挥杆视频 → 上传 → 后端 MediaPipe 抽取人体 33 关键点 → 把挥杆切成**经典 8 阶段**（准备 Address / 起杆 Takeaway / 上杆 Backswing / 顶点 Top / 下杆 Downswing / 击球 Impact / 送杆 Follow-through / 收杆 Finish，与学术 GolfDB 8 事件标准对齐）→ 每个阶段计算身体角度指标（核心诊断指标：**X-Factor 肩髋分离度**）→ 小程序结果页展示 8 张骨架叠加图 + 指标卡 + 风险提示。

参考产品：GolfSwings / AI Golf。

---

## 二、技术底座与硬约束（已实测验证，勿推翻）

| 项 | 结论 |
|---|---|
| Python 环境 | **便携版 3.12.9**：`E:\project\golf\.tools\python312\python.exe`（embeddable，无 venv，直接用） |
| MediaPipe | **锁定 `0.10.14` + legacy API `mp.solutions.pose`**。内置 `pose_landmark_full.tflite`，零外网依赖 |
| MediaPipe 禁忌 | 严禁 `mediapipe.tasks` / `PoseLandmarker` / 下载 `.task` 模型（Google 存储国内直连超时、全部镜像 404，已验证死路；0.10.30+/1.0.0 为精简包无 `mp.solutions`） |
| numpy | **必须 <2**（锁 1.26.4） |
| OpenCV | `opencv-python-headless` 4.11；视频编码器用 **`mp4v`** |
| 后端 | FastAPI + uvicorn，**固定单 worker**（任务状态存进程内字典 `task_store.py`） |
| 前端 | **原生微信小程序**，零 npm、零构建，3 个页面 |
| 部署 | 阿里云 ECS `39.102.63.30:8000`（Conda env `golf` + systemd `golf-backend.service`），运维脚本 `backend/manage.sh` |
| 沙箱限制 | curl 写文件失败（用 Python urllib）；pip 走清华镜像 `-i https://pypi.tuna.tsinghua.edu.cn/simple` |

---

## 三、功能完成度

### 3.1 MVP v1.0 ✅ 完成并已上云

- 后端 13 模块（6182 行）：config / schemas / geometry / pose_extractor / segmenter / reference / metrics / renderer / pipeline / main / task_store / frame_reader / run.py
- 小程序 3 页面：index（上传）/ analyzing（进度）/ result（8 阶段缩略图 + 指标卡 + 免责声明）
- 指标体系：35 条 MetricSpec（32 阶段 + 3 全程），核心 X-Factor = 肩转 − 髋转
- 8 阶段切分：基于手腕轨迹的启发式规则（非深度学习），锚点缺失返回 `NO_SWING`

### 3.2 PDD v2.0 ✅ 已完成（除球杆检测外）

| 模块 | 状态 | 说明 |
|---|---|---|
| 双机位（face-on + down-the-line） | ✅ | 前端机位选择 UI + 后端 `view_detector` 自动判定（双特征投票，实测 9/9 命中） |
| 侧面专属指标 | ✅ | `swing_plane`（④顶点引导臂与水平面夹角 55~65°）、`spine_tilt_change`（= `m_spine_tilt_delta` 改名）、`spine_tilt_fwd` |
| 损伤风险引擎 | ✅ | 17 条规则（RISK-001~017），**10 条启用 / 7 条缺文案默认关闭** |
| 指标状态 5 值 | ✅ | 含 `critical_low` / `critical_high`（红橙配色） |
| 指标卡术语解释行 | ✅ | `description` 字段（20 条已落地） |
| 手册原文弹窗 | ✅ | `manual_excerpt` 门控的半屏弹窗 |
| 全程指标常驻条 | ✅ | 3 条全程指标 |
| 接口契约对齐（PDD §7） | ✅ | 双路径注册 + PDD 错误码映射（详见 §6） |
| **球杆检测（shaft_plane_dev）** | 🔧 **已下线** | 识别率低（置信度 0.206~0.462 全 L1 proxy），用户拍板先移除后期再做；**代码保留可逆** |

### 3.3 ClubLite 击球帧校正 ✅ 已完成（2026-08-17）

| 模块 | 状态 | 说明 |
|---|---|---|
| 击球帧校正 | ✅ | 新增 `impact_refiner.py`：帧差运动峰（M1）+ 杆身端点验证（M2），把击球帧从"手腕位置"（偏早 10+ 帧）修正到"真实杆头运动"（±1~2 帧） |
| 双机位适配 | ✅ | DTL 地面 ROI 全宽；**face-on ROI 收窄到双踝轴中央带（0.60 宽）** + 杆身验证更高权重（0.30 vs 0.15）作遮挡补偿 |
| 集成方式 | ✅ | **不改 `locate_impact`**（349 测试零回归）；`segmenter.reanchor_impact` 纯函数重建 8 事件；pipeline step4a 集成（opens=1、窗口 ≤12 帧、G1/G0 两级降级） |
| 降级策略 | ✅ | G1 采纳校正 / G0 保持现状（失败=回到现状，不新增估算态、不阻断任务） |
| 锚点选帧（D 方案） | ✅ | **M2 杆身最低点作先验锚点 ±3 帧邻域内选帧**（`CLUBLITE_USE_ANCHOR=True`）+ 假锚点守卫（`MIN_SCORE_RATIO=0.7`，防 DTL 底边饱和假阳性）；新样本 120→115 命中真实击球，其余 9 段零回归 |

### 3.4 手动帧微调 ✅ 已完成（2026-08-17）

| 模块 | 状态 | 说明 |
|---|---|---|
| 后端动态帧渲染接口 | ✅ | `GET /api/v1/task/{id}/frame/{idx}`（双路径），返回指定帧骨架图 PNG + `X-Frame-Index` 头；错误码 20001/20002/20003（越界，事件帧±30）/10004；clamp 到视频范围 |
| landmark 复用 | ✅ | 分析时关键点落盘 `.npz`（<1MB），接口复用保证骨架一致（**事件渲染 vs 接口渲染 13/13 关节圆心完全匹配**） |
| 前端 ◀/▶ 按钮 | ✅ | 每阶段缩略图下 ◀/▶ + 帧号 + amber"手动"角标；切换阶段复位；`getFrameImage`（arraybuffer → wxfile:// 本地临时文件） |
| 视频保留 | ⚠️ | `KEEP_SOURCE_VIDEO=True`：原视频改名 `source.{ext}` 留任务目录 7 天（手动微调需原始像素）；可一键关停（关停后帧接口降级 5000） |
| 指标联动 | 🔒 | **手动微调不重算指标**（用户拍板"先看效果"）；v2 算法前移则指标自动更新 |

### 3.5 保存当前页到相册 ✅ 已完成（2026-08-18）

| 模块 | 状态 | 说明 |
|---|---|---|
| 离屏 canvas 合成 | ✅ | 隐藏 `<canvas type="2d">` 重绘：hero 大图 + 阶段名/帧号 + 指标卡（含 description）+ 风险区 + 底部信息 |
| 保存链路 | ✅ | `canvasToTempFilePath` → `saveImageToPhotosAlbum`；授权拒绝 → `openSetting` 引导 |
| 所见即所得 | ✅ | 数据实时取自 `this.data`，**手动微调帧（adjCur+本地图+手动角标）正确体现** |
| 共存 | ✅ | 与既有「保存单张骨架图」按钮并存，后端零改动 |

---

## 四、本次迭代历程（2026-08-05 ~ 08-18）

### A 任务：真实视频跑通 MVP 闭环 + 阈值校准 ✅
- **素材到位**：`.tools/_probe/samples/` 3 段正面 + 6 段 DTL + `video/` 2 段 DTL
- **切分成功率 9/11 = 81.8%**，达成 AC-05（≥70%）；2 段失败为"从挥杆中途开始"的残缺样本，算法诚实返回 `NO_SWING`（未强行迁就）
- **指标符号澄清**：`ROTATION_SIGN=-1` 无误，顶点 `shoulder_turn` 全为正（+66.4/+39.1/+27.1），X-Factor 落 20.7~49.5° 合理带；`metrics.py`/`geometry.py` 零修改
- **挖出真 bug**：阈值缺各向同性校正，竖屏/横屏漂移 3.2 倍 → `segmenter.py` 引入 `aspect=H/W` 校正，`config.py` 6 个阈值按各向同性重算
- 报告：`docs/VALIDATION-A.md`

### B 任务：球杆检测接入（已下线）→ 侧面指标 ✅
- 用户拍板：`swing_plane` 归 PDD 定义（纯人体关键点），球杆版改名 `shaft_plane_dev`（P1 增强）
- **用户拍板：球杆检测下线**（识别率低），`shaft_plane_dev` 从主管线摘除，代码保留

### C 任务：损伤风险筛查引擎 ✅
- PM 许清楚从 PDD v2.0（桌面 docx）提取 17 条规则全量结构化定义 → `docs/PRD-v2-risk-engine.md`
- 架构师高见远产出增量设计 → `docs/ARCHITECTURE-v2.md`（新增 8 文件 / 修改 22 文件 / 零新增依赖）
- 工程师实现 T1~T5：契约层 / 规则库+引擎 / 指标层 v2 / 管线整合 / 接口契约+小程序
- **关键拆弹**：RISK-016 符号陷阱（`fn_key` 映射表，引擎零特判）、RISK-011 内嵌 JS 三元（`_SafeDict` 禁 eval）
- 报告：`docs/QA-VALIDATION-BC.md`、`docs/QA-VALIDATION-T5.md`

### D 任务：球杆检测下线 🔧（2026-08-05）
- 用户拍板：识别率低（实测置信度 0.206~0.462 全 L1 proxy），先移除后期再做
- `shaft_plane_dev` 从主管线摘除（代码保留可逆），QA 复验 NoOne → `docs/QA-VALIDATION-CLUBOFF.md`

### E 任务：ClubLite 击球帧校正 ✅（2026-08-17）
- **触发**：用户测试反馈"击球帧不准确，球杆没在击球位置"（截图正面机位 152 帧）——根因：`locate_impact` 用 MediaPipe 手腕相对高度定位，**手腕≠真实杆头**（杆头在地面高度），天生偏早
- **用户拍板**：Q1 双机位都要可靠校正（face-on 不能只尽力而为）；Q2 球不一定可见（球点仅评分加权不依赖）；Q3 窗口 ≤12 帧；Q4 校正后追加 warning；Q5 前端本期不改
- 架构师高见远选型 → `docs/ARCHITECTURE-v3-clublite.md`：**只做帧级时序校正，不做像素级杆头定位**（与下线重方案的本质区别）
- 工程师实现 → `docs/VALIDATION-CLUBLITE.md`：**11 段真实视频 9/9 校正有效、face-on 3/3、delta ∈[+1,+8]（mean +5.56）、opens=1**
- QA 两轮 → `docs/QA-VALIDATION-CLUBLITE.md`：R1 抓出 **P1（reanchor 后⑦送杆帧不在解码集 → 送杆截图内容错帧）**→ 修复（`plan_reanchor_frames` 前置预计算，opens 保持 1）；R2 确认送杆图逐字节等于真实帧、delta 零回归 → **NoOne 交付**

### F 任务：手动帧微调 ✅（2026-08-17）
- **触发**：ClubLite 校正后仍有边界偏差（球速低视频偏后 1~8 帧），用户决定加 ◀/▶ 手动兜底
- 后端动态帧接口 + 前端按钮 → `docs/QA-VALIDATION-FRAMEADJUST.md`：**382 测试全绿、独立 E2E 57 断言、NoOne 放行**；事件渲染 vs 接口渲染骨架 13/13 一致

### G 任务：ClubLite v2 算法前移 1 帧 ✅（2026-08-18，半天）
- **触发**：用户手动微调发现算法选 155 帧（运动峰）、视觉判断 154 帧才是接触瞬间——算法固有"运动峰 vs 接触瞬间"1 帧偏移
- 方案 C：`config.CLUBLITE_IMPACT_OFFSET = -1`（**0 即回滚 v1**）+ 物理下界守卫（line 827）+ `plan_reanchor_frames` 覆盖扩展（候选 ∪ 候选-1）
- **11 段真实视频 9/9 delta 各 -1 向接触瞬间靠近**（正面1 1→0 无操作校正）；386 测试全绿
- **送杆阶段不适用偏移**：`locate_finish` 找"手腕静止段起点"，送杆无物理"准确点"、偏差方向不固定，加偏移净效果可能为负——**维持手动微调兜底**

### H 任务：保存当前页到相册 ✅（2026-08-18，快速模式）
- 离屏 canvas 2d 合成整页（hero 图+指标卡+风险区）→ `saveImageToPhotosAlbum`；授权拒绝引导 `openSetting`；手动微调帧正确体现
- 3 文件（wxml/wxss/js），后端零改动，386 测试全绿

### I 任务：ClubLite D 方案——杆身最低点先验锚点 ✅（2026-08-18 晚，用户实测驱动）
- **触发**：用户提供真实视频 `22030124ed3bce12cdec7c629d0c6cc8.mp4`（画面含购物车银色杆身反光噪声），实测真实击球 f115、算法选 f120（偏晚 5 帧=167ms）
- **诊断**：旧 `locate_impact`=113（手腕速度峰，偏早 2 帧）；M1 运动峰=121（**偏晚 6 帧**，横扫式噪声）；**M2 杆身最低点=116（只偏 1 帧，最准）**；v2 "-1 偏移"假设"运动峰=接触+1帧"在该视频不成立
- **D 方案**：`impact_refiner.py` 新增 `_anchor_neighborhood`（锚点±3 邻域）+ `_anchor_window_credible`（**假锚点守卫 ratio≥0.7**，防 DTL 底边饱和假阳性）+ `_select_best`；config 新增 `CLUBLITE_USE_ANCHOR=True`（False 即回 v2）/ `CLUBLITE_ANCHOR_WINDOW=3` / `CLUBLITE_ANCHOR_MIN_SCORE_RATIO=0.7`
- **12 段真实视频**：新样本 **120→115 命中真实击球**（delta=+2 ∈ 期望[-1,+3]）；**其余 9 段与 v2 完全一致（0 变化）**；2 段残片 NO_SWING；**399 测试全绿**（386+13）
- **offset 实验**：-1 最优（锚点修正峰位 121→116、偏移修正杆头最低点→视觉接触 116→115，分工明确）
- **回退路径**：`CLUBLITE_USE_ANCHOR=False` 一键回 v2；git HEAD `d9034ae` `git checkout` 即恢复
- 文档：`docs/VALIDATION-CLUBLITE.md` §3 D 方案实验记录（含守卫 ratio 表）；新样本入样本库

### J 任务：DTL 机位感知 + 身高标尺 + 阈值重标 ✅（2026-08-26，用户实测驱动）
- **触发**：用户实测侧面（DTL）视频，击球/下杆/送杆帧普遍走兜底 estimated、⑤⑥ 接近顶点——根因是 DTL 视角下**肩宽在画面里被压缩**（scale 0.005~0.014，正面 1/20），导致 h 信号爆炸到 20~34、穿越判据永不触发。
- **机位感知改造**（`3333a11`/`cddb09e`）：`segmenter`/`pipeline`/`impact_refiner` 加 `view` 尾部默认参数（**默认 face-on 逐字节不变**）；⑤ 新增 `H_DOWNSWING_DTL`；`locate_impact` DTL 用穿越点直接作击球帧（去掉速度峰偏移）。
- **Step1 身高标尺**（`b298251`）：`build_signals` DTL 用 NOSE→双踝**身高中位数**替代肩宽（face-on 肩宽不变）——h 从 20~34 压回 0.6~0.7，**3 个 DTL 样本 impact 从兜底变真实穿越**。
- **Step2 阈值重标**（`7b0058a`）：`H_DOWNSWING_DTL 0.25→0.40`、新增 `FOLLOWTHROUGH_RISE_DTL=0.10`（身高制）；5 样本⑤⑦ 变 real 定位；0bb16a97 修正为正确 NO_SWING（旧肩宽制守卫误判"有挥杆"）。
- **方案C 门槛放宽**（`42e78f0`）：新增 `MIN_TOP_ADDR_SEC_DTL=0.30`（DTL 人物准备 1~2s 导致 address 靠后、address→top 挤压触发误杀）；face-on 保持 0.45 不变。
- **验证结论（重要技术边界）**：DTL 击球帧 CLUBLITE M1 运动峰偏差方向**不固定**（实测 +5/-3/+2 帧）；方案 B 尝试修复 M2 杆身检测假阳性（67% 卡死 y=1279），但暴露更深问题——**MediaPipe 对 DTL 侧面机位 body_h_px 估计严重失真，杆长约束失效，杆身检测在 DTL 下根本不可靠**。结论：**DTL 击球帧精度是当前技术栈天花板**（MediaPipe 只出人体点 + 无球杆检测 + DTL 深度缺失），无法通过杆身检测根治。

### K 任务：手动微调实时重算指标 ✅（2026-08-26 晚，M8 解法）
- **背景**：M8 已知限制"手动微调只换图、指标不变"——算法精度到天花板后，这是兜底方案。
- **实现**（`a13d67f`）：新增接口 `GET /api/v1/task/{id}/phase_metrics/{phase}/{frame_idx}`（双路径），克隆 events 覆盖目标 phase 帧 → 复用 `build_context` + `compute_phase_metrics` 重算；前端 ◀/▶ 换图后追加调用，**只刷新当前 phase 指标卡**（loading + 失败降级）。
- **价值**：手动微调从"猜"变"实时反馈"——调到哪帧，指标就是哪帧的值，算法层偏差被产品层彻底兜底。**核心算法零改动**。
- **测试 467 passed**（457 + 10 新增）；错误码 20001/20002/20003/20004。

---

## 五、当前测试与质量状态

| 项 | 值 |
|---|---|
| 单元测试 | **467 passed / 0 failed**（基线 220 → 355 → 349 → 371 → 382 → 386 → 399 → 432 → 440 → 446 → 451 → 457 → 467） |
| 真实视频端到端 | 7/9 段跑通（2 段残缺 NO_SWING 确定性失败）；正面1 触发 2 条风险、DTL 段 swing_plane=51.6° |
| 机位自动判定 | 9/9 命中 ground-truth |
| 击球帧校正 | 12 段真实视频：9 段有效校正 + 新样本 120→115 命中真实击球、其余 9 段与 v2 零回归、opens=1；DTL 身高标尺后 3 样本 impact 从兜底变真实穿越 |
| 手动帧微调 | 后端动态帧接口 + ◀/▶ 按钮；事件渲染 vs 接口渲染骨架 13/13 一致；错误码 20001/20002/20003 实测；**◀/▶ 实时重算当前阶段指标（新接口 phase_metrics，467 测试）** |
| 风险引擎 | 17 规则 metric_key 17/17 可解析；RISK-016 不恒真误报；RISK-014 无假阳性 |
| 接口契约 | 双路径等价、错误码 10001/10002/10003/20001/20002/20003/10004 实测、legacy 回滚开关验证通过 |
| QA 终判 | **全部 NoOne 放行**（T1~T4 / T5 / 球杆下线 / ClubLite R1+R2 / FrameAdjust）；D 方案由主理人自检（沙箱团队工具不可用） |

---

## 六、接口契约（v2 现状）

- **双路径注册**：PDD 主路径 `POST /api/v1/task/create`、`GET /api/v1/task/status/{id}`、`GET /api/v1/task/result/{id}` + 旧别名 `/tasks`、`/tasks/{id}`、`/tasks/{id}/result`（灰度期双活，**不破坏已上线小程序**）
- **错误码**：对外 PDD 码（10001/10002/10003/10004/20001/20002），内部保留语义码（0/4001/4004/4009/5000），响应层 `ApiError.pdd_code` 映射；`API_CODE_STYLE="legacy"` 可整体回滚旧码
- **字段兼容**：`video`/`file` 双字段名、`step` int + `step_text` str 并列、`.mov` 放开、`camera_view` 缺省落 face_on 不硬拒

---

## 七、已知问题与待确认事项

### 待用户方（PM 郑天虹）确认
| # | 事项 | 影响 |
|---|---|---|
| 1 | **7 条缺文案风险规则**（RISK-003/004/008/009/012/013/015）：触发原因/建议/手册原文缺失 | 判定逻辑已实现但 `enabled=False`，补齐文案后翻开关即可上线，零代码改动 |
| 2 | **20002 错误码**（任务未完成）：PDD 未定义，暂定 `config.PDD_CODE_TASK_PENDING=20002` | 定稿后改 config 一行 |
| 3 | **RISK-002 手册页码**：PDD 内部冲突（P6 vs P5），代码取 §5.2.2 完整版 + P6 | 需确认 |
| 4 | **spine_tilt_side 参考区间**：① 几何量变更后 ref 5~12 待重标（正面1 实测 -5.5° 落 critical_low，疑似区间需按新几何量重标） | 需 PM/教练复核 |
| 5 | **swing_plane 样本复核**：DTL-470057ac 实测 29.8° 偏低（疑似真实扁平挥杆/机位朝向耦合），需人工复核该样本 | 不阻塞，RISK-009 停用无风险 |
| 6 | **球杆检测后期方案**：如需提升识别率，方向是换检测算法（YOLO + 标注数据集），启发式 ROI+Hough 识别率天花板已到 | 用户已决策后期再做 |

### 工程遗留（P2，不阻塞）
| # | 事项 |
|---|---|
| 1 | `club_detector.py:583` docstring 仍引用已删的 `DECODE_BYTES_BUDGET`（纯注释，运行期无影响） |
| 2 | 球杆下线"1.1s 开销消除"无干净 A/B 基准（前后探针脚本不同，DTL 14.6s vs 8.1s 属 MediaPipe 提取方差） |
| 3 | `API_CODE_STYLE` 是硬编码常量非环境变量，线上回滚需改码重部署 |
| 4 | renderer `cv2.imwrite` 非 ASCII 路径失败（仅探针中文目录触发，生产 task_id 为 ASCII） |
| 5 | 旧探针 `probe_v2.py`/`qa_verify.py`/`qa_club_off.py` 引用已删常量/旧签名（`_probe_out` 历史产物，不进测试与主管线） |
| 6 | `deploy/README.md` 仍残留 scp 示例文案（与现 git 中转方式不一致，低优先级） |
| 7 | `impact_refiner.py` 诊断字段 `shaft_lowest_index` 取 `min(shaft_ys)` 与决策语义（选 y 更大更贴地）相反，仅诊断用 |
| 8 | pipeline 的 grab 调用含 possible 帧无单测守卫（若误删 `\| set(_possible_frames)` 单测仍绿），QA 集成探针建议沉淀为正式回归 |
| 9 | 合成 fixture 上送杆帧不移动，ClubLite 新用例的 FT 专用分支是死代码，建议补强制 FT 移动 fixture |
| 10 | `VALIDATION-CLUBLITE.md` §5 仍只断言 8 张截图文件存在、未断言内容帧正确性（P1 曾藏身于此） |
| 11 | FrameAdjust：frame_service 每次开新 VideoCapture 无缓存；getFrameImage 临时 PNG 累积不清理；`KEEP_SOURCE_VIDEO=False` 时帧接口降级 5000；onSaveImage isLocal 判断假设绝对 URL；onPreview 混入 wxfile:// 低版本可能不支持；test_frame_adjust 覆盖度空白（独立 E2E 已补齐）；getFrameImage 多余 content-type header |
| 12 | v2 观察项：WARN 阈值副作用（c6f67f38 v1 delta=+3 触发 → v2 +2 不触发），若要 warning 反映未偏移的运动峰位移需向 pipeline 暴露 `peak_delta` |
| 13 | 保存当前页：远程图跨域依赖 downloadFile+域名配置（BASE_URL 裸 IP 时真机调试模式可用）；canvas 2d 需基础库 2.9+ |

---

## 八、部署与上线状态

| 项 | 状态 |
|---|---|
| 后端部署 | ✅ 阿里云 ECS `39.102.63.30:8000`，Conda env `golf` + systemd，单 worker |
| 部署方式 | ✅ 本地 `git push` → GitHub → ECS `git pull` → `bash deploy-aliyun-conda.sh` |
| 运维 | ✅ `backend/manage.sh`（start/stop/restart/status/logs/check/health） |
| **HTTPS + 备案域名** | ❌ **未做（用户 D 项，自行推进）** |
| 微信 request 合法域名 | ❌ 未配置 |
| 小程序 BASE_URL | ⚠️ `utils/api.js` 仍写死 `http://39.102.63.30:8000`（裸 IP+HTTP）；**预览/体验版/正式版下微信会拦截，仅开发者工具真机调试可用** |

> ⚠️ **上线硬前提（D 项）**：备案域名 + SSL + 微信公众平台 request/uploadFile/downloadFile 合法域名白名单 + nginx 反代 HTTPS，`BASE_URL` 改 `https://域名`。备案 1~3 周，建议尽早并行启动。

---

## 九、文档地图

| 文档 | 内容 |
|---|---|
| `docs/PRD.md` | MVP 需求（3 页面、8 阶段、X-Factor） |
| `docs/ARCHITECTURE.md` | MVP 架构（§7.7 切分阈值、§8.3 指标定义表） |
| `docs/PRD-v2-risk-engine.md` | **增量 PRD**：17 条 RISK 规则完整定义、侧面指标、5 值状态、接口对齐（36k 字符 9 章） |
| `docs/ARCHITECTURE-v2.md` | **增量设计**：文件清单、类图、时序图、任务列表、偏差声明（§0~§11） |
| `docs/v2-class-diagram.mermaid` | 类图（可复用） |
| `docs/v2-sequence-diagram.mermaid` | 时序图（可复用） |
| `docs/VALIDATION-A.md` | A 任务实测：11 段切分表格、正面 3 段完整指标、符号诊断、阈值修改依据 |
| `docs/QA-VALIDATION-BC.md` | QA 验证 T1~T4（NoOne） |
| `docs/QA-VALIDATION-T5.md` | QA 终态回归 T5（NoOne） |
| `docs/QA-VALIDATION-CLUBOFF.md` | QA 复验球杆下线（NoOne） |
| `docs/ARCHITECTURE-v3-clublite.md` | **ClubLite 设计**：选型对比、接口设计、任务列表、测试策略、风险 |
| `docs/VALIDATION-CLUBLITE.md` | **ClubLite 实测**：11 段真实视频逐段 delta 表、face-on 适配、阈值微调依据、P1 修复记录 |
| `docs/QA-VALIDATION-CLUBLITE.md` | **QA 两轮**：R1 抓 P1（送杆帧错位）→ R2 确认修复（NoOne） |
| `docs/QA-VALIDATION-FRAMEADJUST.md` | **QA 验收手动帧微调**：382 测试 + 57 独立 E2E、事件/接口渲染骨架一致性（NoOne） |
| `docs/ADR-001-club-detection.md` | 架构决策：姿态层不迁 Tasks API（4 条理由） |
| `docs/club-detection-design.md` | 球杆检测设计（归档，功能已下线） |

---

## 十、后续路线图

| 阶段 | 内容 | 依赖 |
|---|---|---|
| **上线前置（D 项，用户推进中）** | 备案域名 + HTTPS + 微信白名单 + BASE_URL 切换 | 用户侧商务动作，1~3 周 |
| **M1：补齐风险文案** | 7 条缺文案规则补齐 → 翻 `enabled` 开关上线 | PM 郑天虹补文案 |
| **M2：侧面机位打磨** | swing_plane 样本复核、spine_tilt_side 参考区间重标、DTL 标尺优化 | 真实侧面素材 + 教练/理疗师审核 |
| **M3：球杆检测（后期）** | 击球帧已由 ClubLite 轻量校正覆盖（v2 9/9 有效）；若要**像素级杆头定位**或 `shaft_plane_dev` 增强指标，方向是换 YOLO + 标注数据集，或等待标注飞轮成熟 | 标注数据集（≥30 段） |
| **M6：送杆/其他阶段精度** | `locate_finish` 等阶段若有统计性偏差（多段视频同方向偏移）再加偏移常量；当前手动微调兜底；ClubLite 锚点方案可借鉴 | 多段真实视频统计 |
| **M7：ClubLite 守卫复标** | `CLUBLITE_ANCHOR_MIN_SCORE_RATIO=0.7` 基于当前 12 段标定，样本扩大后复标（改一常量） | 更多特殊场景样本 |
| **M8：DTL 击球帧手动微调** | CLUBLITE refine 在 DTL 视角下 M1 运动峰偏差方向不固定（实测 ±5 帧、不固定方向），单一常量救不了；**已解**——手动微调 ◀/▶ 实时重算指标（K 任务，commit `a13d67f`），指标跟着帧走 | 算法层天花板已确认（杆身检测 DTL 下不可靠）；产品层兜底已落地 |
| **M4：并发能力** | 外置 Redis/DB 支持多 worker | 用户量上来后 |
| **M5：标注真值验收** | 人工标注 8 阶段真值帧，跑 AC-11 严格验收 | 标注素材 |

---

*文档结尾 · 状态为 2026-08-27 快照，后续迭代请更新本文件头部日期与 §三/§五/§七 相关小节。*
