# ADR-001｜球杆检测的姿态层选型与单目投影角近似

- **状态**：已接受（Accepted）
- **日期**：2025-08
- **决策人**：主理人 / 架构师（高见远）
- **执行人**：工程师（寇豆码）
- **相关文档**：`docs/club-detection-design.md`（§1.1、§4.3、§4.5）、`docs/ARCHITECTURE.md`
- **落地批次**：T01（基础设施与数据契约）

---

## 1. 背景

为侧面（DTL）机位的 `swing_plane` 系列指标提供**真实球杆几何量**，替代原「引导腕–肩连线」代理，
需要引入球杆检测能力。评审期间出现两个必须一次性钉死的架构问题：

1. 借这次改动，是否要把姿态层从 MediaPipe **legacy** `mp.solutions.pose` 迁到 **Tasks API**
   `PoseLandmarker` + 外置 `pose_landmarker.task` 模型？
2. 单目、无标定相机算出的"挥杆平面角"，物理含义到底是什么？边界在哪？

---

## 2. 决策一：维持 MediaPipe legacy，**不迁** Tasks API

### 2.1 结论

**不迁移。** 姿态层继续使用 `mediapipe==0.10.14` 的 legacy API `mp.solutions.pose`
（`config.POSE_KW`，`model_complexity=1`），球杆检测走独立模块 `app/club_detector.py`。

### 2.2 理由（四条）

| # | 理由 | 说明 |
|---|---|---|
| 1 | **模型是同一个，收益为零** | legacy `model_complexity=1` 与 Tasks API 的 `pose_landmarker_full.task` 是同一族 BlazePose GHUM 权重，33 点定义完全一致，精度无实质差异。 |
| 2 | **会引入部署资产管理负担** | legacy 的 `.tflite` 捆绑在 wheel 内部，**零外网依赖**是已验证的强优势。Tasks API 要求外置 `.task` 文件 → 新增「路径配置 / 镜像打包 / 版本漂移」三类风险。 |
| 3 | **会打翻已标定的阈值基线** | `config.py` 第 4 区那 20 多个经验阈值（`V_STILL` / `V_PEAK_MIN` / `IMPACT_Y_TOL` / `MIN_WRIST_TRAVEL` / `FALLBACK_RATIO` …）全部标定在**当前姿态输出的数值分布**上。Tasks API 的 VIDEO 模式需显式喂单调时间戳，其内部平滑策略与 legacy 的 `smooth_landmarks=True` 行为不同 → 输出分布漂移 → `segmenter` 与 `metrics` 需全量重标定 + 回归。纯成本、零收益。 |
| 4 | **`.task` 对球杆检测的贡献 = 0** | BlazePose GHUM 的输出头是**固定的 33×(x, y, z, visibility, presence)**，33 个槽位在模型结构里写死，全部是人体解剖点，**没有第 34 个槽位留给球杆**；`lite`/`full`/`heavy` 只是骨干网宽度差异；训练数据里没有"球杆"这个类别。 |

### 2.3 未来迁移的触发条件（满足**任意一条**才重新评估）

1. **需要多人检测** —— legacy 单人假设不再成立（如双人对比教学、教练同框）。
2. **有 GPU 可用且想用 GPU delegate** —— Tasks API 的 GPU 后端能带来实质吞吐收益。
3. **需要 Tasks API 独有的新特性** —— 例如官方后续只在 Tasks 分支提供的新模型/新输出。
4. **mediapipe 必须升级到 legacy 已被移除的版本** —— 被上游强制。

> 目前四条**一条都不满足**。本 ADR 的存在就是为了避免这个话题被反复重开。

### 2.4 由此确定的实现路径

- **路径 A（经典 CV 几何）**：手腕锚定 ROI + CLAHE + Canny + `HoughLinesP` 杆身拟合 —— 低速段主力。
- **路径 C（帧差）**：`absdiff` + 形态学 + 连通域 —— 高速段（运动模糊使直线消失）互补。
- 两者以 `SwingSignals.speed` 门控自动切换（`config.CLUB_SPEED_SWITCH`）。
- **零新依赖**（opencv / numpy 已在），CPU 增量 < 1s。
- ~~路径 B（自训 YOLOv8-pose → ONNX Runtime）~~：**本期取消**。
  `config.CLUB_MODE` / `CLUB_ONNX_PATH` / `CLUB_ONNX_IMGSZ` 保留为占位常量，
  将来数据到位可一键切换而不动管线；**当前不引入 `onnxruntime` / `torch` / `ultralytics` 任何依赖**。

---

