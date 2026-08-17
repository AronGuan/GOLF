# 轻量级球杆/杆头检测 v2 —— 击球帧校正（ClubLite）增量设计

> 作者：高见远（架构师）｜ 版本：v3.0 ｜ 状态：待主理人/用户确认
> 目标：为已下线的"重球杆检测"设计**轻量级替代方案**，解决用户明确问题——**击球帧定位偏早（手腕位置 ≠ 真实杆头/球的击球瞬间）**。
> 前置阅读：`docs/ARCHITECTURE-v2.md`（架构基线）、`docs/QA-VALIDATION-CLUBOFF.md`（下线 QA 报告）、`docs/club-detection-design.md`（历史设计，不再适用）、`docs/VALIDATION-A.md`（真实视频基线）。
> 环境红线：Python 3.12.9 便携版 / MediaPipe 0.10.14 legacy / numpy <2 / **零新增第三方依赖** / OpenCV mp4v / 后端单 worker / 测试基线 349 passed / 0 failed。

---

## 0. 一句话结论

**推荐"地面 ROI 帧差运动峰 + 球点/杆头最低点约束"（M1）作为主方案、"候选帧杆身端点验证"（M2）作为辅助评分与兜底，新增一个模块 `impact_refiner.py`，只做「帧级时序校正」（±1~2 帧），不做「像素级杆头定位」——这正是它与下线的重方案（ROI+Hough+帧差+ONNX，实测真实视频置信度仅 0.206~0.462、L0 从未出现过）的本质区别。零新依赖、3~5 天可交付、失败自动降级不破坏主链路。**

---

## 1. 背景与问题根因

### 1.1 用户问题

"击球帧不准确，球杆没有在击球的位置"——截图显示 5.24s/152 帧的"击球帧"里手腕已在身体前方，但**杆头没到地面击球点**。

### 1.2 根因

当前击球帧定位（`backend/app/segmenter.py:264` `locate_impact`）的判据是：

1. **手腕高度**相对髋部回落到 Address 高度（`h <= h_addr + IMPACT_Y_TOL`）；
2. 在该回落点附近的**手腕速度峰**。

问题在于**手腕 ≠ 杆头**：

| 对象 | 击球瞬间的位置 | 与 Address 的关系 |
|---|---|---|
| 手腕（握把端） | 髋部附近、躯干前方 | 高度 ≈ Address 手位（回落判据命中） |
| 杆头 | **地面高度**，球的正后方 | 杆身由 Address 的躺角（~60°）转到击球时近垂直，杆头在手腕高度回落**之后**才到达地面最低点 |

因此「手腕高度回落」天然**早于**「杆头触球」若干帧（实测截图偏差在 10+ 帧量级，30fps 下约 0.3s+）。速度峰分支同样以手腕速度衡量，早于杆头速度峰。

### 1.3 关键洞察：本方案为什么能轻

下线的重方案失败原因是**追求像素级杆头几何定位**（每帧 head 坐标 + 置信度 + 三级降级），而真实视频中杆身在高速段运动模糊、直线边缘被抹掉，导致 Hough 分支置信度长期 <0.55（L0 从未出现）。

本方案**只追求帧级时序**：哪个采样帧的杆头最接近"贴地/球点"？这是一个**二选一/三选一的帧选择问题**，不是连续几何估计问题——对检测精度的要求大幅放宽，轻量 CV 原语（absdiff + 阈值 + 质心）即可胜任。这是 3~5 天能交付的根本原因。

---

## 2. 技术选型对比

### 2.1 候选方案总表

| 候选 | 思路 | 新增依赖 | 实现复杂度 | 预期准确率（±2 帧内） | 主要风险 |
|---|---|---|---|---|---|
| **A. 地面 ROI 帧差运动峰** | 在下杆窗口内，对**地面带 ROI**（踝关节下方）做帧差，找运动强度最大帧（杆头最快 ≈ 触球） | 0 | 低 | DTL 70~80% / face-on 50~60% | 身体/脚踝运动混入 ROI；杆头速度峰可能早于接触 1~2 帧 |
| **B. 杆身 Hough 直线跟踪**（历史重方案简化版） | 全窗口逐帧 HoughLinesP 找杆身→下端点 | 0 | 中 | DTL 40~50% / face-on 30% | **运动模糊是致命伤**：真实视频 Hough 置信度 0.206~0.462，L0 从未出现（QA 报告 §1） |
| **C. 杆身颜色/亮度特征** | ROI 内按金属反光/颜色阈值提杆身→杆头 | 0 | 低 | DTL 40~50% / face-on 30% | 光照/球杆涂装/背景色差异大，需逐视频校准，跨样本不可靠 |
| **D. 球点检测（球+杆头接近）** | Address 帧检静态白球，击球帧取运动质心距球最近者 | 0 | 中 | 球可见时 DTL 85%+ / face-on 60~70% | 球可能不可见/被杆头遮挡；球场网笼后可能有多球干扰 |
| **E. 帧差+杆身组合**（A 定窗口 + 候选帧 B 验证） | 运动峰选 Top-3 候选，仅对候选帧做简化 Hough 验证取杆头最低点 | 0 | 中 | DTL 75~85% / face-on 55~65% | 综合两方案，但 Hough 仅用于验证（候选帧少、可放宽阈值） |

