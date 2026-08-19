"""``app.pose_extractor`` 单测：视频探测、亮度、平滑工具。

除最后一条 MediaPipe 自检外，其余用例都不加载模型。
"""

from __future__ import annotations

import os

import cv2
import numpy as np
import pytest

from app import config, pose_extractor
from app.schemas import AnalysisError, ErrorCode

PROBE_MP4 = r"E:\project\golf\.tools\_probe\t.mp4"

#: 真实 iPhone 横拍视频（orientation=90），用户报 bug 的真机素材（仅本地存在）
_REAL_ORIENT_90 = r"C:\Users\98025\Desktop\视频\2026-08-19_125638_118.mp4"


def _write_black_mp4(path: str, w: int = 320, h: int = 240, frames: int = 24) -> None:
    """写一个确定全黑（灰度 0）的 mp4，用于亮度阈值验证。"""
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 30.0, (w, h))
    assert writer.isOpened(), "cv2.VideoWriter 无法创建 mp4"
    for _ in range(frames):
        writer.write(np.zeros((h, w, 3), dtype=np.uint8))
    writer.release()


class TestProbeVideo:
    """``probe_video``。"""

    def test_valid_video(self, synth_video):
        meta = pose_extractor.probe_video(synth_video)
        assert meta.fps == pytest.approx(30.0, abs=0.1)
        assert meta.frame_count == 120
        assert meta.duration == pytest.approx(4.0, abs=0.1)
        assert (meta.width, meta.height) == (480, 854)
        assert meta.sample_step == 1
        assert meta.low_fps is False
        # cv2.VideoWriter 不会写 EXIF orientation 标签，synth_video 期望默认 0
        assert meta.orientation == 0

    def test_missing_file_raises_bad_video(self, tmp_path):
        with pytest.raises(AnalysisError) as exc:
            pose_extractor.probe_video(str(tmp_path / "nope.mp4"))
        assert exc.value.code is ErrorCode.BAD_VIDEO

    def test_non_video_file_raises_bad_video(self, tmp_path):
        path = tmp_path / "fake.mp4"
        path.write_bytes(b"not a video at all")
        with pytest.raises(AnalysisError) as exc:
            pose_extractor.probe_video(str(path))
        assert exc.value.code is ErrorCode.BAD_VIDEO

    def test_too_short_duration_raises_bad_video(self):
        """t.mp4 时长 1.0s < MIN_DURATION_SEC(1.5)。"""
        with pytest.raises(AnalysisError) as exc:
            pose_extractor.probe_video(PROBE_MP4)
        assert exc.value.code is ErrorCode.BAD_VIDEO
        assert "duration" in exc.value.detail

    @pytest.mark.skipif(
        not os.path.exists(_REAL_ORIENT_90),
        reason="真实 iPhone 横拍视频不存在（CI 环境跳过）",
    )
    def test_real_iphone_landscape_video_rejected(self):
        """iPhone 横拍视频（orientation=90）→ probe 阶段直接拒绝 BAD_ORIENTATION。

        产品决策（2026-08）：放弃 EXIF 自动旋转（cv2 跨平台行为不一致），
        orientation ≠ 0 一律拒绝并提示竖拍。该文件是用户报 bug 的真机素材
        （此前 cv2 验证 orientation=90），probe_video 必须抛 BAD_ORIENTATION
        且携带对外码 10005，不得再返回转正尺寸。
        """
        with pytest.raises(AnalysisError) as exc:
            pose_extractor.probe_video(_REAL_ORIENT_90)
        assert exc.value.code is ErrorCode.BAD_ORIENTATION
        assert exc.value.pdd_code == config.PDD_CODE_BAD_ORIENTATION  # 10005
        assert "orientation" in exc.value.detail

    @pytest.mark.parametrize("orientation", [90, 180, 270])
    def test_rejects_nonzero_orientation(self, synth_video, monkeypatch, orientation):
        """orientation ∈ {90,180,270} → probe 拒绝（不依赖真实 90° 视频）。

        用 monkeypatch 直接改 ``read_orientation`` 的返回值，测 probe 的拒绝逻辑：
        任何非 0 的 EXIF 旋转标记都必须抛 BAD_ORIENTATION + pdd_code=10005。
        """
        monkeypatch.setattr(pose_extractor, "read_orientation", lambda cap: orientation)
        with pytest.raises(AnalysisError) as exc:
            pose_extractor.probe_video(synth_video)
        assert exc.value.code is ErrorCode.BAD_ORIENTATION
        assert exc.value.pdd_code == config.PDD_CODE_BAD_ORIENTATION  # 10005
        assert str(orientation) in exc.value.detail

    def test_orientation_zero_passes(self, synth_video, monkeypatch):
        """orientation=0（竖拍/无旋转标记）→ 正常通过，不抛 BAD_ORIENTATION。

        显式把 ``read_orientation`` 钉在 0，确保拒绝逻辑只拦非 0，不影响竖拍视频。
        """
        monkeypatch.setattr(pose_extractor, "read_orientation", lambda cap: 0)
        meta = pose_extractor.probe_video(synth_video)
        assert meta.orientation == 0
        assert (meta.width, meta.height) == (480, 854)


