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
    """指标三态。"""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


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
    """分析流水线内部业务异常，携带 :class:`ErrorCode`。"""

    def __init__(self, code: ErrorCode, detail: str = "") -> None:
        self.code: ErrorCode = code
        self.detail: str = detail
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


# ---------------------------------------------------------------------------
# 对外响应结构
# ---------------------------------------------------------------------------


class VideoMeta(BaseModel):
    """视频元信息。"""

    fps: float
    duration: float
    width: int
    height: int
    frame_count: int
    sample_step: int = 1
    low_fps: bool = False


class StageMetric(BaseModel):
    """单个指标。"""

    key: str
    name: str
    value: float
    unit: str
    ref_min: float
    ref_max: float
    status: MetricStatus


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
        )