> 预期准确率口径：**「校正后的击球帧落在真实触球瞬间 ±2 采样帧内」的比例（粗估）**。真实素材为 3 段正面 + 6 段 DTL + 2 段 DTL 补充样本，全部 <480 帧、step=1（无降采样），30fps 下 ±2 帧 ≈ ±67ms。

### 2.2 每个候选的核心算法与关键超参

**A. 地面 ROI 帧差运动峰**
```
输入：窗口帧 [i_est - back, i_est + fwd]（array 下标），Address 帧踝关节
1. ROI = [ankle_mid_y + CLUBLITE_ROI_TOP_MARGIN_RATIO×body_h, 图像底边] × 全宽
   （球半径≈12px、杆头贴地，均落在踝关节下方不远处）
2. 对每对相邻帧：diff = |gray[i] - gray[i-1]|，motion[i] = mean(diff[ROI])
3. motion 用 moving_average 平滑（复用 pose_extractor.moving_average）
4. 候选 = motion 的 Top-K（K=CLUBLITE_TOP_K）局部极大值，且 motion ≥ CLUBLITE_MOTION_MIN_RATIO×max(motion)
5. 对每个候选，取 diff>CLUBLITE_DIFF_THRESH 像素的质心 centroid
6. 评分 = 运动强度归一化 × (1 - 质心到地面线的距离/带高) × (球点加权，若检出球)
7. argmax → 新击球帧；无候选 → 降级
```
关键超参：`CLUBLITE_SEARCH_BACK_SEC=0.05`、`CLUBLITE_SEARCH_FWD_SEC=0.25`、`CLUBLITE_ROI_TOP_MARGIN_RATIO=0.02`、`CLUBLITE_MOTION_MIN_RATIO=0.20`、`CLUBLITE_TOP_K=3`、`CLUBLITE_DIFF_THRESH=20`。

机位适配：DTL 下杆头在身体侧前方、贴地可见，ROI 内运动以杆头/杆身为主，效果好；face-on 下杆头击球瞬间可能被手臂/躯干遮挡，运动峰仍强但可能混入躯干运动 → 加"质心贴地"约束缓解，face-on 按 best-effort 处理（见 §7 待确认 Q1）。

**B. 杆身 Hough 跟踪（不推荐单独用）**
```
逐帧：ROI 灰度 → CLAHE → Canny → HoughLinesP(minLineLength=0.35×club_len) 
→ 过滤（过握把/方向一致/不共线骨架）→ 取远离握把端点 = 杆头
```
关键超参：复用 `CLUB_HOUGH_MIN_LEN_RATIO=0.35`、`CLUB_GRIP_DIST_RATIO=0.08` 等历史常量。
不推荐理由：下杆高速段运动模糊抹掉直线边缘，这正是下线重方案的真实失败模式（QA 报告 §1：L0 从未出现、全为 L1 proxy）。

**C. 颜色/亮度特征（不推荐）**
```
ROI 内 HSV 阈值（金属灰/黑/银）→ 形态学 → 最长连通域长轴端点 = 杆头
```
不推荐理由：光照、球杆涂装、背景颜色跨样本差异大，纯阈值方案需要逐视频校准，3~5 天做不出泛化能力。

**D. 球点检测（作为 M1 的评分加权，不单独作主方案）**
```
Address 帧（或站位段静止帧）ROI 内 HoughCircles(半径 5~25px, param2=18)
→ 候选圆；白色 blob 兜底（阈值>200 + 轮廓圆度）
→ 球心 (bx, by) 作为"接触点目标"
```
风险：网笼后多球干扰、球被杆头遮挡、无球场景。因此**不作为主判据**，仅当唯一且高置信时给候选帧评分加权重。

