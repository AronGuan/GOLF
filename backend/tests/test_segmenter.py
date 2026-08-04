"""``app.segmenter`` 8 阶段切分单测（架构文档 §7）。

用手工构造的 :class:`FrameLandmarks` 序列驱动，不加载 MediaPipe、不读真实视频。
核心断言（架构 §7.5 契约）：**恒 8 个、帧号严格递增、均在合法范围内**。
"""

from __future__ import annotations

import numpy as np
import pytest

from app import config, segmenter
from app.schemas import PHASE_ORDER, AnalysisError, ErrorCode, PhaseKey

from conftest import FPS, N_FRAMES, build_pose, make_still_frames, make_swing_frames

from app import geometry


# ---------------------------------------------------------------------------
# build_signals
# ---------------------------------------------------------------------------


class TestBuildSignals:
    """S1~S8 信号构建。"""

    def test_shapes_and_scale(self, swing_frames):
        sig = segmenter.build_signals(swing_frames, FPS)
        assert sig.n == N_FRAMES
        for arr in (sig.wrist_x, sig.wrist_y, sig.shoulder_mid_y,
                    sig.hip_mid_y, sig.h, sig.speed):
            assert arr.shape == (N_FRAMES,)
            assert np.isfinite(arr).all(), "信号中不允许出现 NaN/inf"
        # S 是全片归一化肩宽中位数（图像坐标，∈[0,1]）。站位帧（th_s=0）肩宽投影
        # 为 0.20；挥杆中躯干转动使投影收窄，故中位数 S 应小于站位帧宽度，且为正、
        # 有限、落在合理区间。
        addr_world, addr_norm = build_pose(0.0)
        addr_width = float(np.linalg.norm(
            addr_norm[geometry.L_SHOULDER, :2] - addr_norm[geometry.R_SHOULDER, :2]
        ))
        assert addr_width == pytest.approx(0.20, abs=0.02)  # 设计：0.40m / SCENE_W 2.0
        assert np.isfinite(sig.S) and sig.S > 0
        assert 0.03 < sig.S < addr_width  # 中位数 < 站位帧宽度（转身收窄投影）
        # 公式一致性：S 应等于「逐帧归一化肩宽中位数」
        manual = float(np.median([
            np.linalg.norm(f.norm[geometry.L_SHOULDER, :2] - f.norm[geometry.R_SHOULDER, :2])
            for f in swing_frames
        ]))
        assert sig.S == pytest.approx(manual, rel=1e-9)
        assert sig.dt == pytest.approx(1.0 / FPS)
        assert sig.fps_eff == pytest.approx(FPS)

    def test_sample_step_recovered(self):
        """降采样序列（帧号步长 2）须还原出 dt = 2/fps。"""
        frames = make_swing_frames(n=60, fps=FPS, step=2)
        sig = segmenter.build_signals(frames, FPS)
        assert sig.dt == pytest.approx(2.0 / FPS)
        assert sig.fps_eff == pytest.approx(FPS / 2.0)

    def test_empty_raises_no_swing(self):
        with pytest.raises(AnalysisError) as exc:
            segmenter.build_signals([], FPS)
        assert exc.value.code is ErrorCode.NO_SWING

    def test_zero_shoulder_scale_raises_no_swing(self):
        """肩宽标尺异常（§7.6 判据 1）。"""
        frames = make_swing_frames(n=40)
        for frame in frames:
            frame.norm = frame.norm.copy()
            frame.norm[segmenter.geometry.L_SHOULDER, :2] = 0.5
            frame.norm[segmenter.geometry.R_SHOULDER, :2] = 0.5
        with pytest.raises(AnalysisError) as exc:
            segmenter.build_signals(frames, FPS)
        assert exc.value.code is ErrorCode.NO_SWING

    def test_speed_peak_during_downswing(self):
        """物理前提：全局速度峰应落在顶点之后（下杆/击球段）。"""
        sig = segmenter.build_signals(make_swing_frames(), FPS)
        i_peak = int(np.argmax(sig.speed))
        i_top = segmenter.locate_top(sig)
        assert i_peak > i_top


# ---------------------------------------------------------------------------
# segment_swing 主契约
# ---------------------------------------------------------------------------


