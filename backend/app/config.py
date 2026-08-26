"""全局可调常量。

本模块**不依赖项目内任何其他模块**，可被任意模块安全导入（杜绝循环导入）。

内容分区：
    1. 目录与运行时
    2. 上传与任务约束
    3. 视频探测 / 姿态提取
    4. 8 阶段切分算法参数（架构文档 §7.7）
    5. 符号约定常量（架构文档 §10.3）—— 符号校准只改这里
    6. 渲染
    7. 文案（错误码中文映射、免责声明）
    8. 球杆检测（club-detection-design.md §5.2 T01）
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Final, FrozenSet, Tuple

# ---------------------------------------------------------------------------
# 1. 目录与运行时
# ---------------------------------------------------------------------------

#: ``backend/`` 目录
BASE_DIR: Final[Path] = Path(__file__).resolve().parent.parent

#: 任务数据根目录，同时也是 ``/static`` 的挂载目录
DATA_DIR: Final[Path] = Path(
    os.getenv("GOLF_DATA_DIR", str(BASE_DIR / "data" / "tasks"))
).resolve()

#: 对外可访问的基地址；``image_url`` 由后端拼成绝对 URL 下发
PUBLIC_BASE_URL: Final[str] = os.getenv(
    "GOLF_PUBLIC_BASE_URL", "http://127.0.0.1:8000"
).rstrip("/")

#: 锁定的 MediaPipe 版本（健康检查会原样下发）
MEDIAPIPE_VERSION: Final[str] = "0.10.14"

#: 日志格式
LOG_FORMAT: Final[str] = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
LOG_LEVEL: Final[str] = os.getenv("GOLF_LOG_LEVEL", "INFO")


# ---------------------------------------------------------------------------
# 2. 上传与任务约束
# ---------------------------------------------------------------------------

#: 上传文件大小上限（40MB）
MAX_UPLOAD_BYTES: Final[int] = 40 * 1024 * 1024

#: 允许的扩展名（PDD 要求放开 .mov）
ALLOWED_VIDEO_EXTS: Final[FrozenSet[str]] = frozenset({".mp4", ".mov"})

#: 允许的 content-type（部分客户端会传 application/octet-stream）
ALLOWED_CONTENT_TYPES: Final[FrozenSet[str]] = frozenset(
    {
        "video/mp4",
        "video/mpeg4",
        "application/mp4",
        "video/quicktime",
        "application/octet-stream",
        "",
    }
)

#: 上传文件名（落盘固定名，兼容旧路径）
UPLOAD_FILENAME: Final[str] = "upload.mp4"


def upload_filename(ext: str) -> str:
    """按原始扩展名生成落盘固定名（PDD 放开 .mov 后使用）。

    Args:
        ext: 扩展名，可带点（``".mov"``）或不带（``"mov"``）。
    """
    suffix = ext.lower() if ext.startswith(".") else f".{ext.lower()}"
    return f"upload{suffix}"

#: 单任务处理超时（秒）
TASK_TIMEOUT_SEC: Final[float] = 120.0

#: 结果目录保留时长（小时），7 天
RESULT_TTL_HOURS: Final[float] = 168.0

#: 同时进行的分析任务软上限
MAX_CONCURRENT_TASKS: Final[int] = 2

#: 分析成功后是否立即删除原视频（PRD Q6）
DELETE_UPLOAD_AFTER_SUCCESS: Final[bool] = True


# ---------------------------------------------------------------------------
# 3. 视频探测 / 姿态提取
# ---------------------------------------------------------------------------

#: 服务端放宽后的时长边界（前端已按 2~20s 拦截）
MIN_DURATION_SEC: Final[float] = 1.5
MAX_DURATION_SEC: Final[float] = 20.0

#: 推理帧数上限，超出则等间隔降采样
MAX_INFER_FRAMES: Final[int] = 480

#: 推理前把画面短边缩放到该值以内
INFER_SHORT_SIDE: Final[int] = 480

#: 亮度探测：等间隔抽帧数量与灰度均值下限
BRIGHTNESS_SAMPLE_FRAMES: Final[int] = 10
DARK_MEAN_THRESHOLD: Final[float] = 40.0

#: 未检出人体帧占比阈值
MISS_RATIO_NO_PERSON: Final[float] = 0.50

#: 低质量判定阈值（低于该比例视为可分析，高于则提示 LOW_QUALITY）。
#: 0.10 -> 0.15：实测 DTL-4e8d0d7e miss_ratio=0.133（即 86.7% 帧成功检出），
#: 放宽后切分完全正常（8 阶段齐全、Top 肩转 +76.8° 符号量纲均正确），旧阈值把
#: 一段「其实可分析」的视频误杀成 LOW_QUALITY；0.15 仍能在 >15% 漏检时提示不稳定，
#: 与 MISS_RATIO_NO_PERSON=0.50 形成「提示→硬失败」两段式分级。
MISS_RATIO_LOW_QUALITY: Final[float] = 0.15

#: 核心 13 点平均 visibility 低于该值即视为"低质量帧"
LOW_VIS_THRESHOLD: Final[float] = 0.50
#: 低质量帧占比超过该值 -> LOW_QUALITY
LOW_VIS_FRAME_RATIO: Final[float] = 0.30

#: fps 低于该值标记 low_fps
LOW_FPS_THRESHOLD: Final[float] = 30.0

#: MediaPipe legacy solutions.Pose 构造参数（严禁改用 tasks API）
POSE_KW: Final[Dict[str, object]] = {
    "static_image_mode": False,
    "model_complexity": 1,
    "smooth_landmarks": True,
    "enable_segmentation": False,
    "min_detection_confidence": 0.5,
    "min_tracking_confidence": 0.5,
}


# ---------------------------------------------------------------------------
# 4. 8 阶段切分算法参数（架构文档 §7.7，工程师照此调参）
#
# ⚠️ 标尺口径（2026-08 真实视频校准后变更，见 docs/VALIDATION-A.md）：
#    以下所有「肩宽制」阈值的基准，是 :func:`app.segmenter.build_signals` 换算到
#    **各向同性图像宽度单位**后的肩宽。此前 y 方向未做 ``H/W`` 校正，竖屏
#    720×1280 与横屏 1280×720 之间阈值实际漂移 3.2 倍。校正后旧阈值需整体
#    ×1.78（竖屏换算系数）才等效，下面的新默认值即据此重算并用 11 段真实视频回归。
# ---------------------------------------------------------------------------

#: 滑动平均窗口时长（秒）
SMOOTH_WIN_SEC: Final[float] = 0.08

#: 静止判定速度阈值（肩宽/秒）。
#: 0.25 -> 0.55：换算系数 1.78 折算得 0.45，再上调至 0.55。
#: 依据：真实视频站位段并非绝对静止（球手 waggle / 重心调整），实测 470057ac
#: 站位段速度在 0.2~1.5 间抖动，0.45 只能捞到零星 3 帧静止段，Address 被定位到
#: 真实站位前 1s；0.55 后 8/9 段视频的 Address / Finish 静止段判定命中。
V_STILL: Final[float] = 0.55

#: 判定"存在挥杆"的最小速度峰值（肩宽/秒）。
#: 1.5 -> 2.7（= 1.5 × 1.78）。实测真实挥杆速度峰 9.99~23.92，余量充足；
#: 静止站立视频速度峰 < 1，判据依然成立。
V_PEAK_MIN: Final[float] = 2.7

#: Address 静止段最短时长（秒）
STILL_MIN_SEC_ADDR: Final[float] = 0.10

#: Finish 静止段最短时长（秒）
STILL_MIN_SEC_FINISH: Final[float] = 0.15

#: Impact 高度回落容差（肩宽）。
#: 0.15 -> 0.35：换算系数折合 0.27，再放宽。依据：实测击球帧手位普遍略高于
#: Address（470057ac 高 0.42 肩宽、正面2 高 0.31 肩宽）——平滑窗抹平了手腕
#: 过底点的瞬时最低位。容差不足会让高度穿越分支整体失效、退化成纯速度峰。
IMPACT_Y_TOL: Final[float] = 0.35

#: Impact 速度峰搜索半窗（秒）
IMPACT_WIN_SEC: Final[float] = 0.05

#: 手腕过髋线判据（肩宽）
H_HIP: Final[float] = 0.18

#: ⑤下杆专用：腕降到"髋线附近"（比 ②/⑦ 共享的 H_HIP 更早触发，避免⑤⑥重叠）。
#: ⚠️ 实际采用值 0.50（2026-08 用户最后一次判据微调，22030124 实测 ⑤=113）：
#: 方案草案 0.05 在用户样本上首次下穿发生在 refined impact(115) 当帧，触发
#: ⑤/⑥ 间距守卫回退兜底（⑤=111，偏差 -2）；因下杆期 ``h`` 单调递减，**阈值越高
#: 越早触发**，要拿到 113（该帧 ``h=0.432``）阈值须落在 ``[0.432, 0.997)``，
#: 故取区间最小值 0.50（尽量贴近「腕接近髋线」语义；22030124 ⑤=113，真实视频
#: 回归 ④<⑤<⑥ 全绿，见 ``_probe_out/probe_h_downswing_sweep.py``）。
H_DOWNSWING: Final[float] = 0.50

#: ⑤下杆专用（**DTL 侧面机位**）：与 :data:`H_DOWNSWING` 语义相同（手腕高度
#: ``h`` 首次下穿阈值 = ⑤），但阈值更低 → 触发更晚（偏离顶点、更靠后、更接近击球）。
#:
#: ⚠️ 分机位原因：face-on / DTL 下人体投影不同（DTL 双肩前后重叠、肩宽压缩，
#: 手腕相对髋部的投影行程/时序与正面不同），下杆判据本应分机位。face-on 恒用
#: :data:`H_DOWNSWING`（0.50，正面回归逐字节一致，用户已验收）；DTL 用本值。
#:
#: 校准值 **0.40**（2026-08 Step 2 DTL 身高制阈值重标，5 段样本扫描，见
#: ``_probe_out/step2_dtl_sweep.py``）。因下杆期 ``h`` 单调递减，**身高制下
#: 阈值反而比肩宽制高**——Step 1 把 DTL 信号标尺从肩宽改为身高（NOSE→双踝
#: 中位数）后，h 范围约 0.6~0.7（肩宽制时 0.05~0.30），要拿到「距⑥ 2-5 帧、
#: 腕接近髋」的真实穿越，阈值须落在 h 衰减末段。Step 2 扫描证据：
#:
#:   - thr=0.40 让 3/4 样本 ⑤ 变 real（0.05~0.35 全走兜底）：
#:     - 11a6594b：⑤ 205(est) → 207(real)，距④ 5 帧、距⑥ 1 帧（腕接近髋，刚击球）；
#:     - c6f67f38：⑤ 177(real) 不变，距④ 8 帧、距⑥ 2 帧（最佳区间）；
#:     - 470057ac：⑤ 98(real) 不变，距④ 6 帧、距⑥ 5 帧（最佳区间）；
#:     - f470c599：⑤ 仍 236(est)（下杆窗仅 3 帧、h 急剧下降，物理上 ⑤ 必贴近击球）；
#:   - thr=0.30 让 c6f67f38/470057ac real、11a6594b/f470c599 仍 est（2/4 real）；
#:   - thr=0.20 同上（2/4 real，且 c6f67f38 ⑤ 距⑥ 仅 1 帧太紧）。
#:
#: 取 0.40（3/4 real，c6f67f38/470057ac ⑤ 在「距②⑤⑥ 目标 2-5 帧」区间）。
#: ⚠️ **鲁棒性**：依赖 DTL 样本 h_top > 0.40（4 样本实测 0.442~0.724）；低于
#: 该值则 ⑤ 走兜底（不影响功能、仅丢 real 标签）。⚠️ 低于 0.35 会让 11a6594b
#: ⑤ 退化为 205 估算帧（反而更早），**不要**调更低。
H_DOWNSWING_DTL: Final[float] = 0.40

#: 最小垂直行程（肩宽）。0.60 -> 1.07（= 0.60 × 1.78）。
#: 实测 9 段可切分视频换算后行程 2.1~6.7，静止视频 ≈ 0，判据区分度充足。
MIN_WRIST_TRAVEL: Final[float] = 1.07

#: ②③⑤⑦ 兜底比例
FALLBACK_RATIO: Final[Tuple[float, float, float, float]] = (0.35, 0.70, 0.50, 0.35)

#: 顶点搜索时排除首尾比例
TOP_SEARCH_MARGIN: Final[float] = 0.05

#: 顶点速度反向点精修半窗（秒）
TOP_REFINE_SEC: Final[float] = 0.10

#: Address -> Top 的最短时长（秒），过短判 NO_SWING。
#: 0.15 -> 0.45。依据：11 段真实视频中，7 段正常切分的 Address->Top 实测
#: 0.80~3.63s（最小 0.80s）；2 段「视频起点已在上杆中、根本没拍到站位」的样本
#: （087d40a0 / 707fb04a）分别只有 0.13s 与 0.33s。0.45s 可干净分开两类，
#: 让残缺视频得到诚实的 NO_SWING 而不是 8 个挤在 0.2s 内的垃圾阶段。
MIN_TOP_ADDR_SEC: Final[float] = 0.45

#: ①Address→④Top 最小时长（秒），**DTL 侧面机位专用**（2026-08 方案 C 用户拍板）。
#: DTL 拍摄时人物往往先准备 1~2 秒不动（找球/调整），``locate_address`` 找不到真实
#: 静止段 → address 估计靠后 → address→top 被挤压（实测 11.mp4 仅 10 帧）→ 触发
#: ``MIN_TOP_ADDR_SEC=0.45`` 守卫误杀正常挥杆。故 DTL 放宽到 0.30s（30fps 下
#: 9 帧门槛，10 帧可通过）；face-on 保持 :data:`MIN_TOP_ADDR_SEC`（0.45）逐字节不变。
MIN_TOP_ADDR_SEC_DTL: Final[float] = 0.30

#: locate_address 候选静止段的「髋部相对手位高度」上限。
#: 站位的手位贴近髋线（``h≈0``，可略负），而**顶点前减速微停**发生在 ``h≈2``
#: （手已高举、手腕瞬时变慢），后者会被 :data:`V_STILL` 误判成一段静止，从而把
#: Address 定位到顶点前几帧、把 Address→Top 挤压成 1 帧触发假 ``NO_SWING``
#: （实测 正面2：真实 Address 在 h≈-0.3 的低手位，顶点前微停 h≈2.0）。用该上限
#: 过滤掉高点假静止，只对低手位的静止段视为 Address。
ADDR_H_MAX: Final[float] = 0.6

#: Top -> Impact 的最短时长（秒），过短判 NO_SWING
MIN_IMPACT_TOP_SEC: Final[float] = 0.06

#: Top -> Impact 的最长时长（秒）。用于把 :func:`app.segmenter.locate_impact` 的搜索
#: 区间限制在物理可行范围，避免顶点后长时间举杆时把击球定位到几秒之后。
#: 0.60 -> 1.5：实测 DTL-470057ac 因顶点后手位长期高于容差带、首个高度回落发生在
#: 95 帧（3.17s）之后，必须用上界拦掉这个假下杆；但 0.60s 同时把一段慢动作/慢挥
#: 视频（正面2）的真实击球（手位回到 Address 高度约在顶点后 1.17s）也截断在边界、
#: 误把下杆压成 0.6s。1.5s 仍远小于 470057ac 的 3.17s（继续拦掉假下杆），却足以
#: 容纳慢挥与慢动作视频的真实下杆，使 impact 落回「手位回到 Address 高度」的语义点。
MAX_DOWNSWING_SEC: Final[float] = 1.5

#: Finish 兜底 A 所需的击球后最短余量（秒）
FINISH_FALLBACK_SEC: Final[float] = 0.10

#: 肩宽标尺下限，低于则判定 NO_SWING
MIN_SHOULDER_SCALE: Final[float] = 1e-6

#: Plan B 开关（架构文档 §7.8）。True 时 ②③⑤⑦ 一律走兜底比例并标记 estimated
ANCHOR_ONLY_MODE: Final[bool] = False

#: ⑦送杆：h 局部最小值后再上升该阈值才触发（用户可调，越大送杆越靠后）。
#: 方案 B（2026-08 用户拍板）：上一版「h 局部最小点」（送杆刚启动）在用户样本
#: 22030124 上定位到 116 帧，用户实测「杆刚过最低点」偏早，视觉上 118~119 帧
#: （腕部超过髋部一些、杆身略上扬）更好。判据 = 全窗 ``h`` 最小点 + 本阈值：
#: ``h >= h_min + FOLLOWTHROUGH_RISE`` 的第一帧。
#: 校准值 0.95（肩宽制）：方案草案 0.10 实测无效——22030124 的 ``h`` 在最低点后
#: 每帧上升 ≈0.20~0.37，0.10 连最低点后第 1 帧都跨不过（⑦ 仍 116）；用户目标
#: 视觉帧 119 对应 ``h≈1.22``（最低点 ``-0.01`` 之上约 1.23），故阈值取
#: 0.95（落在 118 帧 ``h=0.92`` 与 119 帧 ``h=1.22`` 之间），⑦ 精确命中 119。
#: 合成回归样本（tests/conftest）在 0.95 下 ⑦ = impact+8（开放角 ≈28° < 30，
#: RISK-016 端到端仍触发）；阈值再调大（如 1.0）会让合成样本开放角 ≥30 而
#: 破坏该测试，故 0.95 是「用户样本 119 + 合成回归绿」的共同可行点。
#:
#: ⚠️ **face-on 专用**：DTL（侧面）机位下 ⑦ 改用 :data:`FOLLOWTHROUGH_RISE_DTL`。
#: Step 2 DTL 身高制重标证据（4 段 DTL 样本送杆窗扫描，见
#: ``_probe_out/step2_dtl_sweep.py``）：DTL h 范围 0.6~0.7、送杆窗内 h 最小到
#: 最大仅 0.5~0.6（远小于肩宽制的 0.95 跨度），FOLLOWTHROUGH_RISE=0.95 在 DTL
#: 下**永远走兜底**（h_min+0.95 超出窗内最大值）。Face-on 保持 0.95 逐字节不变。
FOLLOWTHROUGH_RISE: Final[float] = 0.95

#: ⑦送杆（**DTL 侧面机位**，2026-08 Step 2 新增）：与 :data:`FOLLOWTHROUGH_RISE`
#: 同语义（``h`` 局部最小点 + 本阈值 = 杆身略上扬 = ⑦ 送杆）。Face-on 恒用
#: :data:`FOLLOWTHROUGH_RISE`（0.95，正面回归逐字节一致，用户已验收）。
#:
#: 校准值 **0.10**（5 段 DTL 样本扫描，见 ``_probe_out/step2_dtl_sweep.py``）：
#: 4 个有挥杆样本送杆窗内「min→max 上升跨度」均 ≥ 0.46 heights（11a6594b
#: 0.068→0.626、f470c599 0.013→0.586、c6f67f38 0.117→0.585、470057ac
#: 0.299→0.524），thr=0.10 让 4/4 ⑦ 变 real 且「距击球 2-5 帧」区间命中 3/4：
#:
#:   - 11a6594b：⑦ 212(est) → 212(real)，距⑥ 4 帧（最佳）；
#:   - f470c599：⑦ 242(est) → 244(real)，距⑥ 7 帧（送杆窗较长，略超目标）；
#:   - c6f67f38：⑦ 182(est) → 181(real)，距⑥ 2 帧（最佳）；
#:   - 470057ac：⑦ 106(est) → 106(real)，距⑥ 3 帧（最佳）。
#:
#: ⚠️ 稳定平台 [0.05, 0.20] ⑦ 帧位变动 ≤1 帧（11a6594b 212、c6f67f38 181~182、
#: 470057ac 104~110、f470c599 244~245）；取中点 0.10。
FOLLOWTHROUGH_RISE_DTL: Final[float] = 0.10


# ---------------------------------------------------------------------------
# 5. 符号约定常量（架构文档 §10.3）—— 符号校准只改这两个
# ---------------------------------------------------------------------------

#: 转动角符号：背对目标方向为正（上杆为正）。
#:
#: 标定结论（见 ``.tools/_probe/smoke_synth.py`` 合成挥杆实测）：
#: MediaPipe world 坐标 x 右+ / y 下+ / z 远离相机+，正面机位右手球手在顶点时
#: 引导肩转向相机侧（z 减小），:func:`geometry.rotation_xz` 的原始输出为 **负**，
#: 因此取 -1 才满足「上杆为正、顶点肩转 +70~88」的口径。
ROTATION_SIGN: Final[int] = -1

#: 目标方向：face-on 机位下右手球手的目标位于图像 x 增大方向。若骨盆位移方向反了，改成 -1
TARGET_DIR_X: Final[int] = +1


# ---------------------------------------------------------------------------
# 6. 渲染
# ---------------------------------------------------------------------------

#: 输出图长边上限
RENDER_LONG_SIDE: Final[int] = 720

#: JPEG 质量
JPEG_QUALITY: Final[int] = 85

#: 骨架线颜色（BGR）与线宽
SKELETON_COLOR: Final[Tuple[int, int, int]] = (0, 255, 180)
SKELETON_THICKNESS: Final[int] = 3

#: 关键点圆点颜色（BGR）与半径
JOINT_COLOR: Final[Tuple[int, int, int]] = (0, 90, 255)
JOINT_OUTLINE_COLOR: Final[Tuple[int, int, int]] = (255, 255, 255)
JOINT_RADIUS: Final[int] = 4

#: 左上角标签颜色（BGR）
LABEL_COLOR: Final[Tuple[int, int, int]] = (255, 255, 255)
LABEL_SHADOW_COLOR: Final[Tuple[int, int, int]] = (0, 0, 0)


# ---------------------------------------------------------------------------
# 7. 文案
# ---------------------------------------------------------------------------

#: ErrorCode.value -> 用户可见中文文案（架构文档 §4.7）
ERROR_MESSAGES: Final[Dict[str, str]] = {
    "NO_PERSON": "没有检测到人物，请确保全身在画面内后重拍",
    "NO_SWING": "没有识别到完整的挥杆动作，请拍摄从站位到收杆的完整过程",
    "TOO_DARK": "画面过暗，建议在光线充足的环境下拍摄",
    "LOW_QUALITY": "人物识别不稳定，请固定手机、避免遮挡后重拍",
    "BAD_VIDEO": "视频无法解析，请换一段 mp4 视频重试",
    "BAD_ORIENTATION": "检测到视频方向异常，请将手机竖向拍摄后重试",
    "TIMEOUT": "当前系统比较繁忙，请稍后再试",
    "INTERNAL": "分析失败了，请稍后重试",
}

#: 结果页固定免责声明（PDD §3.4.4 全文，v2 替换）
DISCLAIMER: Final[str] = (
    "以上姿态数据基于单目视频估算，存在测量误差。损伤风险评估基于"
    "《高尔夫运动保障手册》中的一般性知识，仅供参考，不构成医学诊断或"
    "专业教学建议。如有身体不适，请咨询专业医疗机构。"
)

#: DTL 机位追加的投影角说明（我方补充，非 PDD 原文，已报备）
DISCLAIMER_DTL_SUFFIX: Final[str] = "挥杆平面角为投影角估算，非真实空间角。"

#: 低帧率提示
WARN_LOW_FPS: Final[str] = "帧率偏低，击球阶段定位可能不准"

#: 用户所选机位与自动判定不一致时的提示（v2，不阻断）
WARN_VIEW_MISMATCH: Final[str] = (
    "所选拍摄机位与画面特征不一致，指标口径可能受影响，建议按拍摄指引重新拍摄"
)


def error_message(code_value: str) -> str:
    """按错误码取中文文案，未知码回落到 INTERNAL 文案。"""
    return ERROR_MESSAGES.get(code_value, ERROR_MESSAGES["INTERNAL"])


# ---------------------------------------------------------------------------
# 8. 球杆检测（球杆检测技术方案 §5.2 / T01 常量清单）
#
# ⚠️ 状态（2026-08 下线）：用户反馈球杆识别率偏低（实测真实视频置信度仅
# 0.206~0.462，全为 L1 proxy、无 L0 真实几何结果），已决定**先下线球杆检测**，
# 后期再做。主管线（pipeline/metrics/renderer/reference）已摘除调用链，
# 球杆增强指标不再产出；``swing_plane``（PDD 版，纯 MediaPipe）保留。
#
# 本段常量保留给已归档的 :mod:`app.club_detector` 模块（文件不删、后期直接复用）：
# ``CLUB_ENABLED`` / ``CLUB_MODE`` / ``CLUB_MAX_DECODE_FRAMES`` 等仍被该模块
# 与 ``tests/test_club_detector.py`` 引用，**不能删除**。已移除仅被已下线主管线
# 代码消费的常量：三级降级阈值（``CLUB_CONF_MIN`` / ``CLUB_CONF_PROXY_MIN``）、
# 渲染色（``CLUB_COLOR`` / ``CLUB_THICKNESS``）、用户提示（``WARN_CLUB_*``）、
# ONNX 占位、以及解码字节预算（``DECODE_BYTES_BUDGET``，管线不再解码窗口采样帧）。
# ---------------------------------------------------------------------------

#: 球杆检测总开关。仅 :func:`app.club_detector.detect` 读取（模块已归档，
#: 主管线不再引用）；False 时直接返回 ``ClubTrack(available=False)``。
CLUB_ENABLED: Final[bool] = True

#: 检测模式：``"geom"``（本期唯一实现）| ``"onnx"``（预留）| ``"off"``
CLUB_MODE: Final[str] = "geom"

#: DTL（侧面）机位杆长先验 = 系数 × 图像身高（鼻–踝中点像素距）。
#: 依据：身高 1.75m 时 7 号铁 ≈ 0.94m（0.54×身高），一号木 ≈ 1.14m（0.65×身高）。
#: ⚠️ 侧面机位双肩与光轴近似共线、投影肩宽被严重压缩，**不可**用 S_px 当标尺。
CLUB_LEN_RATIO_DTL: Final[Tuple[float, float]] = (0.52, 0.66)

#: face-on（正面）机位杆长先验 = 系数 × 图像肩宽（肩宽 ≈ 0.25×身高）
CLUB_LEN_RATIO_FACEON: Final[Tuple[float, float]] = (2.0, 2.8)

#: ROI 扇形半张角（度）：``(Address 帧, 后续帧)``。
#: 后续帧靠上一帧杆身方向 + 手腕速度做一阶预测，把搜索区收窄到 ±25°——
#: **时序预测是鲁棒性的最大来源**，禁止退化成逐帧独立检测。
CLUB_ROI_FAN_DEG: Final[Tuple[float, float]] = (45.0, 25.0)

#: HoughLinesP ``minLineLength`` = 该系数 × club_len_px
CLUB_HOUGH_MIN_LEN_RATIO: Final[float] = 0.35

#: HoughLinesP ``maxLineGap`` = 该系数 × club_len_px
CLUB_HOUGH_MAX_GAP_RATIO: Final[float] = 0.10

#: 过滤①：候选线段延长线到握把的垂距上限 = 该系数 × club_len_px（杆身必过握把）
CLUB_GRIP_DIST_RATIO: Final[float] = 0.08

#: 过滤②：候选线段方向与时序预测方向的夹角上限（度）
CLUB_DIR_TOL_DEG: Final[float] = 25.0

#: Hough / 帧差分支切换的速度倍率（相对 :data:`V_STILL`）
CLUB_SPEED_SWITCH_RATIO: Final[float] = 3.0

#: 分支切换速度阈值（肩宽/秒）= :data:`CLUB_SPEED_SWITCH_RATIO` × :data:`V_STILL`。
#: ``speed < 阈值`` 走 Hough（低速段杆身锐利），否则走帧差（高速段运动模糊）。
CLUB_SPEED_SWITCH: Final[float] = CLUB_SPEED_SWITCH_RATIO * V_STILL

#: 机位自动判定：Address 帧「图像肩宽 / 图像身高」低于该值判为 DTL。
#: face-on 约 0.22~0.28；DTL 因双肩前后重叠会掉到 < 0.13。
#: ⚠️ 非球杆常量：view_detector 机位判定使用，必须保留。
VIEW_SHOULDER_RATIO_DTL: Final[float] = 0.13

#: 单次检测最多解码的帧数（原 club_detector._MAX_DECODE_FRAMES=48 下调）。
#: 8 事件帧 + 各自前一帧 + Top→Impact 窗口采样；锚点预算 = 该值 // 2，
#: 因此解码帧数（targets）恒 ≤ 该值。单 worker 内存护栏（架构 §5.2）。
CLUB_MAX_DECODE_FRAMES: Final[int] = 28

#: DTL 等效肩宽标尺 = 图像身高 × 该系数（肩宽 ≈ 0.25×身高 的人体测量先验）。
#: ⚠️ 经验常量，需用真实视频回归校准（架构 §10 #6）。
#: 校准值 0.26（2026-08 实测，见 docs/VALIDATION-B）：3 段正面视频 Address 帧
#: 「图像肩宽/图像身高」实测 0.2486 / 0.2674 / 0.2706（均值 0.262，中位数 0.267），
#: 与人体测量先验 0.25 一致；取 0.26 落在实测区间内且与先验接近。
#: ⚠️ 非球杆常量：metrics DTL 位移标尺使用，必须保留。
SHOULDER_TO_HEIGHT_RATIO: Final[float] = 0.26


# ---------------------------------------------------------------------------
# 8b. 轻量击球帧校正（ARCHITECTURE-v3-clublite.md，2026-08 新追加）
# ⚠️ 与 8a 归档的 CLUB_*（重球杆检测）无关；本块前缀 CLUBLITE_。
# 目标：只做帧级时序校正（±1~2 帧），不做像素级杆头定位。
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

#: 校正最少移动帧数（< 该值不采纳，避免帧级抖动）。
#: 注意：CLUBLITE_IMPACT_OFFSET 生效后，若偏移恰好把运动峰拉回原 impact
#: （delta==0，如正面1），视为「确认原估计正确」的合法结果，照常采纳为
#: 无操作校正（reanchor 返回原 events），不算帧级抖动。
CLUBLITE_MIN_SHIFT_FRAMES: Final[int] = 1

#: 校正最多移动帧数（超过视为检测不可信，不采纳）
CLUBLITE_MAX_SHIFT_FRAMES: Final[int] = 12

#: 系统偏移：运动峰帧 -> 视觉接触瞬间（采样帧，array 下标空间）。
#: v2 设 -1（运动峰是球被杆头加速后的帧，接触在其前 1 帧）；D 方案
#: （2026-08 用户拍板）把 M2 杆身最低点作为先验锚点后，**实验结论**（12 段
#: 真实视频，见 docs/VALIDATION-CLUBLITE.md §3，勿拍脑袋调）：
#:   offset=0 -> 新样本 22030124 落到 116（真实接触 115，仍偏晚 1 帧）；
#:   offset=-1 -> 落到 115（命中真实接触），且其余 9 段与 v2 完全一致。
#: 因此**保持 -1**：锚点负责把峰位从横扫帧 121 拉回杆头最低点 116，-1 偏移
#: 负责把"杆头最低点"微调到"视觉接触"（116 -> 115），方向不变。
#: 受物理下界守卫约束：不得早于 top + min_gap（否则 G0，避免 NO_SWING）。
CLUBLITE_IMPACT_OFFSET: Final[int] = -1

#: D 方案总开关（2026-08 用户拍板）：True = 先算 M2 杆身最低点先验锚点，
#: 只在锚点 ±:data:`CLUBLITE_ANCHOR_WINDOW` 邻域内按综合 score 选帧（横扫式
#: 运动峰偏晚问题）；False = 回退 v2 全窗口运动峰逻辑（原行为不变）。
CLUBLITE_USE_ANCHOR: Final[bool] = True

#: 锚点邻域半窗（采样帧）。锚点 = ``_shaft_lowest_y`` 返回 y 值最大的候选帧
#: （杆头最贴地，即真实击球信号）。横扫式运动峰（杆身水平横扫跨越像素最多、
#: 帧差最强）常晚于真实击球数帧，只在锚点 ±W 邻域内选帧可把横扫帧排除在
#: 候选集外（实测新样本 22030124：横扫峰 121 vs 杆身最低点 116，W=3 时
#: 121 出局、116 命中）。
CLUBLITE_ANCHOR_WINDOW: Final[int] = 3

#: 锚点邻域可信度下限（0~1）：邻域内最优得分须 ≥ 该比例 × 全窗口最优得分，
#: 才把候选集收缩到锚点邻域（横扫假设成立）；否则回退全窗口（v2 行为）。
#: 校准依据（12 段真实视频，见 docs/VALIDATION-CLUBLITE.md §3）：横扫式运动峰
#: 偏晚只在该假设成立时可信——锚点邻域内要有与全窗口最优相当的候选。实测
#: 新样本 22030124 ratio=0.94（应启用锚点 -> 116）；三个假锚点样本 ratio 仅
#: 0.11/0.36/0.55（0bb16a97 / a4fba3d2 / 1446d1b9，锚点系 Hough 假阳性/弱
#: 运动帧，应回退）。0.7 干净分隔两类。
CLUBLITE_ANCHOR_MIN_SCORE_RATIO: Final[float] = 0.7

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


# ---------------------------------------------------------------------------
# 9. v2 风险引擎与接口契约（架构 ARCHITECTURE-v2.md §4 / §6）
# ---------------------------------------------------------------------------

#: 风险引擎总开关。False 时 :func:`app.risk_engine.evaluate_all` 恒返回空，
#: 一键关停整个风险区（线上止血阀）。
RISK_ENGINE_ENABLED: Final[bool] = True

#: 灰度强开白名单：内部自测/灰度时强开某几条缺文案规则（如 ``{"RISK-003"}``），
#: **不改代码**。注意：强开会绕过 :data:`RiskRule.enabled`，但文案自检
#: （``risk_engine.self_check``）仍以 ``enabled=True`` 为准——强开规则若缺文案
#: 会产出空描述卡片，仅限内部使用。
RISK_RULES_FORCE_ENABLE: Final[FrozenSet[str]] = frozenset()

#: 五态判定的 critical 区间宽度倍数（架构 §3.5）。
#: ``critical`` ⟺ ``value < ref_min - span×ratio`` 或 ``value > ref_max + span×ratio``，
#: ``span = ref_max - ref_min``。默认 1.0 = 超出一个完整区间宽度即重度偏离。
CRITICAL_SPAN_RATIO: Final[float] = 1.0

#: 错误码输出风格：``"pdd"``（对外 PDD 码）| ``"legacy"``（旧内部码）。
#: 线上出事的回滚开关（架构 §6.3）。
API_CODE_STYLE: Final[str] = "pdd"

#: PDD 错误码（对外契约，架构 §6.3）
PDD_CODE_FILE_TOO_LARGE: Final[int] = 10001
PDD_CODE_BAD_FORMAT: Final[int] = 10002
PDD_CODE_BAD_DURATION: Final[int] = 10003
PDD_CODE_INTERNAL: Final[int] = 10004
#: 视频方向异常（orientation ≠ 0，横拍）。PDD 未定义，我方在 1000x 校验域内顺延。
#: 产品决策：放弃 EXIF 自动旋转（跨平台 cv2 行为不一致），直接拒绝并提示竖拍。
PDD_CODE_BAD_ORIENTATION: Final[int] = 10005
PDD_CODE_TASK_NOT_FOUND: Final[int] = 20001
#: 「任务尚未完成」PDD 未定义，我方在结果域内顺延的暂定值（架构 §10 #1）
PDD_CODE_TASK_PENDING: Final[int] = 20002

#: step(int) -> step_text(str) 映射（PDD 的字符串 step；step4 文案 = 「计算姿态指标与风险」）
STEP_TEXTS: Final[Dict[int, str]] = {
    1: "上传并校验视频",
    2: "提取身体关键点",
    3: "识别8个挥杆阶段",
    4: "计算姿态指标与风险",
}


# ---------------------------------------------------------------------------
# 10. 手动帧微调（结果页缩略图 ◀▶，ARCHITECTURE-v4-frameadjust.md）
# ---------------------------------------------------------------------------

#: 手动帧微调总开关。False 时新接口直接返回 5000（不影响主链路）。
FRAME_ADJUST_ENABLED: Final[bool] = True

#: 切帧范围：事件帧 ± 该帧数（防越界/滥用；前端按钮同样按此限位）。
FRAME_ADJUST_RANGE: Final[int] = 30

#: 分析成功后是否保留原视频副本（动态帧渲染需要原始像素）。
#: 原 ``upload.mp4`` 仍按 PRD Q6 移除；副本以 ``source.{ext}`` 存在任务目录内，
#: 随任务 TTL（7 天）一起清理，不上传、不外链。关闭则恢复"分析后即删视频"旧行为
#: （此时手动帧微调接口将因缺源视频返回 5000）。
KEEP_SOURCE_VIDEO: Final[bool] = True

#: 保留的视频副本文件名（实际为 ``source`` + 原扩展名，如 ``source.mp4``）
SOURCE_FILENAME: Final[str] = "source.mp4"

#: 关键点序列缓存文件名（分析时落盘，供手动帧微调接口复用同源关键点）
LANDMARK_CACHE_FILENAME: Final[str] = "landmarks.npz"

#: PDD 错误码 20003（帧号越界 / 超出可调整范围）。2000x 为任务结果域，
#: 20001 任务不存在、20002 任务未完成、20003 帧号越界（PDD 未定义，我方顺延）。
PDD_CODE_FRAME_OUT_OF_RANGE: Final[int] = 20003

#: PDD 错误码 20004（阶段标识非法，非 8 阶段之一）。结果域顺延，纯增量新增。
PDD_CODE_PHASE_INVALID: Final[int] = 20004


# ---------------------------------------------------------------------------
# 11. SwingNet（AI DTL 事件检测，M1 封装，M2 才切换 pipeline）
# ---------------------------------------------------------------------------

#: SwingNet 8 事件检测器权重路径（DTL 侧面机位专用）。
#: 权重 60MB 不入 git（``backend/models/`` 已在根 .gitignore 忽略），
#: 可用 ``GOLF_SWINGNET_WEIGHTS`` 环境变量覆盖（如测试/部署指定别的副本）。
#: M1 仅封装；:mod:`app.ai.swingnet_detector` 懒加载该权重，M2 才接入 pipeline。
SWINGNET_WEIGHTS_PATH: Final[Path] = Path(
    os.getenv("GOLF_SWINGNET_WEIGHTS", str(BASE_DIR / "models" / "swingnet_1800.pth.tar"))
).resolve()