**E. 帧差+杆身组合 = 本方案推荐（M1 + M2）**
```
M1（主）：A 的完整流程，产出 Top-3 候选帧
M2（辅助/兜底）：对 Top-3 候选帧各做一次简化 Hough（不要求全窗口连续跟踪），
   取"杆头端点 y 最低且贴近地面线"的帧；作为评分权重 + 平票 tie-breaker
降级：M1 无候选 / M2 全部失败 → 保持原 locate_impact 结果（estimated 不变）
```

### 2.3 推荐与理由

**主方案 M1（地面 ROI 运动峰 + 球点/贴地约束） + 辅助 M2（候选帧杆身端点验证）**。

理由（为什么最快能出价值）：

1. **只解决"一个帧"问题**：校正目标单一（impact 事件帧），不需要全片杆头轨迹，不需要新指标链路，改动面收敛到 `impact_refiner.py` + `pipeline.py` + `segmenter.py` 三个文件。
2. **信号与物理事实强相关**：地面 ROI 内"谁在动"在挥杆窗口里几乎只有杆头/杆身（站位段双腿基本钉住、球静止），运动峰与"杆头最快≈触球"高度耦合；配合"质心贴地"约束后，等效于"找杆头最低点"，这正是击球瞬间的定义。
3. **零新依赖、复用现成基建**：`frame_reader.grab_frames`（第 2 趟解码共享）、`pose_extractor.moving_average`、`geometry` 全套（`project_along` / `fit_line_2d` / `body_height_px` / `skeleton_polygon_mask`）全部现成。
4. **天然向后兼容**：不改 `locate_impact` 本身（349 个既有测试零回归）；校正结果通过新纯函数 `segmenter.reanchor_impact` 重建 8 事件，单调性不变量由既有 `enforce_monotonic` 保证。
5. **失败代价可控**：检测失败只是"不校正"（回到现状），不引入新的假值；配合 `CLUBLITE_ENABLED=False` 一键关停。

为什么不选其他：
- **B 单独用**：就是下线重方案的真实失败模式（运动模糊 + 直线拟合置信度低），3~5 天重做一遍只会再踩一遍坑；
- **C 单独用**：颜色/光照不可控，需要逐视频标定，超出轻量范围；
- **D 单独用**：依赖"球一定可见"这一用户方无法保证的前提，且网笼/多球场景误检率高；把它降级为评分加权是最稳的用法。

---

## 3. 推荐方案详细设计

### 3.1 新模块 `backend/app/impact_refiner.py`

```
输入：video_path, frames(采样序列), events(8), signals, view, meta, frames_bgr(可选)
输出：ImpactRefineResult（available / new_array_index / delta_frames / confidence / ...）
流程：
  Step 1  窗口规划   plan_refine_frames：array 下标窗口 [i_est-back, i_est+fwd] → 帧号集合（含前一帧）
  Step 2  解码       复用 frames_bgr 或 frame_reader.grab_frames（与 renderer 共享第 2 趟）
  Step 3  地面 ROI   由 Address 帧踝关节 + 图像身高构造
  Step 4  运动信号   _motion_signal：相邻采样帧 ROI 灰度差均值 → 平滑
  Step 5  候选       _pick_candidates：Top-K 局部极大值（带最低比例门槛）
  Step 6  球点(可选) _detect_ball：HoughCircles / 白 blob，唯一高置信才采信
  Step 7  评分       _score：运动强度 × 贴地度 × 球点加权；M2 辅助 _shaft_lowest_y
  Step 8  采纳判定   |delta| ∈ [CLUBLITE_MIN_SHIFT_FRAMES, CLUBLITE_MAX_SHIFT_FRAMES]
                    → 返回 available=True；否则 available=False（调用方保持原 events）

🔴 模块级硬约束（与 club_detector 相同）：本模块禁止外抛异常。
任何失败（解码失败、关键点缺失、OpenCV 报错……）都被 refine_impact 吞掉，
统一返回 ImpactRefineResult(available=False)。主链路不能被这个增量特性拖垮。
```

### 3.2 为什么不动 `locate_impact`

`locate_impact` 是 349 个测试覆盖的既有纯函数，其"手腕回落 + 速度峰"作为**粗定位**依然有价值（给出校正搜索的锚点窗口）。校正作为**后置精修**叠加，风险隔离最干净：若校正失败，系统行为与现状完全一致。

### 3.3 时序预算