class TestSegmentSwingContract:
    """架构 §7.5：恒 8 个、严格递增、落在合法区间。"""

    @pytest.fixture(scope="class")
    def events(self):
        return segmenter.segment_swing(make_swing_frames(), FPS)

    def test_exactly_eight_events(self, events):
        assert len(events) == 8

    def test_phase_order_and_index(self, events):
        assert [e.key for e in events] == list(PHASE_ORDER)
        assert [e.index for e in events] == list(range(1, 9))

    def test_frame_index_strictly_increasing(self, events):
        frames = [e.frame_index for e in events]
        assert all(b > a for a, b in zip(frames, frames[1:])), frames

    def test_array_index_strictly_increasing(self, events):
        idx = [e.array_index for e in events]
        assert all(b > a for a, b in zip(idx, idx[1:])), idx

    def test_indices_within_range(self, events):
        for event in events:
            assert 0 <= event.array_index <= N_FRAMES - 1
            assert 0 <= event.frame_index <= N_FRAMES - 1

    def test_timestamps_increasing_and_consistent(self, events):
        stamps = [e.timestamp for e in events]
        assert all(b > a for a, b in zip(stamps, stamps[1:])), stamps
        for event in events:
            assert event.timestamp == pytest.approx(event.frame_index / FPS, abs=1e-3)

    def test_anchor_events_are_not_estimated(self, events):
        """合成序列信号干净，四锚点应被真实定位而非兜底。"""
        by_key = {e.key: e for e in events}
        for key in (PhaseKey.ADDRESS, PhaseKey.TOP, PhaseKey.IMPACT, PhaseKey.FINISH):
            assert by_key[key].estimated is False, f"{key} 退化成了兜底估算"

    def test_events_land_near_designed_keyframes(self, events):
        """定位结果须落在合成轨迹的设计时刻附近（±0.25s）。"""
        by_key = {e.key: e.timestamp for e in events}
        assert by_key[PhaseKey.ADDRESS] == pytest.approx(0.70, abs=0.25)
        assert by_key[PhaseKey.TOP] == pytest.approx(1.45, abs=0.25)
        assert by_key[PhaseKey.IMPACT] == pytest.approx(1.80, abs=0.25)
        assert by_key[PhaseKey.FINISH] == pytest.approx(2.40, abs=0.25)

    def test_downsampled_sequence_also_valid(self):
        """降采样（步长 2）序列同样必须满足 8 个 + 严格递增。"""
        frames = make_swing_frames(n=60, fps=FPS, step=2)
        events = segmenter.segment_swing(frames, FPS)
        assert len(events) == 8
        nums = [e.frame_index for e in events]
        assert all(b > a for a, b in zip(nums, nums[1:])), nums
        assert all(0 <= n <= 118 for n in nums)
        assert all(n % 2 == 0 for n in nums), "帧号必须还原到原视频（步长 2）"


# ---------------------------------------------------------------------------
# NO_SWING 判据（架构 §7.6）
# ---------------------------------------------------------------------------


class TestNoSwingGuards:
    """六条判据的可测子集。"""

    def test_still_standing_raises_no_swing(self, still_frames):
        """判据 2：静止站立（PRD AC-10）。"""
        with pytest.raises(AnalysisError) as exc:
            segmenter.segment_swing(still_frames, FPS)
        assert exc.value.code is ErrorCode.NO_SWING

    @pytest.mark.parametrize("n", [1, 5, 9, 14])
    def test_too_few_frames_raises_no_swing(self, n):
        """判据 1：帧数 < max(10, round(0.5*fps))。"""
        frames = make_swing_frames(n=n)
        with pytest.raises(AnalysisError) as exc:
            segmenter.segment_swing(frames, FPS)
        assert exc.value.code is ErrorCode.NO_SWING

    def test_tiny_motion_raises_no_swing(self):
        """判据 3：手腕垂直行程不足 0.60*S。"""
        frames = make_swing_frames()
        base = frames[0].norm.copy()
        for k, frame in enumerate(frames):
            norm = base.copy()
            # 只给手腕一点点抖动：速度峰可能够，但垂直行程远不足
            norm[segmenter.geometry.L_WRIST, 0] += 0.02 * np.sin(k * 0.9)
            norm[segmenter.geometry.L_WRIST, 1] += 0.004 * np.sin(k * 0.9)
            frame.norm = norm
        with pytest.raises(AnalysisError) as exc:
            segmenter.segment_swing(frames, FPS)
        assert exc.value.code is ErrorCode.NO_SWING

    def test_guard_message_maps_to_chinese(self):
        """NO_SWING 必须能映射到 PRD §5.3 的中文文案。"""
        assert config.error_message(ErrorCode.NO_SWING.value) == (
            "没有识别到完整的挥杆动作，请拍摄从站位到收杆的完整过程"
        )


# ---------------------------------------------------------------------------
# 锚点定位与工具函数
# ---------------------------------------------------------------------------