class TestBrightness:
    """``check_brightness``。"""

    def test_bright_video_passes(self, synth_video):
        pose_extractor.check_brightness(synth_video)  # 不抛异常即通过

    def test_black_video_raises_too_dark(self, tmp_path):
        """确定全黑（灰度 0）视频 -> TOO_DARK。

        注：共享资产 t.mp4 实为灰度渐变（均值≈114），不触发阈值，
        故亮度用例自造确定全黑视频，避免对外部素材内容的脆弱假设。
        """
        black = str(tmp_path / "black.mp4")
        _write_black_mp4(black)
        with pytest.raises(AnalysisError) as exc:
            pose_extractor.check_brightness(black)
        assert exc.value.code is ErrorCode.TOO_DARK

    def test_unopenable_raises_bad_video(self, tmp_path):
        path = tmp_path / "fake.mp4"
        path.write_bytes(b"xx")
        with pytest.raises(AnalysisError) as exc:
            pose_extractor.check_brightness(str(path))
        assert exc.value.code is ErrorCode.BAD_VIDEO


class TestSmoothing:
    """``smooth_window`` / ``moving_average``。"""

    @pytest.mark.parametrize("fps_eff", [15.0, 25.0, 30.0, 60.0, 120.0])
    def test_window_is_odd_and_at_least_three(self, fps_eff):
        win = pose_extractor.smooth_window(fps_eff)
        assert win >= 3
        assert win % 2 == 1

    def test_window_scales_with_fps(self):
        assert pose_extractor.smooth_window(120.0) > pose_extractor.smooth_window(30.0)

    def test_moving_average_preserves_shape(self):
        arr = np.random.default_rng(0).normal(size=(50, 3))
        out = pose_extractor.moving_average(arr, 5)
        assert out.shape == arr.shape
        assert np.isfinite(out).all()

    def test_moving_average_constant_signal_unchanged(self):
        """常数信号平滑后仍是常数（边缘填充正确的必要条件）。"""
        arr = np.full((30, 2), 7.0)
        assert np.allclose(pose_extractor.moving_average(arr, 5), 7.0)

    def test_moving_average_reduces_noise(self):
        rng = np.random.default_rng(1)
        clean = np.linspace(0, 1, 200).reshape(-1, 1)
        noisy = clean + rng.normal(scale=0.1, size=clean.shape)
        smoothed = pose_extractor.moving_average(noisy, 9)
        assert np.std(smoothed - clean) < np.std(noisy - clean)

    def test_moving_average_short_input_passthrough(self):
        arr = np.array([[1.0], [2.0]])
        assert np.allclose(pose_extractor.moving_average(arr, 5), arr)


class TestInterpolation:
    """缺帧插值。"""

    def test_missing_frames_filled(self):
        from conftest import make_swing_frames

        frames = make_swing_frames(n=20)
        frames[5].detected = False
        frames[5].norm = np.full_like(frames[5].norm, np.nan)
        frames[5].world = np.full_like(frames[5].world, np.nan)
        pose_extractor._interpolate_missing(frames)
        assert np.isfinite(frames[5].norm).all()
        assert np.isfinite(frames[5].world).all()

    def test_all_missing_is_noop(self):
        from conftest import make_swing_frames

        frames = make_swing_frames(n=5)
        for frame in frames:
            frame.detected = False
        pose_extractor._interpolate_missing(frames)  # 不应抛异常


class TestMediaPipeRuntime:
    """真实加载 MediaPipe legacy Pose，验证离线可用（无需下载 .task 模型）。"""

    def test_pose_runs_on_blank_frame(self):
        import mediapipe as mp

        with mp.solutions.pose.Pose(**config.POSE_KW) as pose:
            blank = np.zeros((480, 320, 3), dtype=np.uint8)
            result = pose.process(blank)
        assert result is not None
        # 纯黑图不应检出人体
        assert result.pose_landmarks is None