- 窗口帧数：back=0.05s + fwd=0.25s ≈ 9~10 帧 + 1 帧前向差分 ≈ **≤ 12 帧**（30fps 采样帧）。
- 解码：并入现有第 2 趟 grab（与 renderer 共享），解码趟数**保持 2 趟不变**；窗口帧在 refine 后立即释放（沿用 pipeline 现有 `frames_bgr` 过滤逻辑）。
- 计算：每窗口 ≤ 12 帧 absdiff + 阈值 + 质心 ≈ **< 50ms**；Hough 验证仅 3 帧 ≈ 10ms。
- 内存峰值：12 帧 720×1280×3 ≈ 33MB 瞬态，单 worker 可接受（refine 后释放）。

---

## 4. 接口设计

### 4.1 模块 API（`impact_refiner.py`）

```python
# ---- 内部数据结构（schemas.py，与 ClubTrack 同级，不出网）----------------

@dataclass
class ImpactRefineResult:
    """击球帧校正结果。硬约束：任何失败 -> available=False。"""
    available: bool = False
    method: str = "none"          # "motion" | "motion+shaft" | "none"
    old_array_index: int = -1     # 校正前 impact 的 array 下标
    new_array_index: int = -1     # 校正后 impact 的 array 下标
    delta_frames: int = 0         # new - old（array 下标差 = 采样帧差）
    confidence: float = 0.0       # 0~1
    ball_detected: bool = False
    motion_peak_index: Optional[int] = None
    shaft_lowest_index: Optional[int] = None

# ---- 纯函数（可单测）-------------------------------------------------------

def plan_refine_frames(
    events: Sequence[SwingEvent],
    signals: SwingSignals,
    meta: VideoMeta,
    back_sec: float = config.CLUBLITE_SEARCH_BACK_SEC,
    fwd_sec: float = config.CLUBLITE_SEARCH_FWD_SEC,
) -> Tuple[List[int], List[int]]:
    """返回 (候选帧号升序, 需解码帧号升序[含前一帧])。纯函数，无 IO。"""

def _ground_roi(
    addr_lm: FrameLandmarks, width: int, height: int, body_h_px: float
) -> Tuple[int, int, int, int]:
    """地面带 ROI (x0,y0,x1,y1)：上边界 = 踝关节 y + 0.02×身高，下到图像底。"""

def _motion_signal(gray_frames: Sequence[np.ndarray], roi: Tuple[int, ...]) -> np.ndarray:
    """相邻帧 ROI 灰度差均值序列（长度 = len-1），含平滑。"""

def _detect_ball(addr_bgr: np.ndarray, roi: Tuple[int, ...]) -> Optional[np.ndarray]:
    """HoughCircles / 白 blob 检球；唯一且高置信才返回 (cx, cy)，否则 None。"""

def _pick_candidates(motion: np.ndarray, min_ratio: float, top_k: int) -> List[int]:
    """运动峰 Top-K 局部极大值（array 下标，相对窗口起点偏移）。"""

def _shaft_lowest_y(
    bgr: np.ndarray, landmark_px: np.ndarray, grip_px: np.ndarray,
    club_len_px: float, view: CameraView,
) -> Optional[float]:
    """单帧简化 Hough：ROI 内找过握把的杆身线，返回杆头端点 y（越低越贴地）。"""

# ---- 主入口（永不外抛）-----------------------------------------------------

def refine_impact(
    video_path: str,
    frames: Sequence[FrameLandmarks],
    events: Sequence[SwingEvent],
    signals: SwingSignals,
    view: CameraView,
    meta: VideoMeta,
    frames_bgr: Optional[Dict[int, np.ndarray]] = None,
) -> ImpactRefineResult:
    """击球帧校正主入口。任何失败返回 available=False。"""
```

### 4.2 `segmenter.py` 新增纯函数（重建 8 事件）

```python
def reanchor_impact(
    frames: Sequence[FrameLandmarks],
    signals: SwingSignals,
    events: Sequence[SwingEvent],
    new_impact_array_index: int,
) -> Optional[List[SwingEvent]]:
    """用校正后的击球帧重建 8 事件。

    1. 用新 impact 替换旧 impact（frame_index/timestamp/array_index/estimated=False）；
    2. 重跑 locate_intermediate（②③⑤⑦ 依赖 impact 边界）→ 新中间四帧；
    3. enforce_monotonic_indices + _assemble 重建事件；
    4. 任何冲突（AnalysisError）→ 返回 None，调用方保持原 events（保守降级）。
    """
```

### 4.3 `pipeline.py` 集成点（插入 step 4a）