class TestAnchors:
    """四锚点顺序关系。"""

    @pytest.fixture(scope="class")
    def sig(self):
        return segmenter.build_signals(make_swing_frames(), FPS)

    def test_anchor_ordering(self, sig):
        i_top = segmenter.locate_top(sig)
        i_addr, _ = segmenter.locate_address(sig, i_top)
        i_impact, _ = segmenter.locate_impact(sig, i_top, i_addr)
        i_finish, _ = segmenter.locate_finish(sig, i_impact)
        assert 0 <= i_addr < i_top < i_impact < i_finish <= sig.n - 1

    def test_top_is_highest_wrist_position(self, sig):
        """顶点处手腕图像 y 应接近全片最小值（图像 y 向下为正）。"""
        i_top = segmenter.locate_top(sig)
        assert float(sig.wrist_y[i_top]) <= float(np.min(sig.wrist_y)) + 0.05 * sig.S + 0.02

    def test_intermediate_all_located(self, sig):
        i_top = segmenter.locate_top(sig)
        i_addr, _ = segmenter.locate_address(sig, i_top)
        i_impact, _ = segmenter.locate_impact(sig, i_top, i_addr)
        i_finish, _ = segmenter.locate_finish(sig, i_impact)
        mid = segmenter.locate_intermediate(sig, (i_addr, i_top, i_impact, i_finish))
        assert set(mid) == {
            PhaseKey.TAKEAWAY, PhaseKey.BACKSWING,
            PhaseKey.DOWNSWING, PhaseKey.FOLLOW_THROUGH,
        }
        for key, (idx, _est) in mid.items():
            assert 0 <= idx <= sig.n - 1, key

    def test_top_at_head_raises(self, sig):
        with pytest.raises(AnalysisError) as exc:
            segmenter.locate_address(sig, 0)
        assert exc.value.code is ErrorCode.NO_SWING

    def test_top_at_tail_raises(self, sig):
        with pytest.raises(AnalysisError) as exc:
            segmenter.locate_impact(sig, sig.n - 1, 0)
        assert exc.value.code is ErrorCode.NO_SWING


class TestRunsBelow:
    """``_runs_below`` 连续段检测。"""

    def test_basic_runs(self):
        values = np.array([0.0, 0.0, 5.0, 0.0, 0.0, 0.0, 5.0])
        assert segmenter._runs_below(values, 1.0) == [(0, 1), (3, 5)]

    def test_run_to_the_end(self):
        values = np.array([5.0, 0.0, 0.0])
        assert segmenter._runs_below(values, 1.0) == [(1, 2)]

    def test_no_run(self):
        assert segmenter._runs_below(np.array([5.0, 6.0]), 1.0) == []

    def test_nan_is_not_below(self):
        values = np.array([0.0, np.nan, 0.0])
        assert segmenter._runs_below(values, 1.0) == [(0, 0), (2, 2)]


class TestMonotonic:
    """§7.4 单调性校正。"""

    def test_already_monotonic_unchanged(self):
        idx, est = segmenter.enforce_monotonic_indices(
            [0, 1, 2, 3, 4, 5, 6, 7], [False] * 8, 100
        )
        assert idx == [0, 1, 2, 3, 4, 5, 6, 7]
        assert est == [False] * 8

    def test_duplicates_pushed_forward_and_marked_estimated(self):
        idx, est = segmenter.enforce_monotonic_indices(
            [10, 10, 10, 13, 14, 15, 16, 17], [False] * 8, 100
        )
        assert idx == [10, 11, 12, 13, 14, 15, 16, 17]
        assert est[1] is True and est[2] is True
        assert est[0] is False and est[3] is False

    def test_overflow_squeezed_backwards(self):
        idx, est = segmenter.enforce_monotonic_indices(
            [90, 91, 92, 93, 94, 95, 96, 120], [False] * 8, 100
        )
        assert idx[-1] == 99
        assert all(b > a for a, b in zip(idx, idx[1:])), idx
        assert est[-1] is True

    def test_impossible_fit_raises(self):
        """序列容不下 8 个严格递增事件（§7.6 判据 6）。"""
        with pytest.raises(AnalysisError) as exc:
            segmenter.enforce_monotonic_indices([0] * 8, [False] * 8, 5)
        assert exc.value.code is ErrorCode.NO_SWING

    def test_enforce_monotonic_detects_conflict(self):
        from app.schemas import SwingEvent

        events = [
            SwingEvent(index=i + 1, key=key, frame_index=0, timestamp=0.0,
                       estimated=False, array_index=0)
            for i, key in enumerate(PHASE_ORDER)
        ]
        with pytest.raises(AnalysisError) as exc:
            segmenter.enforce_monotonic(events)
        assert exc.value.code is ErrorCode.NO_SWING
