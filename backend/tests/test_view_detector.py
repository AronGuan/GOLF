"""机位自动判定测试（架构 ARCHITECTURE-v2.md §5.7 / B6）。

覆盖：竖屏/横屏 × 正面/侧面四象限判定；强特征优先；画幅先验回退；
一致性校验产出 warning；``resolve`` 的 AUTO / 显式机位两条路径。
"""

from __future__ import annotations

import numpy as np
import pytest

from app import config, view_detector
from app.schemas import CameraView, FrameLandmarks, VideoMeta

W, H = 480, 854


def make_meta(width: int = W, height: int = H) -> VideoMeta:
    return VideoMeta(
        fps=30.0, duration=1.0, width=width, height=height,
        frame_count=30, sample_step=1, low_fps=False,
    )


def make_frames(shoulder_ratio: float = 0.25, width: int = W, height: int = H,
                valid: bool = True) -> list:
    """构造单帧：图像肩宽 / 图像身高 = ``shoulder_ratio``（像素口径）。

    身高 norm = 0.7（鼻 y=0.2，双踝 y=0.9）；肩宽像素 = ratio × 身高像素。
    ``valid=False`` 时把双肩置为 NaN（强特征不可用）。
    """
    from app import geometry

    height_norm = 0.7
    ankle_y, nose_y, shoulder_y = 0.9, 0.2, 0.4
    height_px = height_norm * height
    shoulder_px = shoulder_ratio * height_px
    shoulder_norm_x = shoulder_px / width
    mid_x = 0.5

    norm = np.zeros((geometry.NUM_LANDMARKS, 3))
    if valid:
        norm[geometry.L_SHOULDER] = [mid_x - shoulder_norm_x / 2.0, shoulder_y, 0.0]
        norm[geometry.R_SHOULDER] = [mid_x + shoulder_norm_x / 2.0, shoulder_y, 0.0]
    else:
        norm[geometry.L_SHOULDER] = [np.nan, np.nan, np.nan]
        norm[geometry.R_SHOULDER] = [np.nan, np.nan, np.nan]
    norm[geometry.NOSE] = [mid_x, nose_y, 0.0]
    norm[geometry.L_ANKLE] = [mid_x - 0.1, ankle_y, 0.0]
    norm[geometry.R_ANKLE] = [mid_x + 0.1, ankle_y, 0.0]

    frame = FrameLandmarks(
        frame_index=0,
        timestamp=0.0,
        detected=True,
        norm=norm,
        world=np.zeros((geometry.NUM_LANDMARKS, 3)),
        visibility=np.full(geometry.NUM_LANDMARKS, 0.95),
    )
    return [frame]


class TestDetectView:
    """四象限判定。"""

    def test_portrait_face_on(self):
        """竖屏 + 高肩宽比(0.25) → FACE_ON。"""
        frames = make_frames(0.25, W, H)
        assert view_detector.detect_view(frames, make_meta(), 0) is CameraView.FACE_ON

    def test_portrait_dtl(self):
        """竖屏 + 低肩宽比(0.10) → DTL（强特征压过画幅先验）。"""
        frames = make_frames(0.10, W, H)
        assert view_detector.detect_view(frames, make_meta(), 0) is CameraView.DOWN_THE_LINE

    def test_landscape_face_on(self):
        """横屏 + 高肩宽比(0.25) → FACE_ON（强特征压过画幅先验）。"""
        frames = make_frames(0.25, H, W)  # 854×480
        assert view_detector.detect_view(frames, make_meta(H, W), 0) is CameraView.FACE_ON

    def test_landscape_dtl(self):
        """横屏 + 低肩宽比(0.10) → DTL。"""
        frames = make_frames(0.10, H, W)
        assert view_detector.detect_view(frames, make_meta(H, W), 0) is CameraView.DOWN_THE_LINE

    def test_strong_feature_unavailable_portrait(self):
        """强特征不可用（关键点 NaN）+ 竖屏 → FACE_ON（画幅先验回退）。"""
        frames = make_frames(0.25, W, H, valid=False)
        assert view_detector.detect_view(frames, make_meta(), 0) is CameraView.FACE_ON

    def test_strong_feature_unavailable_landscape(self):
        """强特征不可用 + 横屏 → DTL（画幅先验回退）。"""
        frames = make_frames(0.25, H, W, valid=False)
        assert view_detector.detect_view(frames, make_meta(H, W), 0) is CameraView.DOWN_THE_LINE

    def test_empty_frames_falls_back(self):
        """空帧列表不抛异常，回退默认 face-on。"""
        assert view_detector.detect_view([], make_meta(), 0) is CameraView.FACE_ON


class TestCheckConsistency:
    """一致性校验。"""

    def test_mismatch_returns_warning(self):
        warn = view_detector.check_consistency(
            CameraView.FACE_ON, CameraView.DOWN_THE_LINE
        )
        assert warn == config.WARN_VIEW_MISMATCH

    def test_match_returns_none(self):
        assert view_detector.check_consistency(
            CameraView.FACE_ON, CameraView.FACE_ON
        ) is None
        assert view_detector.check_consistency(
            CameraView.DOWN_THE_LINE, CameraView.DOWN_THE_LINE
        ) is None

    def test_auto_never_warns(self):
        assert view_detector.check_consistency(CameraView.AUTO, CameraView.FACE_ON) is None
        assert view_detector.check_consistency(CameraView.FACE_ON, CameraView.AUTO) is None


class TestResolve:
    """``resolve`` 入口。"""

    def test_auto_adopts_detected(self):
        frames = make_frames(0.10, W, H)  # DTL 特征
        view, warn = view_detector.resolve(CameraView.AUTO, frames, make_meta(), 0)
        assert view is CameraView.DOWN_THE_LINE
        assert warn is None

    def test_explicit_matching_view(self):
        frames = make_frames(0.10, W, H)  # 检测为 DTL
        view, warn = view_detector.resolve(CameraView.DOWN_THE_LINE, frames, make_meta(), 0)
        assert view is CameraView.DOWN_THE_LINE
        assert warn is None

    def test_explicit_mismatch_warns_but_keeps_chosen(self):
        frames = make_frames(0.10, W, H)  # 检测为 DTL
        view, warn = view_detector.resolve(CameraView.FACE_ON, frames, make_meta(), 0)
        assert view is CameraView.FACE_ON  # 采信用户所选，不阻断
        assert warn == config.WARN_VIEW_MISMATCH

    def test_resolve_never_returns_auto(self):
        frames = make_frames(0.10, W, H)
        view, _warn = view_detector.resolve(CameraView.AUTO, frames, make_meta(), 0)
        assert view in (CameraView.FACE_ON, CameraView.DOWN_THE_LINE)
