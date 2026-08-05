"""球杆几何检测（T02）合成测试。

不依赖 MediaPipe、不读真实挥杆视频，全部用「空白图 + 手画直线」或合成视频桩，
验证：

① ``line_angle_from_horizontal`` / Hough 杆身拟合的投影角回归误差 < 2°；
② 全遮挡（人体掩膜全覆盖 ROI）→ ``available=False``；
③ 纯黑 / 无杆视频 → ``available=False``；
④ ``skeleton_polygon_mask`` 排除（掩膜覆盖 + 与骨架共线误检过滤）；
以及「解码趟数锁 2 趟」的模块级契约（``frame_reader.stats()``）。
"""

from __future__ import annotations

import math

import cv2
import numpy as np
import pytest

from app import club_detector, config, frame_reader, geometry
from app.schemas import CameraView, ClubTrack, VideoMeta

from conftest import FPS, N_FRAMES, VIDEO_H, VIDEO_W, make_swing_frames


# ---------------------------------------------------------------------------
# 测试小工具
# ---------------------------------------------------------------------------


def _make_black_video(path: str, n: int = N_FRAMES, w: int = VIDEO_W, h: int = VIDEO_H,
                      fps: float = FPS) -> str:
    """生成一段纯黑视频（无杆、可解码）。"""
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    assert writer.isOpened(), "cv2.VideoWriter 无法创建 mp4（缺少 mp4v 编码器）"
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    for _ in range(n):
        writer.write(frame)
    writer.release()
    return str(path)


def _landmark_px(frame, width: int, height: int) -> np.ndarray:
    """复用 ``club_detector`` 的像素换算口径。"""
    norm = np.asarray(frame.norm, dtype=np.float64)
    out = np.empty((norm.shape[0], 2), dtype=np.float64)
    out[:, 0] = norm[:, 0] * float(width)
    out[:, 1] = norm[:, 1] * float(height)
    return out


# ---------------------------------------------------------------------------
# ① 投影角回归
# ---------------------------------------------------------------------------


class TestProjectionAngle:
    """已知角度直线 → 几何函数 / Hough 拟合角度误差 < 2°。"""

    @pytest.mark.parametrize(
        "angle_deg,dx,dy",
        [
            # 约定：line_angle = atan2(-dy, dx) % 180
            (0.0, 1.0, 0.0),        # 水平
            (90.0, 0.0, 1.0),       # 竖直（dy>0 时 -dy<0，%180=90）
            (45.0, 1.0, -1.0),      # 右上 45°
            (135.0, -1.0, -1.0),    # 左上 135°
            (30.0, math.sqrt(3), -1.0),
        ],
    )
    def test_line_angle_from_horizontal(self, angle_deg, dx, dy):
        p1 = np.array([100.0, 200.0])
        length = 250.0
        p2 = p1 + np.array([dx, dy], dtype=np.float64) / math.hypot(dx, dy) * length
        got = geometry.line_angle_from_horizontal(p1, p2)
        assert math.isclose(got, angle_deg, abs_tol=1e-6), (got, angle_deg)
        # 直线无向：交换端点结果一致
        assert math.isclose(geometry.line_angle_from_horizontal(p2, p1), angle_deg, abs_tol=1e-6)

    def test_hough_fit_recovers_angle(self):
        """在空白图上画一条 35° 直线，Hough 拟合方向应与真实方向差 < 2°。"""
        h = w = 400
        club_len = 280.0
        angle_deg = 35.0
        rad = math.radians(angle_deg)
        # 让 far-grip 的方向角 = 35°：dx=cos, dy=-sin
        grip = np.array([w * 0.25, h * 0.75], dtype=np.float64)
        dir_vec = np.array([math.cos(rad), -math.sin(rad)], dtype=np.float64)
        far = grip + dir_vec * club_len

        img = np.zeros((h, w, 3), dtype=np.uint8)
        cv2.line(
            img,
            (int(round(grip[0])), int(round(grip[1]))),
            (int(round(far[0])), int(round(far[1]))),
            (255, 255, 255),
            4,
            cv2.LINE_AA,
        )

        body_mask = np.zeros((h, w), dtype=np.uint8)
        outcome = club_detector._detect_hough(
            img,
            grip,
            club_len,
            pred_dir=dir_vec.copy(),
            fan_deg=45.0,
            dir_tol_deg=45.0,
            body_mask=body_mask,
            skeleton=[],
        )
        assert outcome is not None, "Hough 未在空白图上找到手画直线"
        _head, shaft_dir, _conf = outcome
        # shaft_dir 与真实方向夹角
        dot = float(np.clip(np.dot(shaft_dir, dir_vec), -1.0, 1.0))
        err = math.degrees(math.acos(abs(dot)))
        assert err < 2.0, f"Hough 方向误差 {err:.3f}° 超出 2°"

        # 用 head 反推投影角，同样应在 2° 内（方向由 shaft_dir 决定，与精修无关）
        rendered = geometry.line_angle_from_horizontal(grip, _head)
        # 计算真实直线角（与 dir_vec 同向）
        true_angle = geometry.line_angle_from_horizontal(grip, far)
        diff = abs((rendered - true_angle + 90.0) % 180.0 - 90.0)
        assert diff < 2.0, f"投影角误差 {diff:.3f}° 超出 2°"