```
step1 校验   probe_video / check_brightness
step2 姿态   pose_extractor.extract                        ← 第 1 趟全量解码
step3 切分   segmenter.segment_swing                        ← 粗定位（含 impact 估计）
step4a ★    view_detector.resolve（机位解析，addr 不变）
            impact_refiner.plan_refine_frames(events, signals, meta)
            frame_reader.grab_frames(video, 事件帧 ∪ 校正窗口帧)   ← 第 2 趟共享解码
            impact_refiner.refine_impact(..., frames_bgr)
            if refine.available and |delta| ∈ [MIN, MAX]:
                events = segmenter.reanchor_impact(frames, signals, events, refine.new_array_index)
                if events 不为 None and |delta| ≥ CLUBLITE_WARN_THRESHOLD_FRAMES:
                    warnings.append(config.WARN_IMPACT_REFINED)
            frames_bgr = {仅 8 事件帧}（释放窗口帧，内存峰值锁回 8 帧）
step4b 指标   metrics.build_context / compute_phase_metrics（基于校正后 events）
step4c 风险   risk_engine.evaluate_all
step4d 渲染   renderer.render_events(..., frames_bgr)（8 帧不变）
```

**要点**：
- 解码趟数保持 **2 趟**（`frame_reader.stats()["opens"] <= 2` 回归测试继续成立）；
- `view_detector.resolve` 使用 Address 帧，校正不改变 Address，顺序无冲突；
- 校正后 impact 的 `estimated` 置为 `False`（有真实杆头/球证据），`PhaseResult.estimated` 随之变化；
- 若 `reanchor_impact` 返回 None（单调性冲突）→ 丢弃校正，保持原 events，**绝不让校正破坏主链路**。

### 4.4 与 metrics 的协作

- **现有指标自动受益**：impact 阶段指标（`hip_open` / `shoulder_square` / `spine_tilt_delta` / `pelvis_shift_pct`）通过 `ctx.frame_of(PhaseKey.IMPACT)` 取帧，校正后自动在真实击球帧计算；全程指标 `tempo_ratio` 用 impact 的 array 下标，同样自动更新。
- **新增指标（P2 可选，本次不强制）**：`impact_frame_shift`（诊断性，帧数 = 校正后 - 校正前）落 IMPACT 阶段，`allow_drop=True`；若加需同步 `reference.py` spec + `metrics.py` 函数 + `METRIC_FUNCS`（注意 metrics 底部导入期自检）。**v3 核心范围建议不加**，避免连锁改动，先以 warning 呈现校正事实。

### 4.5 数据契约（Pydantic / dataclass 新增）

| 位置 | 新增 | 说明 |
|---|---|---|
| `schemas.py` | `ImpactRefineResult` dataclass | 内部结构，不出网 |
| `AnalysisResult` | 无新增字段 | 校正体现为 impact 事件本身的 `frame_index/timestamp/estimated` 变化 + `warnings` 追加 |
| `StageMetric` | 无新增字段 | 现有 `estimated/source/confidence` 已够用 |
| config.py §8 | `CLUBLITE_*` 常量 + `WARN_IMPACT_REFINED` | 见 §4.6，**不动已有常量** |

### 4.6 降级策略（简化 2 级，替代原 L0/L1/L2 三级）

| 级别 | 触发条件 | 行为 |
|---|---|---|
| **G1 校正成功** | `refine.available=True` 且 `|delta| ∈ [CLUBLITE_MIN_SHIFT_FRAMES, CLUBLITE_MAX_SHIFT_FRAMES]` | 采纳校正；`estimated=False`；`|delta| ≥ 3` 时追加 `WARN_IMPACT_REFINED` |
| **G0 保持原状** | 其余一切情况（未启用 / 无候选 / 置信不足 / 超上限 / 单调性冲突） | **不做任何修改**，impact 保持 `locate_impact` 结果，`estimated` 不变，仅 DEBUG 日志记录原因 |

不引入用户可见错误码、不阻断任务、不造假值——这是轻量方案与三级降级方案的本质区别：**检测失败 = 回到现状，而不是新增一个"估算"状态**。

### 4.7 配置常量（config.py §8 新追加子块，勿动 CLUB_*）

