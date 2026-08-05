"""共享帧解码工具（球杆检测技术方案 §4.1）。

**存在的唯一理由：把解码趟数锁死在 2 趟。**

``pose_extractor.extract()`` 逐帧解码后只保留 33 点、丢弃像素（第 1 趟）；
``renderer.render_events()`` 原本又独立开一次 ``VideoCapture``（第 2 趟）。
球杆检测同样需要像素，天真做法会引入**第 3 趟解码**（10s / 1080p 约 +2~5s）。

本模块把那段"顺序 grab / 命中才 retrieve"的逻辑上提为公共工具，由
:mod:`app.club_detector` 与 :mod:`app.renderer` **共享同一次解码结果**：

- 解码趟数 2 趟不变，球杆检测的 I/O 成本 = 0；
- ``renderer`` 从"自己开 VideoCapture"变成"接收帧字典"，可测试性大幅提升
  （可直接注入合成帧，不再必须准备真视频）。

为什么用顺序 ``grab()`` 而不是 ``cap.set(CAP_PROP_POS_FRAMES, i)`` 逐帧 seek：
后者在 B 帧密集的手机 mp4 上会反复触发关键帧回退重解，稀疏取 24~48 帧反而更慢，
且不同 ffmpeg 版本的定位精度不一致（帧号会漂 ±1~2）。顺序 grab 精确且可预期。
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, Iterable, List

import cv2
import numpy as np

from .schemas import AnalysisError, ErrorCode

logger = logging.getLogger(__name__)

#: 解码统计（进程级，仅供测试与性能排查断言"解码趟数"）
_STATS_LOCK = threading.Lock()
_STATS: Dict[str, int] = {"opens": 0, "retrieved": 0, "grabbed": 0}


def reset_stats() -> None:
    """清零解码统计。"""
    with _STATS_LOCK:
        _STATS["opens"] = 0
        _STATS["retrieved"] = 0
        _STATS["grabbed"] = 0


def stats() -> Dict[str, int]:
    """读取解码统计快照。

    Returns:
        ``{"opens": 打开次数, "retrieved": 真正解码的帧数, "grabbed": grab 次数}``。
        ``opens`` 即本模块贡献的**解码趟数**。
    """
    with _STATS_LOCK:
        return dict(_STATS)


def _bump(key: str, delta: int = 1) -> None:
    """线程安全地累加统计项。"""
    with _STATS_LOCK:
        _STATS[key] = _STATS.get(key, 0) + delta


def normalize_indices(frame_indices: Iterable[int]) -> List[int]:
    """把任意可迭代帧号整理成「去重 + 升序 + 非负」的列表。"""
    cleaned: List[int] = []
    for raw in frame_indices:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            cleaned.append(value)
    return sorted(set(cleaned))


def grab_frames(
    video_path: str, frame_indices: Iterable[int]
) -> Dict[int, np.ndarray]:
    """只解码目标帧，返回 ``{frame_index: BGR 图}``。

    顺序 ``grab()`` 推进，命中目标帧号才 ``retrieve()`` 真正解码；所有目标命中后
    立即停止，不会把整段视频读完。视频提前结束导致的未命中帧**直接跳过**
    （不抛异常），由调用方决定兜底策略。

    Args:
        video_path: 视频路径。
        frame_indices: 目标帧号（原视频帧号，任意顺序，允许重复）。

    Returns:
        ``{frame_index: BGR ndarray}``；未命中的帧号不会出现在结果里。
        ``frame_indices`` 为空时返回空字典且**不打开视频**。

    Raises:
        AnalysisError: ``BAD_VIDEO`` —— 视频无法打开。
    """
    targets = normalize_indices(frame_indices)
    if not targets:
        return {}

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        cap.release()
        raise AnalysisError(ErrorCode.BAD_VIDEO, f"cannot open video: {video_path}")
    _bump("opens")

    pending = set(targets)
    last_target = targets[-1]
    out: Dict[int, np.ndarray] = {}

    try:
        raw_index = 0
        while pending and raw_index <= last_target:
            if not cap.grab():
                break
            _bump("grabbed")
            if raw_index in pending:
                ok, bgr = cap.retrieve()
                if ok and bgr is not None:
                    out[raw_index] = bgr
                    _bump("retrieved")
                pending.discard(raw_index)
            raw_index += 1
    finally:
        cap.release()

    if pending:
        logger.warning(
            "grab_frames missed %d/%d frames (video ended early): %s",
            len(pending),
            len(targets),
            sorted(pending)[:8],
        )
    return out
