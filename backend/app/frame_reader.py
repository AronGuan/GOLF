"""共享帧解码工具（架构文档 §6.4 / 渲染支持）。

**存在的理由：让"只解码需要的帧"成为公共能力，避免重复开 VideoCapture。**

``pose_extractor.extract()`` 逐帧解码后只保留 33 点、丢弃像素（第 1 趟）；
``renderer.render_events()`` 需要事件帧的像素图做骨架叠加，若各自独立开
``VideoCapture`` 会引入额外解码趟。本模块把"顺序 grab / 命中才 retrieve"
的逻辑上提为公共工具，由 :mod:`app.pipeline` 解码后传入 :mod:`app.renderer`。

- 主管线只解码 8 个事件帧（球杆检测 2026-08 下线后不再解码窗口采样帧）；
- ``renderer`` 从"自己开 VideoCapture"变成"接收帧字典"，可测试性大幅提升
  （可直接注入合成帧，不再必须准备真视频）。

为什么用顺序 ``grab()`` 而不是 ``cap.set(CAP_PROP_POS_FRAMES, i)`` 逐帧 seek：
后者在 B 帧密集的手机 mp4 上会反复触发关键帧回退重解，稀疏取帧反而更慢，
且不同 ffmpeg 版本的定位精度不一致（帧号会漂 ±1~2）。顺序 grab 精确且可预期。

EXIF 旋转贯穿（iPhone 横拍视频横躺修复，2026-08）：
iPhone 横拍视频把帧编码为 1920×1080 横向 buffer，但元数据标记「旋转 90° 显示」
（``cv2.CAP_PROP_ORIENTATION_META`` 返回 90）。``cv2.VideoCapture`` 在不同
后端/版本下是否自动应用该旋转不一致：部分 FFmpeg 构建会解码时按 display-matrix
旋转，另一些（以及非 FFmpeg 后端）则按 buffer 原样输出 → 人物侧躺。
本模块统一处理：

- :func:`normalize_orientation` 把任意输入归一到 ``{0, 90, 180, 270}``；
- :func:`read_orientation` 从 cap 读 ``CAP_PROP_ORIENTATION_META``（带 try/except
  兜底，FFmpeg 以外的后端可能抛异常或返回 0）；
- :func:`detect_backend_applied` 通过对比「首帧实际 shape」与「CAP_PROP W/H」
  判定后端是否已自动旋转——若已旋转则不重复旋转；
- :func:`rotate_frame` 按 orientation 调用 ``cv2.rotate``（90→CLOCKWISE，
  180→180，270→COUNTERCLOCKWISE，方向按 EXIF 习惯约定）；
- :func:`grab_frames` 对每个解码帧自动转正，保证所有调用方
  （pipeline 事件帧、impact_refiner 校正窗口、renderer、frame_service
  手动帧微调）拿到的 BGR 都是「人在画面中站直」的方向，meta.width/height
  也是转正后尺寸，下游 MediaPipe 关键点、renderer 骨架图、前端 aspect 一致。
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, Iterable, List, Optional

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


def normalize_orientation(value) -> int:
    """把任意输入归一到 ``{0, 90, 180, 270}``；非法值回退 0。

    Args:
        value: ``CAP_PROP_ORIENTATION_META`` 的返回值（float / int / None）。

    Returns:
        合法旋转角度（0/90/180/270）。
    """
    try:
        n = int(float(value)) % 360
    except (TypeError, ValueError):
        return 0
    if n not in (0, 90, 180, 270):
        return 0
    return n


def read_orientation(cap: cv2.VideoCapture) -> int:
    """从已打开的 cap 读 ``CAP_PROP_ORIENTATION_META``，非 FFmpeg 后端兜底 0。

    部分 OpenCV 构建的 ``cap.get(CAP_PROP_ORIENTATION_META)`` 会抛
    ``cv2.error``（属性不存在）或返回负数；本函数把这些情况统一归一为 0。
    """
    try:
        raw = cap.get(cv2.CAP_PROP_ORIENTATION_META)
    except cv2.error:  # pragma: no cover - 后端不支持
        return 0
    except Exception:  # noqa: BLE001 - 防御性兜底
        return 0
    return normalize_orientation(raw)


def rotate_frame(bgr: np.ndarray, orientation: int) -> np.ndarray:
    """按 EXIF 旋转角度把 BGR 帧转到转正方向。

    方向约定（与 EXIF orientation tag 的「显示前需要旋转的角度」一致）：

    - ``0``   -> 原样返回；
    - ``90``  -> :data:`cv2.ROTATE_90_CLOCKWISE`（顺时针 90°）；
    - ``180`` -> :data:`cv2.ROTATE_180`；
    - ``270`` -> :data:`cv2.ROTATE_90_COUNTERCLOCKWISE`（逆时针 90°）。

    若实际视频需要反方向，调用方在探测到「转正后人物仍侧躺」时调换
    ``90`` 与 ``270`` 的分支即可（一行改动）。非旋转角度原样返回。
    """
    if bgr is None:
        return bgr
    if orientation == 90:
        return cv2.rotate(bgr, cv2.ROTATE_90_CLOCKWISE)
    if orientation == 180:
        return cv2.rotate(bgr, cv2.ROTATE_180)
    if orientation == 270:
        return cv2.rotate(bgr, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return bgr


def detect_backend_applied(
    cap: cv2.VideoCapture, bgr: Optional[np.ndarray], declared: int
) -> bool:
    """判定后端是否已自动应用 EXIF 旋转（避免重复旋转）。

    FFmpeg 的 ``autorotate`` 在不同构建/版本下默认行为不一致：开启时
    解码出的帧已是 display-orientation，未开启时按 buffer 原样输出。
    ``CAP_PROP_ORIENTATION_META`` 只反映文件元数据，与后端是否应用无关。
    因此必须用「首帧实际 shape」对比「``CAP_PROP_FRAME_WIDTH/HEIGHT``」
    （后者是 container/encoded 维度）才能区分。

    规则（仅 ``declared ∈ {90, 270}`` 时能区分；``0/180`` shape 不变，
    但 ``180`` 自反、二次旋转仍是 180，无副作用，故统一按未应用处理）：

    - 帧 ``(h, w)`` 已是 encoded ``(H, W)`` 的 swap（即 ``h == W and w == H``）
      → 后端已应用 → 不要再 rotate；
    - 否则 → 后端未应用 → 需要按 declared 旋转。

    Args:
        cap: 已打开的 VideoCapture（用于读 ``CAP_PROP_FRAME_WIDTH/HEIGHT``）。
        bgr: 首帧 BGR（用于读实际 shape）。
        declared: ``CAP_PROP_ORIENTATION_META`` 归一后的旋转角度。

    Returns:
        ``True`` 表示后端已自动旋转（调用方应跳过 ``rotate_frame``）。
    """
    if declared not in (90, 270) or bgr is None:
        return False
    try:
        encoded_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        encoded_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    except (cv2.error, TypeError, ValueError):  # pragma: no cover
        return False
    if encoded_w <= 0 or encoded_h <= 0:
        return False
    h, w = bgr.shape[:2]
    # 后端已应用 → 解码帧是 display-orientation（已 swap）→ h == encoded_h, w == encoded_w
    return h == encoded_h and w == encoded_w


def grab_frames(
    video_path: str,
    frame_indices: Iterable[int],
    orientation: Optional[int] = None,
) -> Dict[int, np.ndarray]:
    """只解码目标帧，返回 ``{frame_index: BGR 图}``。

    顺序 ``grab()`` 推进，命中目标帧号才 ``retrieve()`` 真正解码；所有目标命中后
    立即停止，不会把整段视频读完。视频提前结束导致的未命中帧**直接跳过**
    （不抛异常），由调用方决定兜底策略。

    解出的帧**已按 EXIF 旋转转正**：若调用方已知 ``orientation``（典型场景：
    pipeline 持有 :class:`VideoMeta`），传入可省一次 cap 属性读取；未传时本函数
    自动从 cap 的 ``CAP_PROP_ORIENTATION_META`` 读取，并通过首帧 shape 探测
    后端是否已自动旋转，避免「后端已旋转 + 本函数再旋转」的双重旋转。

    Args:
        video_path: 视频路径。
        frame_indices: 目标帧号（原视频帧号，任意顺序，允许重复）。
        orientation: EXIF 旋转角度（0/90/180/270）；``None`` 时自动从 cap 读。

    Returns:
        ``{frame_index: 已转正 BGR ndarray}``；未命中的帧号不会出现在结果里。
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

    declared = (
        normalize_orientation(orientation)
        if orientation is not None
        else read_orientation(cap)
    )

    pending = set(targets)
    last_target = targets[-1]
    out: Dict[int, np.ndarray] = {}
    # 后端是否已自动旋转（仅在首帧后判定；declared ∈ {90,270} 且已 swap 时跳过旋转）
    backend_applied: Optional[bool] = None
    # 首帧若 orientation 未知，自动从 cap 读取后回填 effective
    effective = declared
    # 总是探测一次 cv2 是否已应用 EXIF 旋转（避免"cv2 未转→需手动"或
    # "cv2 已转→手动又转"双重旋转）。cv2 在不同平台/Win/Linux 下行为不一致，
    # 显式 orientation 来自 metadata 也不必然代表 raw 未旋转，必须看 shape。
    auto_detect = True

    try:
        raw_index = 0
        while pending and raw_index <= last_target:
            if not cap.grab():
                break
            _bump("grabbed")
            if raw_index in pending:
                ok, bgr = cap.retrieve()
                if ok and bgr is not None:
                    if backend_applied is None:
                        backend_applied = detect_backend_applied(
                            cap, bgr, effective
                        )
                        if backend_applied:
                            logger.info(
                                "backend already applied EXIF rotation "
                                "(declared=%d, frame shape %dx%d vs encoded %dx%d); "
                                "skip manual rotate to avoid double-rotation (%s)",
                                effective,
                                bgr.shape[1],
                                bgr.shape[0],
                                int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
                                int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
                                video_path,
                            )
                            effective = 0
                    if effective != 0 and not backend_applied:
                        bgr = rotate_frame(bgr, effective)
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