```python
# ---------------------------------------------------------------------------
# 8b. 轻量击球帧校正（ARCHITECTURE-v3-clublite.md，2026-08 新追加）
# ⚠️ 与 8a 归档的 CLUB_*（重球杆检测）无关；本块前缀 CLUBLITE_。
# ---------------------------------------------------------------------------

#: 击球帧校正总开关。False 时 pipeline 完全不调用 impact_refiner（一键关停）
CLUBLITE_ENABLED: Final[bool] = True

#: 校正搜索窗口（相对腕部击球估计，秒）。腕部估计天然偏早，主要靠向前探测
CLUBLITE_SEARCH_BACK_SEC: Final[float] = 0.05
CLUBLITE_SEARCH_FWD_SEC: Final[float] = 0.25

#: 地面 ROI 上边界 = Address 帧踝关节 y + 该比例×图像身高（y 向下）。
#: 球（半径≈12px）与杆头（贴地）均落在踝关节下方不远处
CLUBLITE_ROI_TOP_MARGIN_RATIO: Final[float] = 0.02

#: 运动峰候选最低强度 = 该比例 × 窗口内最大运动强度（低于 → 无显著运动 → 降级）
CLUBLITE_MOTION_MIN_RATIO: Final[float] = 0.20

#: 运动峰候选数（评分后取最优；M2 仅对这 K 帧做 Hough 验证）
CLUBLITE_TOP_K: Final[int] = 3

#: 帧差二值化阈值（灰度差）
CLUBLITE_DIFF_THRESH: Final[int] = 20

#: 校正最少移动帧数（< 该值不采纳，避免帧级抖动）
CLUBLITE_MIN_SHIFT_FRAMES: Final[int] = 1

#: 校正最多移动帧数（超过视为检测不可信，不采纳）
CLUBLITE_MAX_SHIFT_FRAMES: Final[int] = 12

#: 校正幅度 ≥ 该帧数时追加 warning（WARN_IMPACT_REFINED）
CLUBLITE_WARN_THRESHOLD_FRAMES: Final[int] = 3

#: 球检测：HoughCircles 半径范围（像素，按身高先验 1px≈1.75mm，球半径≈12px）
CLUBLITE_BALL_RADIUS_PX: Final[Tuple[int, int]] = (5, 25)
#: HoughCircles 累加器阈值（越高越保守，仅唯一高置信才采信）
CLUBLITE_BALL_PARAM2: Final[int] = 18

#: （可选 P2）渲染 impact 帧球点/杆头标记
CLUBLITE_DRAW_MARKER: Final[bool] = False

#: 校正提示文案（追加进 warnings）
WARN_IMPACT_REFINED: Final[str] = "击球帧已按杆头/球位置校正"
```

---

## 5. 任务列表（3 批次 · 4 任务 · 5 个工作日）

> 依赖链：T01 → T02 → T03；T04 依赖 T02（T03 完成与否不影响 T04 主流程验收，M2 属加分项）。

### 批次 1（Day 1 上午）：契约 + 常量 + 模块骨架

**T01｜数据契约与模块骨架（P0，依赖：无）**
| 项 | 内容 |
|---|---|
| 新增文件 | `backend/app/impact_refiner.py`（骨架：`refine_impact` 签名 + `plan_refine_frames` 纯函数实现 + 其余返回默认/空 + 模块级 try/except 包装）<br>`backend/tests/test_impact_refiner.py`（骨架用例，见 §6） |
| 修改文件 | `backend/app/config.py`（§8 追加 `CLUBLITE_*` 子块，**不动已有常量**）<br>`backend/app/schemas.py`（新增 `ImpactRefineResult` dataclass） |
| 完成标志 | `pytest backend/tests -q` 全绿（349+新增骨架用例）；`CLUBLITE_ENABLED=False` 时 `refine_impact` 不解码（`frame_reader.stats()["opens"]` 不增长）且返回 `available=False` |

### 批次 2（Day 1 下午 ~ Day 3）：核心校正 + 管线集成

**T02｜地面 ROI 运动峰校正核心 + 管线集成（P0，依赖：T01）**
| 项 | 内容 |
|---|---|
| 修改文件 | `backend/app/impact_refiner.py`（实现 `_ground_roi` / `_motion_signal` / `_detect_ball` / `_pick_candidates` / `_score` / 完整 `refine_impact`）<br>`backend/app/segmenter.py`（新增纯函数 `reanchor_impact`）<br>`backend/app/pipeline.py`（step4a 集成：合并解码窗口帧、调用 refine、reanchor、warnings、释放窗口帧）<br>`backend/tests/test_impact_refiner.py`（合成视频用例，见 §6） |
| 完成标志 | 合成视频用例绿（球/无球/遮挡/低对比/顶盖）；pipeline 端到端 `frame_reader.stats()["opens"] <= 2`；`reanchor_impact` 单调性冲突返回 None 且主链路不崩 |

### 批次 3（Day 4）：辅助验证 + 可选渲染标注

