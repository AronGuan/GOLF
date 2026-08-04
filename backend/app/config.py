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

#: 上传文件大小上限（20MB）
MAX_UPLOAD_BYTES: Final[int] = 20 * 1024 * 1024

#: 允许的扩展名
ALLOWED_VIDEO_EXTS: Final[FrozenSet[str]] = frozenset({".mp4"})

#: 允许的 content-type（部分客户端会传 application/octet-stream）
ALLOWED_CONTENT_TYPES: Final[FrozenSet[str]] = frozenset(
    {
        "video/mp4",
        "video/mpeg4",
        "application/mp4",
        "application/octet-stream",
        "",
    }
)

#: 上传文件名（落盘固定名）
UPLOAD_FILENAME: Final[str] = "upload.mp4"

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

#: 服务端放宽后的时长边界（前端已按 2~15s 拦截）
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
MISS_RATIO_LOW_QUALITY: Final[float] = 0.10

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
# ---------------------------------------------------------------------------

#: 滑动平均窗口时长（秒）
SMOOTH_WIN_SEC: Final[float] = 0.08

#: 静止判定速度阈值（肩宽/秒）
V_STILL: Final[float] = 0.25

#: 判定"存在挥杆"的最小速度峰值（肩宽/秒）
V_PEAK_MIN: Final[float] = 1.5

#: Address 静止段最短时长（秒）
STILL_MIN_SEC_ADDR: Final[float] = 0.10

#: Finish 静止段最短时长（秒）
STILL_MIN_SEC_FINISH: Final[float] = 0.15

#: Impact 高度回落容差（肩宽）
IMPACT_Y_TOL: Final[float] = 0.15

#: Impact 速度峰搜索半窗（秒）
IMPACT_WIN_SEC: Final[float] = 0.05

#: 手腕过髋线判据（肩宽）
H_HIP: Final[float] = 0.10

#: 最小垂直行程（肩宽）
MIN_WRIST_TRAVEL: Final[float] = 0.60

#: ②③⑤⑦ 兜底比例
FALLBACK_RATIO: Final[Tuple[float, float, float, float]] = (0.35, 0.70, 0.50, 0.35)

#: 顶点搜索时排除首尾比例
TOP_SEARCH_MARGIN: Final[float] = 0.05

#: 顶点速度反向点精修半窗（秒）
TOP_REFINE_SEC: Final[float] = 0.10

#: Address -> Top 的最短时长（秒），过短判 NO_SWING
MIN_TOP_ADDR_SEC: Final[float] = 0.15

#: Top -> Impact 的最短时长（秒），过短判 NO_SWING
MIN_IMPACT_TOP_SEC: Final[float] = 0.06

#: Finish 兜底 A 所需的击球后最短余量（秒）
FINISH_FALLBACK_SEC: Final[float] = 0.10

#: 肩宽标尺下限，低于则判定 NO_SWING
MIN_SHOULDER_SCALE: Final[float] = 1e-6

#: Plan B 开关（架构文档 §7.8）。True 时 ②③⑤⑦ 一律走兜底比例并标记 estimated
ANCHOR_ONLY_MODE: Final[bool] = False


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
    "TIMEOUT": "分析超时了，请稍后重试",
    "INTERNAL": "分析失败了，请稍后重试",
}

#: 结果页固定免责声明（PRD §6.5）
DISCLAIMER: Final[str] = (
    "以上数据基于单目视频姿态估算，仅供动作参考，存在测量误差，不构成专业教学建议。"
)

#: 低帧率提示
WARN_LOW_FPS: Final[str] = "帧率偏低，击球阶段定位可能不准"


def error_message(code_value: str) -> str:
    """按错误码取中文文案，未知码回落到 INTERNAL 文案。"""
    return ERROR_MESSAGES.get(code_value, ERROR_MESSAGES["INTERNAL"])