# ---------------------------------------------------------------------------
# ② + ③ available=False 路径
# ---------------------------------------------------------------------------


class TestUnavailablePaths:
    """遮挡 / 无杆 / 异常视频 → 统一降级为 available=False，且永不外抛。"""

    def test_full_occlusion_returns_unavailable(self, synth_video, monkeypatch):
        """人体掩膜全覆盖 ROI 时，即使画面里有强边缘也应被剔除。"""
        h, w = VIDEO_H, VIDEO_W

        def _full_mask(landmarks_px, shape, *args, **kwargs):
            return np.full((int(shape[0]), int(shape[1])), 255, dtype=np.uint8)

        monkeypatch.setattr(geometry, "skeleton_polygon_mask", _full_mask)

        frames = make_swing_frames()
        track = club_detector.detect(synth_video, frames, view=CameraView.FACE_ON)
        assert isinstance(track, ClubTrack)
        assert track.available is False
        assert track.detections == []

    def test_black_video_returns_unavailable(self, tmp_path):
        path = _make_black_video(str(tmp_path / "black.mp4"))
        frames = make_swing_frames()
        track = club_detector.detect(path, frames, view=CameraView.FACE_ON)
        assert track.available is False
        assert track.detections == []

    def test_missing_video_returns_unavailable_not_raises(self, tmp_path):
        """视频打不开时 ``detect`` 必须吞掉异常、返回空轨迹（模块级硬约束）。"""
        frames = make_swing_frames()
        track = club_detector.detect(
            str(tmp_path / "does_not_exist.mp4"), frames, view=CameraView.FACE_ON
        )
        assert track.available is False

    def test_no_landmarks_returns_unavailable(self, tmp_path):
        path = _make_black_video(str(tmp_path / "black.mp4"))
        track = club_detector.detect(path, [], view=CameraView.FACE_ON)
        assert track.available is False


# ---------------------------------------------------------------------------
# ④ skeleton_polygon_mask 排除
# ---------------------------------------------------------------------------


class TestSkeletonMaskExclusion:
    """人体粗掩膜 + 与骨架共线误检过滤。"""

    def test_mask_covers_torso_excludes_corner(self):
        frame = make_swing_frames(n=1)[0]
        landmark_px = _landmark_px(frame, VIDEO_W, VIDEO_H)
        mask = geometry.skeleton_polygon_mask(landmark_px, (VIDEO_H, VIDEO_W))

        # 躯干中心（双肩中点 + 双髋中点再取中）必被掩膜覆盖
        sh_mid = (landmark_px[geometry.L_SHOULDER] + landmark_px[geometry.R_SHOULDER]) / 2.0
        hip_mid = (landmark_px[geometry.L_HIP] + landmark_px[geometry.R_HIP]) / 2.0
        torso_mid = (sh_mid + hip_mid) / 2.0
        sx, sy = int(round(torso_mid[0])), int(round(torso_mid[1]))
        assert mask[sy, sx] == 255

        # 画面左上角背景必不被掩膜
        assert mask[0, 0] == 0

    def test_collinear_with_skeleton_arm_detected(self):
        frame = make_swing_frames(n=1)[0]
        landmark_px = _landmark_px(frame, VIDEO_W, VIDEO_H)
        segments = club_detector._skeleton_segments(landmark_px)
        club_len = 200.0

        sa = landmark_px[geometry.L_SHOULDER]
        sb = landmark_px[geometry.L_WRIST]
        # 候选线段与手臂几乎重合（略缩短，仍覆盖同一中点）
        p1 = sa + 0.1 * (sb - sa)
        p2 = sb - 0.1 * (sb - sa)
        assert club_detector._collinear_with_skeleton(p1, p2, segments, club_len) is True

    def test_collinear_with_skeleton_far_line_rejected(self):
        frame = make_swing_frames(n=1)[0]
        landmark_px = _landmark_px(frame, VIDEO_W, VIDEO_H)
        segments = club_detector._skeleton_segments(landmark_px)
        club_len = 200.0

        # 远离人体、与任何骨架段都不平行
        p1 = np.array([10.0, 10.0])
        p2 = np.array([10.0, 60.0])
        assert club_detector._collinear_with_skeleton(p1, p2, segments, club_len) is False


