"""机位自动判定与一致性校验（架构 ARCHITECTURE-v2.md §5.7 / B6）。

双特征投票：
1. 画幅先验（弱）：``width > height``（横持）→ 倾向 DTL；
2. 肩宽压缩比（强）：Address 帧「图像肩宽 / 图像身高」
   face-on ≈ 0.22~0.28；DTL 因双肩前后重叠 < ``config.VIEW_SHOULDER_RATIO_DTL`` (0.13)。

两特征一致 → 采信；冲突 → 以强特征为准。

对外接口：
- :func:`detect_view`：纯判定；
- :func:`resolve`：``AUTO`` -> 采信判定；显式机位 -> 采信所选但做一致性校验，
  不一致时返回 ``config.WARN_VIEW_MISMATCH`` 提示，**不阻断**。

**模块级硬约束**：绝不外抛异常（架构 §9.3）。任何失败回退画幅先验 / 默认 face-on。
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

import numpy as np

from . import config, geometry
from .schemas import CameraView, FrameLandmarks, VideoMeta

_EPS: float = 1e-9


def _shoulder_height_ratio(
    frames: List[FrameLandmarks], meta: VideoMeta, addr_index: int
) -> Optional[float]:
    """Address 帧「图像肩宽 / 图像身高」（像素口径）。"""
    if not (0 <= addr_index < len(frames)):
        return None
    norm = frames[addr_index].norm
    if not np.all(np.isfinite(norm[geometry.L_SHOULDER, :2])) or not np.all(
        np.isfinite(norm[geometry.R_SHOULDER, :2])
    ):
        return None

    left = np.array(
        [norm[geometry.L_SHOULDER, 0] * meta.width,
         norm[geometry.L_SHOULDER, 1] * meta.height],
        dtype=np.float64,
    )
    right = np.array(
        [norm[geometry.R_SHOULDER, 0] * meta.width,
         norm[geometry.R_SHOULDER, 1] * meta.height],
        dtype=np.float64,
    )
    shoulder_px = float(np.linalg.norm(left - right))

    nose_y = float(norm[geometry.NOSE, 1]) * meta.height
    ankle_mid_y = (
        float(norm[geometry.L_ANKLE, 1]) + float(norm[geometry.R_ANKLE, 1])
    ) / 2.0 * meta.height
    height_px = geometry.body_height_px(nose_y, ankle_mid_y)

    if not math.isfinite(shoulder_px) or not math.isfinite(height_px):
        return None
    if shoulder_px <= _EPS or height_px <= _EPS:
        return None
    return shoulder_px / height_px


def detect_view(
    frames: List[FrameLandmarks], meta: VideoMeta, addr_index: int = 0
) -> CameraView:
    """机位自动判定：双特征投票（冲突以强特征 = 肩宽压缩比为准）。

    强特征不可用（关键点缺失）时回退画幅先验。
    """
    try:
        ratio = _shoulder_height_ratio(frames, meta, addr_index)
        if ratio is not None:
            if ratio < config.VIEW_SHOULDER_RATIO_DTL:
                return CameraView.DOWN_THE_LINE
            return CameraView.FACE_ON
    except Exception:  # noqa: BLE001 - 模块级硬约束：绝不外抛
        pass

    # 回退：画幅先验（弱特征）
    try:
        if meta.width > meta.height:
            return CameraView.DOWN_THE_LINE
    except Exception:  # noqa: BLE001
        pass
    return CameraView.FACE_ON


def check_consistency(
    chosen: CameraView, detected: CameraView
) -> Optional[str]:
    """一致性校验：显式所选 vs 自动判定。

    Returns:
        不一致时返回 ``config.WARN_VIEW_MISMATCH``；一致或任一为 ``AUTO`` 返回 ``None``。
    """
    if chosen is CameraView.AUTO or detected is CameraView.AUTO:
        return None
    if chosen is not detected:
        return config.WARN_VIEW_MISMATCH
    return None


def resolve(
    chosen: CameraView,
    frames: List[FrameLandmarks],
    meta: VideoMeta,
    addr_index: int = 0,
) -> Tuple[CameraView, Optional[str]]:
    """机位解析入口（pipeline 调用）。

    - ``chosen`` 是 ``AUTO``      -> 采信 :func:`detect_view`；
    - ``chosen`` 是显式机位       -> 采信 ``chosen``，但跑一次判定做一致性校验；
      不一致时返回 ``config.WARN_VIEW_MISMATCH``，**不阻断**。

    Returns:
        ``(解析后的机位, 可选的 warning)``。解析结果恒为 FACE_ON / DOWN_THE_LINE。
    """
    if chosen is CameraView.AUTO:
        return detect_view(frames, meta, addr_index), None
    detected = detect_view(frames, meta, addr_index)
    return chosen, check_consistency(chosen, detected)