**T03｜候选帧杆身端点验证（M2） + 可选渲染标注（P1，依赖：T02）**
| 项 | 内容 |
|---|---|
| 修改文件 | `backend/app/impact_refiner.py`（实现 `_shaft_lowest_y` 简化 Hough，接入 `_score` 评分权重与 tie-breaker）<br>`backend/app/renderer.py`（可选：`CLUBLITE_DRAW_MARKER=True` 时 impact 帧画球点/杆头圈，低置信虚线）<br>`backend/tests/test_impact_refiner.py`（杆身验证用例） |
| 完成标志 | M2 在候选帧上给出杆头最低点且与 M1 结论一致的用例通过；`CLUBLITE_DRAW_MARKER=False` 时 renderer 输出与现状逐字节一致 |

### 批次 4（Day 5）：真实视频校准 + 验收报告

**T04｜真实视频 E2E 校准 + VALIDATION-CLUBLITE 报告（P0，依赖：T02）**
| 项 | 内容 |
|---|---|
| 新增文件 | `backend/_probe_out/probe_clublite.py`（对 11 段真实视频跑校正，输出 delta 表）<br>`docs/VALIDATION-CLUBLITE.md`（验收报告） |
| 修改文件 | `backend/app/config.py`（按实测微调 §8 CLUBLITE_* 阈值，附依据注释） |
| 完成标志 | §6.2 验收标准达成；报告落盘；`pytest` 全绿 |

---

## 6. 测试策略

### 6.1 单元测试场景（10 个，`tests/test_impact_refiner.py`）

基于 conftest 的合成视频能力（`synth_video` fixture 可渲染骨架 + 自绘杆/球），新增一个"带杆+球"的合成视频构造（画一条从握把指向地面球的亮线 + 地面白球，球在已知帧被杆头覆盖）：

| # | 场景 | 断言 |
|---|---|---|
| 1 | 合成"杆头贴球"视频（球在帧 t_c 被杆头覆盖） | `refine_impact` 返回的 `new_array_index` 与注入真值 t_c 差 ≤ 1 帧 |
| 2 | 无球场景（仅杆头贴地线，无白球） | 仍能通过"质心贴地"约束校正，`ball_detected=False` 但 `available=True` |
| 3 | 静止/无挥杆（`still_frames` 对应视频） | `available=False`，`method="none"` |
| 4 | ROI 全黑（底部遮挡带） | 降级 `available=False`，不抛异常 |
| 5 | 低对比（运动强度 < MIN_RATIO） | 降级 `available=False` |
| 6 | `_detect_ball`：ROI 内画白色圆 | 返回球心，误差 < 3px；多圆/无圆返回 None |
| 7 | `plan_refine_frames` 窗口边界 | 候选帧 ∈ [i_est-back, i_est+fwd]，含前一帧，去重升序，不越界 |
| 8 | `CLUBLITE_ENABLED=False` | `refine_impact` 直接 `available=False` 且 `frame_reader.stats()["opens"]` 不增 |
| 9 | `reanchor_impact` 单调性冲突（新 impact ≥ finish） | 返回 `None`，原 events 不变 |
| 10 | `CLUBLITE_MAX_SHIFT_FRAMES` 顶盖（注入远超上限的峰） | 不采纳（`available=False` 或 delta 被拒），不产生异常大跳变 |

### 6.2 端到端验证（VALIDATION-CLUBLITE.md）

- **素材**：`.tools/_probe/samples/`（正面1/2/3 + 6 DTL）+ `video/`（2 DTL）共 11 段。
- **基线**：`docs/VALIDATION-A.md` §3 的切分结果（或当前 segmenter 重跑）——以各视频 impact 帧号/时间戳为对照基线。
- **步骤**：`probe_clublite.py` 对每段输出 `old_impact / new_impact / delta_frames / ball_detected / method / confidence`；人工抽查 ≥ 3 段渲染截图（校正后 impact 帧杆头是否落在球/地面线）。
- **验收标准**：
  1. 至少 5/9 段（成功切分段）`delta_frames > 0` 且截图确认杆头在球/地面线（校正有效）；
  2. 全部视频 `delta_frames ∈ [-2, +12]`（无异常后跳）；
  3. 无任何视频因校正产生 `AnalysisError` / 阶段顺序破坏；
  4. 全量 `pytest` 349+ 通过；`frame_reader.stats()["opens"] <= 2`；单任务墙钟增量 < 0.5s；
  5. 抽查 impact 阶段指标（`shoulder_square` / `hip_open`）校正前后数值合理（量纲无突变）。