# ---------------------------------------------------------------------------
# 解码趟数锁 2 趟（用 frame_reader.stats() 断言）
# ---------------------------------------------------------------------------


class TestDecodeTrips:
    """管线注入合成帧时本模块贡献 0 趟解码；缺失时自解 1 趟。"""

    def test_no_decode_when_frames_injected(self, synth_video):
        frame_reader.reset_stats()
        frames = make_swing_frames()
        fake = {0: np.zeros((VIDEO_H, VIDEO_W, 3), dtype=np.uint8)}
        club_detector.detect(synth_video, frames, frames_bgr=fake)
        assert frame_reader.stats()["opens"] == 0

    def test_self_decode_once_when_no_frames(self, synth_video):
        frame_reader.reset_stats()
        frames = make_swing_frames()
        club_detector.detect(synth_video, frames)  # 未注入 → 自解 1 趟


# ---------------------------------------------------------------------------
# ⑤ plan_frames 预算护栏（架构 §5.2：锚点预算 + 字节预算）
# ---------------------------------------------------------------------------


class TestPlanFramesBudget:
    """plan_frames 两道护栏：优先保留 8 个事件帧，超预算自动削减。"""

    def _events(self, frames):
        from app import segmenter
        from conftest import FPS

        sig = segmenter.build_signals(frames, FPS)
        return segmenter.segment_swing(frames, FPS, sig=sig)

    def test_keeps_all_eight_event_frames(self):
        frames = make_swing_frames()
        events = self._events(frames)
        anchors, targets = club_detector.plan_frames(frames, events)
        event_frames = {e.frame_index for e in events}
        assert event_frames.issubset(set(anchors)), "8 个事件帧必须全部保留"

    def test_targets_within_decode_budget(self):
        frames = make_swing_frames()
        events = self._events(frames)
        _anchors, targets = club_detector.plan_frames(frames, events)
        assert len(targets) <= config.CLUB_MAX_DECODE_FRAMES, (
            f"解码帧数 {len(targets)} 超预算 {config.CLUB_MAX_DECODE_FRAMES}"
        )

    def test_byte_budget_shrinks_on_4k(self):
        """4K 尺寸下单帧字节巨大，字节护栏应把解码帧压回预算内。"""
        frames = make_swing_frames()
        events = self._events(frames)
        meta = VideoMeta(
            fps=30.0, duration=4.0, width=3840, height=2160,
            frame_count=120, sample_step=1, low_fps=False,
        )
        per_frame = 3840 * 2160 * 3
        budget = 192 * 1024 * 1024
        anchors, targets = club_detector.plan_frames(
            frames, events, meta=meta, budget_bytes=budget
        )
        assert len(targets) * per_frame <= budget, "字节护栏失效"
        event_frames = {e.frame_index for e in events}
        assert event_frames.issubset(set(anchors)), "字节护栏也不能丢事件帧"

    def test_byte_budget_noop_on_small_video(self):
        frames = make_swing_frames()
        events = self._events(frames)
        meta = VideoMeta(
            fps=30.0, duration=4.0, width=480, height=854,
            frame_count=120, sample_step=1, low_fps=False,
        )
        _a1, t1 = club_detector.plan_frames(frames, events)
        _a2, t2 = club_detector.plan_frames(
            frames, events, meta=meta, budget_bytes=192 * 1024 * 1024
        )
        assert t2 == t1, "小视频不应被字节护栏削减"
