"""第二趟解码 + 骨架叠加 + JPG 导出（架构文档 §6.4）。

只在 8 个事件帧上渲染，避免全帧缓存爆内存。

⚠️ OpenCV 无法绘制中文，图上只写 ``#4 f37 0.62s``；中文阶段名由小程序端在大图
下方以文本展示（PRD §5.4 本就有该文本行）。**禁止**为此引入 PIL 字体依赖。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from . import config, frame_reader, geometry
from .schemas import (
    AnalysisError,
    CameraView,
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


def _draw_horizon(img: np.ndarray) -> None:
    """DTL 机位画一条淡色水平参考线（供用户自查手机是否倾斜）。"""
    h, w = img.shape[:2]
    y = int(round(h * 0.5))
    cv2.line(
        img, (0, y), (w, y), (220, 220, 220), 1, cv2.LINE_AA
    )


def _draw_impact_marker(img: np.ndarray, center: Tuple[int, int]) -> None:
    """在 impact 帧画球点/杆头圈（CLUBLITE 校正的可视化标注，默认关闭）。

    center 为**原图像素坐标**；调用方需先按 ``_resize_long_side`` 的缩放比换算。
    只画几何标注，不写中文（OpenCV 无法绘制中文，沿用本模块约定）。
    """
    cx, cy = int(round(center[0])), int(round(center[1]))
    h, w = img.shape[:2]
    if not (0 <= cx < w and 0 <= cy < h):
        return
    color = (0, 215, 255)  # BGR 亮黄
    cv2.circle(img, (cx, cy), 16, color, 2, cv2.LINE_AA)
    cv2.drawMarker(
        img, (cx, cy), color, markerType=cv2.MARKER_CROSS,
        markerSize=14, thickness=2, line_type=cv2.LINE_AA,
    )


def _compose(
    bgr: np.ndarray,
    event: SwingEvent,
    frame_lm: Optional[FrameLandmarks],
    view: CameraView = CameraView.FACE_ON,
    marker: Optional[Tuple[int, int]] = None,
) -> np.ndarray:
    """把单帧画面合成成带骨架叠加的结果图（不写盘，返回 BGR 图）。

    事件帧图（``_render_one``）与手动帧微调接口（``render_frame_png``）共用，
    保证两处绘制顺序/样式逐像素一致。
    """
    img, scale = _resize_long_side(bgr, config.RENDER_LONG_SIDE)
    height, width = img.shape[:2]
    if frame_lm is not None:
        _draw_skeleton(img, frame_lm.norm, width, height)
    if view is CameraView.DOWN_THE_LINE:
        _draw_horizon(img)
    if marker is not None:
        # marker 为原图像素坐标，按缩放比换算到渲染图坐标
        _draw_impact_marker(img, (marker[0] * scale, marker[1] * scale))
    _draw_label(img, f"#{event.index} f{event.frame_index} {event.timestamp:.2f}s")
    return img


def _render_one(
    bgr: np.ndarray,
    event: SwingEvent,
    frame_lm: Optional[FrameLandmarks],
    out_dir: Path,
    view: CameraView = CameraView.FACE_ON,
    marker: Optional[Tuple[int, int]] = None,
) -> str:
    """渲染并写盘单张结果图，返回文件名。"""
    img = _compose(bgr, event, frame_lm, view, marker)

    filename = phase_image_name(event.key)
    path = out_dir / filename
    ok = cv2.imwrite(
        str(path), img, [int(cv2.IMWRITE_JPEG_QUALITY), config.JPEG_QUALITY]
    )
    if not ok:
        raise AnalysisError(ErrorCode.INTERNAL, f"cannot write image: {path}")
    return filename


def render_frame_png(
    bgr: np.ndarray,
    event: SwingEvent,
    frame_lm: Optional[FrameLandmarks],
    view: CameraView = CameraView.FACE_ON,
    marker: Optional[Tuple[int, int]] = None,
) -> bytes:
    """渲染单帧骨架叠加图并编码为 PNG 字节（手动帧微调接口用，不写盘）。

    绘制样式与事件帧图完全一致（同一 :func:`_compose`），仅编码格式为 PNG。

    Args:
        bgr: 原始帧 BGR 图。
        event: 用于左上角标签（阶段号 + 实际帧号 + 时间戳）。
        frame_lm: 该帧关键点；``None`` 时只画原画面（理论上不会发生）。
        view: 机位（DTL 画水平参考线）。
        marker: 可选标记（默认关闭，与事件帧图一致）。

    Returns:
        PNG 编码字节。

    Raises:
        AnalysisError: ``INTERNAL`` —— PNG 编码失败。
    """
    img = _compose(bgr, event, frame_lm, view, marker)
    ok, encoded = cv2.imencode(".png", img)
    if not ok:
        raise AnalysisError(ErrorCode.INTERNAL, "cannot encode png")
    return encoded.tobytes()


def render_events(
    video_path: str,
    events: Sequence[SwingEvent],
    out_dir: str,
    frames: Sequence[FrameLandmarks],
    frames_bgr: Optional[Dict[int, np.ndarray]] = None,
    view: CameraView = CameraView.FACE_ON,
    markers: Optional[Dict[int, Tuple[int, int]]] = None,
) -> Dict[PhaseKey, str]:
    """在 8 个事件帧上叠加骨架并导出 JPG。

    ``frames_bgr`` 为 ``None`` 时保持既有自解码行为（第二趟解码，向后兼容 +
    测试友好）；管线应传入已解码的 8 个事件帧字典（与 pipeline 共享解码结果，
    避免额外开一趟 VideoCapture）。DTL 机位画水平参考线。

    Args:
        video_path: 原视频路径（``frames_bgr`` 已给出时不使用）。
        events: 8 个事件。
        out_dir: 输出目录。
        frames: 姿态提取产出，用于取该帧关键点。
        frames_bgr: 已解码帧字典（原视频帧号 -> BGR）；缺省时自行解码。
        view: 机位（控制水平参考线）。
        markers: 可选标记 ``{原视频帧号: (x, y)}``；仅当
            :data:`config.CLUBLITE_DRAW_MARKER` 为 True 时绘制（默认关闭，
            输出与现状逐字节一致）。

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

    draw_markers = bool(config.CLUBLITE_DRAW_MARKER and markers)

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
                bgr, event, lm_by_frame.get(frame_index), target_dir, view=view,
                marker=markers.get(frame_index) if draw_markers else None,
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
                    view=view,
                    marker=markers.get(frame_index) if draw_markers else None,
                )

    missing = [PHASE_META[e.key].name_en for e in events if e.key not in produced]
    if missing:
        raise AnalysisError(ErrorCode.INTERNAL, f"render missing: {missing}")
    return produced