## 3. 决策二：`swing_plane` 是**单目投影角近似**，仅定位业余参考级

### 3.1 结论

- 精度定位 **业余参考级（±5~8°）**，不做相机标定。
- `swing_plane` 系列是 **DTL（侧面）机位专属**；face-on 机位下该指标**不可测**，走整项剔除（`MetricSpec.allow_drop=True`）而**不是**填参考中值。

### 3.2 四条必须显式声明的近似假设

真实的"挥杆平面"是三维空间中近似包含杆身运动轨迹的平面。单目 2D 视频无相机标定，
**无法恢复真实三维平面**，可行的只是"投影角近似"。因此：

1. **结果是投影角，不是真实空间角。** 无相机标定，**不可宣称绝对精度**。
2. **假设 DTL 机位光轴与目标线夹角 < 15°。** 经验值：机位每偏离 10~15°，角度误差约 3~8°。
3. **需要"地平线"参考，且只能用图像水平线。** DTL 下双踝前后重叠，**不能用踝连线定地平线** →
   强制要求「手机保持水平、不倾斜、不俯拍」，该条必须写进小程序拍摄指引；
   建议在结果图上画一条淡色水平参考线供用户自查。
4. **检测的是杆头质心的投影位置，与杆面角度（open/closed face）无关。**
   杆面角需要更高分辨率 + 杆面朝向检测，**明确排除在本方案范围外**，避免产品侧过度承诺。

### 3.3 配套的失败兜底（三级降级）

| 级别 | 触发条件 | 行为 |
|---|---|---|
| **L0 measured** | `overall_confidence ≥ config.CLUB_CONF_MIN`（0.55） | 用真实球杆几何量计算，`source=MEASURED` |
| **L1 proxy** | `CLUB_CONF_PROXY_MIN ≤ conf < CLUB_CONF_MIN` | 回退「引导腕–肩连线倾角」代理，参考区间放宽，`estimated=True`、`source=PROXY` |
| **L2 unavailable** | `conf < CLUB_CONF_PROXY_MIN`，或机位为 face-on | **整项剔除**（`allow_drop=True` 生效）+ 追加 `config.WARN_CLUB_UNAVAILABLE` |

> **降级绝不失败整个任务。** `club_detector` 的任何异常都在模块内部被吞掉并返回
> `ClubTrack(available=False)`，**禁止外抛 `AnalysisError`**——挥杆分析主链路
> （已跑通的 23 个指标）不能被一个增量特性拖垮。这是模块级硬约束。

---

## 4. 影响面

| 文件 | 变更 |
|---|---|
| `backend/app/config.py` | 新增第 8 区球杆检测参数（约 18 个常量，全部带默认值） |
| `backend/app/schemas.py` | 新增 `CameraView` / `MetricSource` / `ClubDetection` / `ClubTrack`；`StageMetric` +`estimated`/`source`/`confidence`；`VideoMeta` +`camera_view` |
| `backend/app/geometry.py` | 新增 7 个图像平面几何纯函数 |
| `backend/app/reference.py` | `MetricSpec` +`views`/`allow_drop` |
| `backend/app/frame_reader.py` | **新增**，共享帧解码，解码趟数保持 2 趟不变 |
| `backend/app/club_detector.py` | **新增**，路径 A + C 检测核心 |
| `backend/app/renderer.py` | 改为消费 `frame_reader` 的解码结果 |

**全部新增均为向后兼容默认值，现有 23 个指标行为零回归。**

---

## 5. 备选方案与否决理由

| 备选 | 否决理由 |
|---|---|
| 迁到 Tasks API 顺带取球杆 | `.task` 模型物理上不含球杆输出头，贡献为 0；迁移打翻已标定阈值 |
| 找现成的球杆检测研究模型 | 学术界标注的是人体与挥杆事件（GolfDB / SwingNet），**不是球杆关键点**；社区数据集多为 bbox + 静态商品图，域差距大；**不存在**可直接下载即用的资产 |
| 只用帧差（路径 C 单独） | 背景有人走动即失效，误检率极高，只能做 A 的互补分支 |
| 给 `pose_extractor.extract()` 加 `frame_sink` 回调复用第 1 趟解码 | 会把球杆逻辑塞进明确声明"只做姿态"的模块，破坏单一职责，且被迫对全部 480 帧做检测（浪费）。改用独立 `frame_reader` 稀疏取帧 |
| 开 `POSE_KW["enable_segmentation"]=True` 拿精细人体掩膜 | 单帧推理 +15%（全片约 +3~5s）。改用 `geometry.skeleton_polygon_mask()` 的零成本粗掩膜 |
