"""第二趟解码 + 骨架叠加 + JPG 导出（架构文档 §6.4）。

只在 8 个事件帧上渲染，避免全帧缓存爆内存。

⚠️ OpenCV 无法绘制中文，图上只写 ``#4 f37 0.62s``；中文阶段名由小程序端在大图
下方以文本展示（PRD §5.4 本就有该文本行）。**禁止**为此引入 PIL 字体依赖。
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from . import config, frame_reader, geometry
from .schemas import (
    AnalysisError,
    CameraView,
    ClubDetection,
    ClubTrack,
    ErrorCode,
    FrameLandmarks,
    PHASE_META,
    PhaseKey,
    SwingEvent,
    phase_image_name,
)

logger = logging.getLogger(__name__)


def _resize_long_side(image: np.ndarray, long_side: int) -> Tuple[np.ndarray, float]:
    """等比缩放使长边不超过 ``long_side``，返回 (图像, 缩放比)。"""
    h, w = image.shape[:2]
    cur = max(h, w)
    if cur <= long_side:
        return image, 1.0
    ratio = long_side / float(cur)
    resized = cv2.resize(
        image,
        (max(1, int(round(w * ratio))), max(1, int(round(h * ratio)))),
        interpolation=cv2.INTER_AREA,
    )
    return resized, ratio


def _draw_skeleton(img: np.ndarray, norm: np.ndarray, width: int, height: int) -> None:
    """在图像上绘制骨架连线与核心关键点（原地修改）。"""
    points: Dict[int, Tuple[int, int]] = {}
    for idx in set([i for edge in geometry.SKELETON_EDGES for i in edge]) | set(
        geometry.CORE_IDS
    ):
        x = float(norm[idx, 0]) * width
        y = float(norm[idx, 1]) * height
        if not (np.isfinite(x) and np.isfinite(y)):
            continue
        points[idx] = (int(round(x)), int(round(y)))

    for a, b in geometry.SKELETON_EDGES:
        if a in points and b in points:
            cv2.line(
                img,
                points[a],
                points[b],
                config.SKELETON_COLOR,
                config.SKELETON_THICKNESS,
                lineType=cv2.LINE_AA,
            )

    for idx in geometry.CORE_IDS:
        if idx not in points:
            continue
        cv2.circle(
            img,
            points[idx],
            config.JOINT_RADIUS + 1,
            config.JOINT_OUTLINE_COLOR,
            -1,
            lineType=cv2.LINE_AA,
        )
        cv2.circle(
            img,
            points[idx],
            config.JOINT_RADIUS,
            config.JOINT_COLOR,
            -1,
            lineType=cv2.LINE_AA,
        )


def _draw_label(img: np.ndarray, text: str) -> None:
    """左上角写英文/数字标签（带阴影提升可读性）。"""
    origin = (14, 34)
    cv2.putText(
        img, text, (origin[0] + 1, origin[1] + 1), cv2.FONT_HERSHEY_SIMPLEX,
        0.7, config.LABEL_SHADOW_COLOR, 3, cv2.LINE_AA,
    )
    cv2.putText(
        img, text, origin, cv2.FONT_HERSHEY_SIMPLEX,
        0.7, config.LABEL_COLOR, 1, cv2.LINE_AA,
    )


def _draw_dashed_line(
    img: np.ndarray, p1: Tuple[int, int], p2: Tuple[int, int],
    color: Tuple[int, int, int], thickness: int, dash: int = 12, gap: int = 8,
) -> None:
    """画虚线（OpenCV 无原生虚线，按段绘制）。"""
    x1, y1 = float(p1[0]), float(p1[1])
    x2, y2 = float(p2[0]), float(p2[1])
    length = math.hypot(x2 - x1, y2 - y1)
    if length < 1e-6:
        return
    step = float(dash + gap)
    n = int(length / step) + 1
    for k in range(n):
        t0 = k * step / length
        t1 = min(1.0, (k * step + dash) / length)
        if t1 <= t0:
            continue
        cv2.line(
            img,
            (int(round(x1 + (x2 - x1) * t0)), int(round(y1 + (y2 - y1) * t0))),
            (int(round(x1 + (x2 - x1) * t1)), int(round(y1 + (y2 - y1) * t1))),
            color, thickness, lineType=cv2.LINE_AA,
        )


def _draw_club(
    img: np.ndarray, detection: Optional[ClubDetection], scale: float
) -> None:
    """画杆身线 + 杆头实心圆（球杆检测结果，像素坐标按 ``scale`` 缩放）。

    置信度低于 ``config.CLUB_CONF_MIN`` 时画虚线且标签追加 ``~club``。
    """
    if detection is None or not detection.valid:
        return
    grip = (int(round(detection.grip[0] * scale)), int(round(detection.grip[1] * scale)))
    head = (int(round(detection.head[0] * scale)), int(round(detection.head[1] * scale)))

    high_conf = detection.confidence >= config.CLUB_CONF_MIN
    if high_conf:
        cv2.line(img, grip, head, config.CLUB_COLOR, config.CLUB_THICKNESS, cv2.LINE_AA)
    else:
        _draw_dashed_line(img, grip, head, config.CLUB_COLOR, config.CLUB_THICKNESS)
    # 杆头实心圆
    cv2.circle(img, head, max(3, config.CLUB_THICKNESS), config.CLUB_COLOR, -1, cv2.LINE_AA)


def _draw_horizon(img: np.ndarray) -> None:
    """DTL 机位画一条淡色水平参考线（供用户自查手机是否倾斜）。"""
    h, w = img.shape[:2]
    y = int(round(h * 0.5))
    cv2.line(
        img, (0, y), (w, y), (220, 220, 220), 1, cv2.LINE_AA
    )


def _render_one(
    bgr: np.ndarray,
    event: SwingEvent,
    frame_lm: Optional[FrameLandmarks],
    out_dir: Path,
    club_detection: Optional[ClubDetection] = None,
    view: CameraView = CameraView.FACE_ON,
) -> str:
    """渲染并写盘单张结果图，返回文件名。"""
    img, scale = _resize_long_side(bgr, config.RENDER_LONG_SIDE)
    height, width = img.shape[:2]
    if frame_lm is not None:
        _draw_skeleton(img, frame_lm.norm, width, height)
    if view is CameraView.DOWN_THE_LINE:
        _draw_horizon(img)
    _draw_club(img, club_detection, scale)
    _draw_label(img, f"#{event.index} f{event.frame_index} {event.timestamp:.2f}s")

    filename = phase_image_name(event.key)
    path = out_dir / filename
    ok = cv2.imwrite(
        str(path), img, [int(cv2.IMWRITE_JPEG_QUALITY), config.JPEG_QUALITY]
    )
    if not ok:
        raise AnalysisError(ErrorCode.INTERNAL, f"cannot write image: {path}")
    return filename


def render_events(
    video_path: str,
    events: Sequence[SwingEvent],
    out_dir: str,
    frames: Sequence[FrameLandmarks],
    frames_bgr: Optional[Dict[int, np.ndarray]] = None,
    club: Optional[ClubTrack] = None,
    view: CameraView = CameraView.FACE_ON,
) -> Dict[PhaseKey, str]:
    """在 8 个事件帧上叠加骨架并导出 JPG。

    ``frames_bgr`` 为 ``None`` 时保持既有自解码行为（第二趟解码，向后兼容 +
    测试友好）；管线应传入与球杆检测**共享**的解码帧字典，把解码趟数锁在 2 趟。
    ``club`` 非空时叠加杆身（低置信画虚线 + ``~club`` 角标），DTL 机位画水平参考线。

    Args:
        video_path: 原视频路径（``frames_bgr`` 已给出时不使用）。
        events: 8 个事件。
        out_dir: 输出目录。
        frames: 姿态提取产出，用于取该帧关键点。
        frames_bgr: 已解码帧字典（原视频帧号 -> BGR）；缺省时自行解码。
        club: 球杆检测轨迹（可为 ``None`` / ``available=False``）。
        view: 机位（控制水平参考线）。

    Returns:
        ``{PhaseKey: 文件名}``，恒 8 项。

    Raises:
        AnalysisError: ``BAD_VIDEO`` / ``INTERNAL``。
    """
    target_dir = Path(out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    lm_by_frame: Dict[int, FrameLandmarks] = {f.frame_index: f for f in frames}
    targets: Dict[int, List[SwingEvent]] = {}
    for event in events:
        targets.setdefault(event.frame_index, []).append(event)

    if frames_bgr is not None:
        decoded: Dict[int, np.ndarray] = dict(frames_bgr)
    else:
        # 第二趟解码：改用共享帧解码工具（不再自开 VideoCapture）
        decoded = frame_reader.grab_frames(video_path, list(targets.keys()))

    club_by_frame: Dict[int, ClubDetection] = club.by_frame() if club is not None else {}

    produced: Dict[PhaseKey, str] = {}
    pending = dict(targets)
    last_bgr: Optional[np.ndarray] = None
    decoded_frames = sorted(decoded.keys())

    # 命中且成功解码的帧：直接渲染；并维护兜底帧（≤ 该事件帧号最近一张成功解码者）
    for frame_index in sorted(pending.keys()):
        bgr = decoded.get(frame_index)
        if bgr is None:
            continue
        recent = [f for f in decoded_frames if f <= frame_index]
        if recent:
            last_bgr = decoded[recent[-1]]
        for event in pending[frame_index]:
            produced[event.key] = _render_one(
                bgr, event, lm_by_frame.get(frame_index), target_dir,
                club_detection=club_by_frame.get(frame_index), view=view,
            )
        del pending[frame_index]

    # 视频提前结束导致的漏帧：用最后一张成功解码的画面兜底，保证恒 8 张
    if pending:
        logger.warning("render fallback for frames: %s", sorted(pending.keys()))
        if last_bgr is None:
            raise AnalysisError(ErrorCode.INTERNAL, "no frame decoded for rendering")
        for frame_index, event_list in pending.items():
            for event in event_list:
                produced[event.key] = _render_one(
                    last_bgr, event, lm_by_frame.get(frame_index), target_dir,
                    club_detection=club_by_frame.get(frame_index), view=view,
                )

    missing = [PHASE_META[e.key].name_en for e in events if e.key not in produced]
    if missing:
        raise AnalysisError(ErrorCode.INTERNAL, f"render missing: {missing}")
    return produced
