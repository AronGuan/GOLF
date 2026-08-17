"""关键点序列缓存（手动帧微调的数据底座，ARCHITECTURE-v4-frameadjust.md §3）。

**为什么需要**：结果页「上一帧/下一帧」要渲染**任意帧**的骨架叠加图，但：
1. 原视频在分析成功后会被移除（PRD Q6）——动态渲染依赖保留的 ``source.*`` 副本；
2. MediaPipe 推理结果（``FrameLandmarks``）只在流水线进程内存在，未落盘。

若在接口内对该帧重跑 ``pose_extractor``，会因缺少整段插值/平滑上下文而与原结果
**不一致**（同一帧的骨架位置不同）。因此流水线在提取/平滑完成后把全序列关键点
以 ``.npz`` 落盘到任务目录，接口直接复用**同源**关键点渲染——这是最简洁且
保证一致性的方案。

落盘内容（压缩后通常 < 1MB）：
- ``frame_index`` ``(N,)``：原视频帧号（已还原降采样）
- ``detected`` ``(N,)``：该帧是否检出人体
- ``norm`` ``(N, 33, 3)`` / ``world`` ``(N, 33, 3)`` / ``visibility`` ``(N, 33)``
- 标量：``fps`` / ``sample_step`` / ``total_frames``

只依赖 numpy / schemas / config，可被 pipeline 与 frame_service 安全导入。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Sequence

import numpy as np

from . import config
from .schemas import FrameLandmarks, VideoMeta

logger = logging.getLogger(__name__)


def save_landmarks(
    out_dir: str,
    frames: Sequence[FrameLandmarks],
    meta: VideoMeta,
) -> Path:
    """把姿态提取产出落盘到任务目录。

    Args:
        out_dir: 任务目录。
        frames: 提取/平滑后的关键点序列（与分析、渲染使用同一份）。
        meta: 视频元信息（``fps`` / ``sample_step`` / ``frame_count``）。

    Returns:
        写入的 ``.npz`` 路径。

    Raises:
        OSError: 写盘失败（调用方决定是否阻断主链路）。
    """
    target = Path(out_dir) / config.LANDMARK_CACHE_FILENAME
    n = len(frames)
    if n == 0:
        raise OSError("no frames to cache")

    np.savez_compressed(
        str(target),
        frame_index=np.asarray([f.frame_index for f in frames], dtype=np.int64),
        detected=np.asarray([bool(f.detected) for f in frames], dtype=bool),
        norm=np.stack([f.norm for f in frames]).astype(np.float32),
        world=np.stack([f.world for f in frames]).astype(np.float32),
        visibility=np.stack([f.visibility for f in frames]).astype(np.float32),
        fps=np.float64(getattr(meta, "fps", 0.0)),
        sample_step=np.int64(getattr(meta, "sample_step", 1)),
        total_frames=np.int64(getattr(meta, "frame_count", 0)),
    )
    logger.info(
        "landmark cache saved: %s (%d frames, %.1f KB)",
        target, n, target.stat().st_size / 1024.0,
    )
    return target


def load_landmarks(out_dir: str) -> List[FrameLandmarks]:
    """从任务目录读回关键点序列；缺失 / 损坏时返回空列表（调用方降级）。

    Args:
        out_dir: 任务目录。

    Returns:
        :class:`FrameLandmarks` 列表；文件缺失或解析失败返回 ``[]``。
    """
    target = Path(out_dir) / config.LANDMARK_CACHE_FILENAME
    if not target.is_file():
        return []
    try:
        with np.load(str(target), allow_pickle=False) as data:
            frame_index = np.asarray(data["frame_index"]).tolist()
            detected = np.asarray(data["detected"]).tolist()
            norm = np.asarray(data["norm"], dtype=np.float64)
            world = np.asarray(data["world"], dtype=np.float64)
            visibility = np.asarray(data["visibility"], dtype=np.float64)
            fps = float(np.asarray(data["fps"], dtype=np.float64))
    except (OSError, KeyError, ValueError) as exc:  # pragma: no cover - 防御
        logger.exception("landmark cache load failed: %s", target)
        return []

    frames: List[FrameLandmarks] = []
    for i in range(len(frame_index)):
        frames.append(
            FrameLandmarks(
                frame_index=int(frame_index[i]),
                timestamp=(float(frame_index[i]) / fps) if fps > 0 else 0.0,
                detected=bool(detected[i]),
                norm=norm[i],
                world=world[i],
                visibility=visibility[i],
            )
        )
    return frames


def find_source_video(out_dir: str) -> Optional[Path]:
    """在任务目录里找保留的视频副本（``source.*``），找不到返回 ``None``。

    副本名 = ``source`` + 原扩展名（如 ``source.mp4`` / ``source.mov``），
    用 glob 兜底，不依赖调用方知道原始扩展名。
    """
    base = Path(out_dir)
    for candidate in sorted(base.glob("source*")):
        if candidate.is_file():
            return candidate
    return None
