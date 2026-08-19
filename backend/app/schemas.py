"""数据契约：枚举、内部 dataclass、对外 Pydantic 响应体。

本模块只依赖标准库 / numpy / pydantic，不依赖项目内其他模块。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class TaskStatus(str, Enum):
    """任务状态机。"""

    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"


class PhaseKey(str, Enum):
    """经典 8 阶段。"""

    ADDRESS = "address"
    TAKEAWAY = "takeaway"
    BACKSWING = "backswing"
    TOP = "top"
    DOWNSWING = "downswing"
    IMPACT = "impact"
    FOLLOW_THROUGH = "follow_through"
    FINISH = "finish"


class MetricStatus(str, Enum):
    """指标五态（PDD §6.4，v2 从三态扩到五态）。

    ``CRITICAL_LOW`` / ``CRITICAL_HIGH`` 表示重度偏离参考区间，由后端
    :func:`app.reference.judge5` 判定下发；前端直接按状态值映射配色。
    """

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL_LOW = "critical_low"
    CRITICAL_HIGH = "critical_high"


class RiskLevel(str, Enum):
    """损伤风险等级（PDD §5.1）。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class CameraView(str, Enum):
    """拍摄机位（球杆检测技术方案 §4.3）。

    ``swing_plane`` 系列是 **DTL（侧面）专属**指标：DTL 机位下相机光轴大致沿
    目标线，挥杆平面近似 edge-on，二维投影角 ≈ 真实平面倾角；face-on 机位下
    挥杆平面几乎正对相机，倾角信息完全丢失，强行计算没有物理意义。

    ``AUTO`` 只出现在**请求入参**上，需在进入指标计算前被解析成前两者之一。
    """

    FACE_ON = "face_on"
    DOWN_THE_LINE = "down_the_line"
    AUTO = "auto"


class MetricSource(str, Enum):
    """指标数值来源，对应三级降级（球杆检测技术方案 §4.5）。"""

    #: L0 —— 由真实球杆几何量算出
    MEASURED = "measured"
    #: L1 —— 代理估算（如引导腕–肩连线倾角）
    PROXY = "proxy"
    #: L2/兜底 —— ``_sanitize`` 填充的参考区间中值
    REFERENCE = "reference"


class ErrorCode(str, Enum):
    """业务错误码。"""

    NO_PERSON = "NO_PERSON"
    NO_SWING = "NO_SWING"
    TOO_DARK = "TOO_DARK"
    LOW_QUALITY = "LOW_QUALITY"
    BAD_VIDEO = "BAD_VIDEO"
    TIMEOUT = "TIMEOUT"
    INTERNAL = "INTERNAL"


class AnalysisError(Exception):
    """分析流水线内部业务异常，携带 :class:`ErrorCode`。

    ``pdd_code``（v2 新增）：对外 PDD 错误码；``None`` 时由响应层按默认规则映射
    （架构 §6.3——不在响应层解析 message，而是抛异常时显式携带）。
    """

    def __init__(
        self, code: ErrorCode, detail: str = "", pdd_code: Optional[int] = None
    ) -> None:
        self.code: ErrorCode = code
        self.detail: str = detail
        self.pdd_code: Optional[int] = pdd_code
        super().__init__(f"{code.value}: {detail}" if detail else code.value)


# ---------------------------------------------------------------------------
# 8 阶段静态元信息
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseMeta:
    """阶段静态元信息。"""

    index: int
    key: PhaseKey
    name_cn: str
    name_en: str


PHASE_ORDER: Tuple[PhaseKey, ...] = (
    PhaseKey.ADDRESS,
    PhaseKey.TAKEAWAY,
    PhaseKey.BACKSWING,
    PhaseKey.TOP,
    PhaseKey.DOWNSWING,
    PhaseKey.IMPACT,
    PhaseKey.FOLLOW_THROUGH,
    PhaseKey.FINISH,
)