---

## 7. 风险与待确认

### 7.1 关键风险（影响 3~5 天交付）

| # | 风险 | 等级 | 缓解 |
|---|---|---|---|
| R1 | 运动峰 ≠ 触球瞬间（杆头速度峰可略早于接触 1~2 帧） | 中 | 30fps 下 ±1~2 帧即 ±67ms，相对现状 10+ 帧偏差是量级改善；球点/贴地约束拉回最低点 |
| R2 | face-on 击球瞬间杆头被身体遮挡 | 高 | 地面 ROI 仍能捕获杆头进入/离开遮挡的运动峰；face-on 按 best-effort，失败即降级（Q1 需用户拍板是否强需求） |
| R3 | ROI 内混入脚/腿运动（脚跟抬起等） | 中 | ROI 上边界取踝关节下方、贴地带收窄；"质心贴地"评分抑制高位运动 |
| R4 | 低光照/背景杂乱导致帧差阈值失效 | 中 | G0 降级保持现状；`CLUBLITE_ENABLED` 一键关停 |
| R5 | 校正后 impact 影响既有指标数值（预期效果，但需回归） | 低 | 校正仅后移 ≤12 帧，impact 阶段 4 指标 + tempo 同步重算；VALIDATION 抽查量纲 |
| R6 | 网笼/多球场景球检测误检 | 中 | 球检测仅作评分加权且要求"唯一高置信"，不采信即跳过 |

### 7.2 待用户确认

| # | 问题 | 为什么关键 | 默认建议 |
|---|---|---|---|
| **Q1** 🔴 | **是否要求正面（face-on）机位也校正？** | face-on 下杆头击球瞬间常被躯干/手臂遮挡，准确率显著低于 DTL（预期 50~60% vs 75~85%）。若主要用户场景是 DTL（侧面），可优先保证 DTL、face-on best-effort | **DTL 为主、face-on 尽力而为**（失败自动降级） |
| **Q2** 🔴 | **球是否总是可见（白色小球）？** | 球可见时可用"球点接触"判定（准确率最高 85%+）；球不可见时退化为"杆头贴地最低点"（仍可用但略低） | 按"球不一定可见"设计（M1 不依赖球），球点仅作加权 |
| **Q3** 🟡 | **允许击球帧最多后移多少帧？** | 决定 `CLUBLITE_MAX_SHIFT_FRAMES` 顶盖（默认 12 帧 ≈ 0.4s@30fps）。顶盖过小可能拦掉真实校正，过大可能引入误检 | 默认 12 帧，VALIDATION 后按实测收紧 |
| **Q4** 🟡 | **是否接受校正后 impact 帧截图/指标变化（这正是预期效果）？是否需要保留"原始帧"对照展示？** | 产品呈现层面：结果页击球截图会变，可能让老用户困惑 | 追加 `WARN_IMPACT_REFINED` 提示；不做双帧对照（保持简单） |
| **Q5** 🟢 | **是否需要前端展示"已校正"标识？** | 影响小程序改动范围 | 本期后端 warnings 已带文案，前端是否展示由 PM 定 |

---

## 8. 与下线"重球杆检测"的对比（为什么这次会不一样）

| 维度 | 下线重方案（club_detector.py） | 本次轻量方案（impact_refiner.py） |
|---|---|---|
| 目标 | 全片杆头**像素级几何定位**（每帧 head 坐标 + swing_plane 系列指标） | 只校正**一个帧**（impact 事件帧的时序） |
| 检测对象 | 逐帧杆身直线 + 杆头端点（需高置信） | 地面 ROI 运动峰 + 质心贴地（帧级二选一） |
| 真实视频结果 | 置信度 0.206~0.462，L0 从未出现（QA §1） | 目标 ≥75%（DTL）在 ±2 帧内命中 |
| 复杂度 | ROI 扇形 + 时序预测 + Hough + 帧差 + 平滑 + 三级降级 + ONNX 预留 | absdiff + 阈值 + 质心 + 3 帧简化 Hough 验证 |
| 失败代价 | 三级降级引入 L1/L2 估算态 | G0 直接回到现状，无新状态 |
| 新增依赖 | 0（但预留 ONNX 路径） | **0（纯 OpenCV/numpy，已就绪）** |

---

*报告完毕。核心结论：只做帧级时序校正、不做像素级定位，是轻量方案能在 3~5 天交付且真实有效的关键；推荐 M1（地面 ROI 运动峰）+ M2（候选帧杆身验证）组合，零新依赖，失败自动降级。*
