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

from . import config, geometry
from .schemas import (
    AnalysisError,
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


def _render_one(
    bgr: np.ndarray,
    event: SwingEvent,
    frame_lm: Optional[FrameLandmarks],
    out_dir: Path,
) -> str:
    """渲染并写盘单张结果图，返回文件名。"""
    img, _ = _resize_long_side(bgr, config.RENDER_LONG_SIDE)
    height, width = img.shape[:2]
    if frame_lm is not None:
        _draw_skeleton(img, frame_lm.norm, width, height)
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
) -> Dict[PhaseKey, str]:
    """第二趟顺序解码，在 8 个事件帧上叠加骨架并导出 JPG。

    Args:
        video_path: 原视频路径。
        events: 8 个事件。
        out_dir: 输出目录。
        frames: 姿态提取产出，用于取该帧关键点。

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

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        cap.release()
        raise AnalysisError(ErrorCode.BAD_VIDEO, f"cannot open video: {video_path}")

    produced: Dict[PhaseKey, str] = {}
    pending = dict(targets)
    last_bgr: Optional[np.ndarray] = None

    try:
        raw_index = 0
        while pending:
            grabbed = cap.grab()
            if not grabbed:
                break
            if raw_index in pending:
                ok, bgr = cap.retrieve()
                if ok and bgr is not None:
                    last_bgr = bgr
                    for event in pending[raw_index]:
                        produced[event.key] = _render_one(
                            bgr, event, lm_by_frame.get(event.frame_index), target_dir
                        )
                    del pending[raw_index]
            raw_index += 1
    finally:
        cap.release()

    # 视频提前结束导致的漏帧：用最后一张成功解码的画面兜底，保证恒 8 张
    if pending:
        logger.warning("render fallback for frames: %s", sorted(pending.keys()))
        if last_bgr is None:
            raise AnalysisError(ErrorCode.BAD_VIDEO, "no frame decoded for rendering")
        for frame_index, event_list in pending.items():
            for event in event_list:
                produced[event.key] = _render_one(
                    last_bgr, event, lm_by_frame.get(frame_index), target_dir
                )

    missing = [PHASE_META[e.key].name_en for e in events if e.key not in produced]
    if missing:
        raise AnalysisError(ErrorCode.INTERNAL, f"render missing: {missing}")
    return produced