PHASE_META: Dict[PhaseKey, PhaseMeta] = {
    PhaseKey.ADDRESS: PhaseMeta(1, PhaseKey.ADDRESS, "准备", "Address"),
    PhaseKey.TAKEAWAY: PhaseMeta(2, PhaseKey.TAKEAWAY, "起杆", "Takeaway"),
    PhaseKey.BACKSWING: PhaseMeta(3, PhaseKey.BACKSWING, "上杆", "Backswing"),
    PhaseKey.TOP: PhaseMeta(4, PhaseKey.TOP, "顶点", "Top"),
    PhaseKey.DOWNSWING: PhaseMeta(5, PhaseKey.DOWNSWING, "下杆", "Downswing"),
    PhaseKey.IMPACT: PhaseMeta(6, PhaseKey.IMPACT, "击球", "Impact"),
    PhaseKey.FOLLOW_THROUGH: PhaseMeta(
        7, PhaseKey.FOLLOW_THROUGH, "送杆", "Follow-through"
    ),
    PhaseKey.FINISH: PhaseMeta(8, PhaseKey.FINISH, "收杆", "Finish"),
}


def phase_image_name(key: PhaseKey) -> str:
    """结果图文件名，例如 ``04_top.jpg``。"""
    return f"{PHASE_META[key].index:02d}_{key.value}.jpg"


# ---------------------------------------------------------------------------
# 内部数据结构（不出网）
# ---------------------------------------------------------------------------


@dataclass
class FrameLandmarks:
    """单帧 33 关键点。

    Attributes:
        frame_index: 在【原视频】中的帧号（已还原降采样）。
        timestamp: 秒 = ``frame_index / fps``。
        detected: 该帧 MediaPipe 是否检出人体（False 表示由插值填补）。
        norm: ``(33, 3)`` 归一化图像坐标，x/y ∈ [0,1]，原点左上，y 向下；z 为相对深度。
        world: ``(33, 3)`` world 3D（米），原点=双髋中点；x 右+ / y 下+ / z 远离相机+。
        visibility: ``(33,)`` 可见度 0~1。
    """

    frame_index: int
    timestamp: float
    detected: bool
    norm: np.ndarray
    world: np.ndarray
    visibility: np.ndarray


@dataclass
class SwingSignals:
    """切分算法使用的一维信号包，所有数组长度 = ``n``。"""

    n: int
    fps: float
    dt: float
    S: float
    wrist_x: np.ndarray
    wrist_y: np.ndarray
    shoulder_mid_y: np.ndarray
    hip_mid_y: np.ndarray
    h: np.ndarray
    speed: np.ndarray

    @property
    def fps_eff(self) -> float:
        """降采样后信号序列的有效帧率（帧/秒）。"""
        return 1.0 / self.dt if self.dt > 0 else 1.0


@dataclass
class SwingEvent:
    """一个挥杆事件（阶段）定位结果。"""

    index: int
    key: PhaseKey
    frame_index: int
    timestamp: float
    estimated: bool
    #: 在采样后序列中的数组下标，供 metrics / renderer 反查（不出网）
    array_index: int = 0


@dataclass
class ImpactRefineResult:
    """击球帧校正结果（ARCHITECTURE-v3-clublite.md §4.1）。

    硬约束：任何失败 -> ``available=False``（调用方保持原 events，绝不破坏主链路）。
    校正只在**帧级时序**上移动 impact（±1~2 采样帧），不做像素级杆头定位。
    内部结构，不出网；校正事实通过 impact 事件本身的
    ``frame_index/timestamp/estimated`` 变化 + ``warnings`` 追加体现。

    Attributes:
        available: 校正是否可用（采纳判定通过）。
        method: ``"motion"`` | ``"motion+shaft"`` | ``"none"``。
        old_array_index: 校正前 impact 的 array 下标。
        new_array_index: 校正后 impact 的 array 下标。
        delta_frames: ``new - old``（array 下标差 = 采样帧差）。
        confidence: 0~1，取最优候选的运动峰归一化强度。
        ball_detected: Address 帧是否检出唯一高置信球点。
        motion_peak_index: M1 最优候选（array 下标）；未检出为 None。
        shaft_lowest_index: M2 杆头最低点候选（array 下标）；未检出为 None。
        ball_center_px: 球心像素坐标 ``(x, y)``（渲染 marker 用）；未检为 None。
    """

    available: bool = False
    method: str = "none"
    old_array_index: int = -1
    new_array_index: int = -1
    delta_frames: int = 0
    confidence: float = 0.0
    ball_detected: bool = False
    motion_peak_index: Optional[int] = None
    shaft_lowest_index: Optional[int] = None
    ball_center_px: Optional[Tuple[int, int]] = None


@dataclass
class ClubDetection:
    """单帧球杆检测结果（像素坐标系，与 ``metrics._img_pt()`` 同口径）。

    Attributes:
        frame_index: 在【原视频】中的帧号。
        grip: ``(2,)`` 握把像素坐标；``None`` 表示该帧未定位。
        head: ``(2,)`` 杆头像素坐标；``None`` 表示该帧未定位。
        confidence: 0~1，由投票得分 / 方向一致性 / 边缘强度合成。
        method: ``"hough"`` | ``"framediff"`` | ``"onnx"`` | ``"interp"`` | ``"none"``。
    """

    frame_index: int
    grip: Optional[np.ndarray] = None
    head: Optional[np.ndarray] = None
    confidence: float = 0.0
    method: str = "none"

    @property
    def valid(self) -> bool:
        """握把与杆头都已定位。"""
        return self.grip is not None and self.head is not None


@dataclass
class ClubTrack:
    """全片球杆轨迹。

    **硬约束**：:mod:`app.club_detector` 的任何异常都必须被内部吞掉并返回
    ``available=False`` 的空轨迹，**禁止**外抛 :class:`AnalysisError`——挥杆分析
    主链路（已跑通的 23 个指标）不能被一个增量特性拖垮。
    """

    detections: List[ClubDetection] = field(default_factory=list)
    #: 杆长先验（像素）
    club_len_px: float = 0.0
    #: 关键帧①④⑤⑥ confidence 的中位数，三级降级的判据
    overall_confidence: float = 0.0
    #: 是否产出了可用的检测结果
    available: bool = False
    #: 本次检测采用的机位
    view: CameraView = CameraView.FACE_ON
    #: 该机位下 ``swing_plane`` 系列是否物理可测（仅 DTL 为 True）
    swing_plane_measurable: bool = False

    def by_frame(self) -> Dict[int, ClubDetection]:
        """``{frame_index: ClubDetection}`` 快查表。"""
        return {d.frame_index: d for d in self.detections}

    def get(self, frame_index: int) -> Optional[ClubDetection]:
        """取指定原视频帧号的检测结果，缺失返回 ``None``。"""
        for detection in self.detections:
            if detection.frame_index == frame_index:
                return detection
        return None


# ---------------------------------------------------------------------------
# 对外响应结构
# ---------------------------------------------------------------------------


class VideoMeta(BaseModel):
    """视频元信息。

    Attributes:
        fps / duration / width / height / frame_count / total_frames / sample_step /
        low_fps / camera_view: 既有字段。
        orientation: EXIF 旋转角度（0/90/180/270）—— iPhone 横拍视频的元数据旋转标记。
            ``probe_video`` 从 ``cv2.CAP_PROP_ORIENTATION_META`` 读取（FFmpeg 后端不
            支持时回退 0）。当 ``orientation ∈ {90, 270}`` 时 ``width`` / ``height``
            已被交换为**转正后**的尺寸（即人在画面中站直后的 w×h），保证下游
            ``computeVideoAspect`` / 机位判定 / 渲染 / MediaPipe 关键点全部用转正后
            的宽高。抽帧后用 ``frame_reader.rotate_frame`` 把解码帧旋转到转正方向。
    """

    fps: float
    duration: float
    width: int
    height: int
    frame_count: int
    #: PDD 字段名，= frame_count（v2 新增；``frame_count`` 保留 deprecated）
    total_frames: int = 0
    sample_step: int = 1
    low_fps: bool = False
    #: 拍摄机位；默认 face-on，保持既有行为不变
    camera_view: CameraView = CameraView.FACE_ON
    #: EXIF 旋转角度（0/90/180/270）。0 = 不旋转；90/270 时 width/height 已互换为转正尺寸
    orientation: int = 0


class StageMetric(BaseModel):
    """单个指标。

    ``estimated`` / ``source`` / ``confidence`` 三个字段用于三级降级
    （球杆检测技术方案 §4.5），语义与 :attr:`PhaseResult.estimated` 对齐，
    前端复用同一个"估算"角标组件。全部带默认值，对现有 23 个指标零影响。

    ``description`` 为指标卡下方的术语解释行（PDD §4.2）；缺失为 ``""``
    时前端不渲染该行。
    """

    key: str
    name: str
    value: float
    unit: str
    ref_min: float
    ref_max: float
    status: MetricStatus
    #: 是否为估算值（代理/兜底），前端据此显示"估算"角标
    estimated: bool = False
    #: 数值来源等级
    source: MetricSource = MetricSource.MEASURED
    #: 该指标的置信度 0~1；常规指标恒 1.0
    confidence: float = 1.0
    #: 术语解释行（v2 新增）；``""`` -> 前端不渲染
    description: str = ""


class RiskItem(BaseModel):
    """单条损伤风险（PDD §5.1，v2 新增）。

    触发原因（``trigger_description``）由后端完成模板渲染（含条件分支），
    下发的是成品字符串；小程序不做任何模板解析。
    """

    rule_id: str
    risk_name: str
    risk_level: RiskLevel
    trigger_phase: PhaseKey
    #: 指标上下文，前端渲染"触发原因"与跳转高亮用
    metric_key: str
    metric_name: str
    value: float
    unit: str
    ref_min: float
    ref_max: float
    #: 已渲染完毕的成品文案（含分支）
    trigger_description: str
    suggestions: List[str] = Field(default_factory=list)
    #: 手册原文摘录；None -> 前端隐藏「查看手册原文」入口
    manual_excerpt: Optional[str] = None
    #: 手册页码，字符串（实际出现 "P6/P11" 与 "-" 缺失两种情况）
    manual_page: Optional[str] = None


class PhaseResult(BaseModel):
    """单个阶段的完整结果。"""

    index: int
    key: PhaseKey
    name_cn: str
    name_en: str
    frame_index: int
    timestamp: float
    estimated: bool
    image_url: str
    metrics: List[StageMetric] = Field(default_factory=list)
    #: 本阶段风险列表（v2 新增）；空数组 = 无风险
    risks: List[RiskItem] = Field(default_factory=list)


class GlobalMetrics(BaseModel):
    """全程指标。"""

    tempo_ratio: float
    swing_duration: float
    max_head_drift_pct: float
    metrics: List[StageMetric] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    """完整分析结果（``GET /api/v1/tasks/{id}/result`` 的 data）。"""

    task_id: str
    status: TaskStatus = TaskStatus.SUCCESS
    #: 本次分析实际采用的机位（v2 新增，PDD 顶层位置）
    camera_view: CameraView = CameraView.FACE_ON
    video_meta: VideoMeta
    global_metrics: GlobalMetrics
    phases: List[PhaseResult] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    disclaimer: str = ""


class TaskStatusView(BaseModel):
    """``GET /api/v1/tasks/{id}`` 的 data。"""

    task_id: str
    status: TaskStatus
    progress: int
    step: int
    message: str
    error_code: Optional[ErrorCode] = None
    error_message: Optional[str] = None
    #: PDD 的字符串 step（v2 新增；``step`` 保持 int 供小程序进度条）
    step_text: str = ""


# ---------------------------------------------------------------------------
# 任务状态
# ---------------------------------------------------------------------------


@dataclass
class TaskState:
    """进程内任务记录。"""

    task_id: str
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0
    step: int = 1
    message: str = "排队中"
    error_code: Optional[ErrorCode] = None
    error_message: Optional[str] = None
    result: Optional[AnalysisResult] = None
    video_path: Optional[str] = None
    out_dir: Optional[str] = None
    #: 用户选择的拍摄机位（v2 新增）
    camera_view: CameraView = CameraView.FACE_ON
    #: PDD 的字符串 step（v2 新增）
    step_text: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_view(self) -> TaskStatusView:
        """转成对外状态视图。"""
        return TaskStatusView(
            task_id=self.task_id,
            status=self.status,
            progress=self.progress,
            step=self.step,
            message=self.message,
            error_code=self.error_code,
            error_message=self.error_message,
            step_text=self.step_text,
        )
