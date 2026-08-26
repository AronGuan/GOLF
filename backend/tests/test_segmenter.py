"""``app.segmenter`` 8 阶段切分单测（架构文档 §7）。

用手工构造的 :class:`FrameLandmarks` 序列驱动，不加载 MediaPipe、不读真实视频。
核心断言（架构 §7.5 契约）：**恒 8 个、帧号严格递增、均在合法范围内**。
"""

from __future__ import annotations

import numpy as np
import pytest

from app import config, segmenter
from app.schemas import (
    PHASE_ORDER,
    AnalysisError,
    CameraView,
    ErrorCode,
    FrameLandmarks,
    PhaseKey,
    SwingSignals,
)

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


# ---------------------------------------------------------------------------
# ⑤下杆机位感知（2026-08 改造：face-on / DTL 分机位阈值）
# ---------------------------------------------------------------------------


def _downswing_sweep_signals() -> SwingSignals:
    """构造 h 在顶点→击球间线性单调递减的信号包，验证 ⑤ 阈值分机位。

    h: ``[0,5)`` 站位 ≈ 0；``[5,40)`` 上杆 0→2；``[40,60)`` 下杆 2→0（线性）；
    ``[60,90)`` 送杆 -0.1→0.48；``[90,100)`` 收杆 ≈ -0.1。
    下杆段 ``h[i] = 2*(60-i)/20``（i ∈ [40,60]）：
      - 首次 ``h <= 0.50``（:data:`config.H_DOWNSWING`）发生在 i=55；
      - 首次 ``h <= 0.18``（测试用 :data:`config.H_DOWNSWING_DTL`）发生在 i=59
        （h[58]=0.20 > 0.18，h[59]=0.10 <= 0.18）。
    ``h = (hip_mid_y - wrist_y) / S``，取 S=1、hip_mid_y=0 → ``wrist_y = -h``。
    """
    n = 100
    h = np.empty(n, dtype=np.float64)
    for i in range(n):
        if i < 5:
            h[i] = 0.02
        elif i < 40:
            h[i] = 2.0 * (i - 5) / 35.0
        elif i < 60:
            h[i] = 2.0 * (60 - i) / 20.0
        elif i < 90:
            h[i] = -0.1 + 0.6 * (i - 60) / 30.0
        else:
            h[i] = -0.1
    wrist_y = -h
    return SwingSignals(
        n=n,
        fps=30.0,
        dt=1.0 / 30.0,
        S=1.0,
        wrist_x=np.zeros(n, dtype=np.float64),
        wrist_y=wrist_y,
        shoulder_mid_y=np.full(n, 0.5, dtype=np.float64),
        hip_mid_y=np.zeros(n, dtype=np.float64),
        h=h,
        speed=np.zeros(n, dtype=np.float64),
    )


def _frames_for_reanchor(n: int = 100):
    """与 :func:`_downswing_sweep_signals` 配套的 FrameLandmarks 序列。"""
    return [
        FrameLandmarks(
            frame_index=i,
            timestamp=i / 30.0,
            detected=True,
            norm=np.zeros((33, 3), dtype=np.float64),
            world=np.zeros((33, 3), dtype=np.float64),
            visibility=np.zeros(33, dtype=np.float64),
        )
        for i in range(n)
    ]


class TestViewAwareDownswing:
    """⑤ 下杆机位感知：view 参数向后兼容 + DTL 阈值生效 + 正面零影响。"""

    def test_segment_swing_default_equals_face_on(self, swing_frames):
        """不传 view 与显式 face-on 必须逐字节一致（历史行为保持）。"""
        ev_default = segmenter.segment_swing(swing_frames, FPS)
        ev_face = segmenter.segment_swing(swing_frames, FPS, view=CameraView.FACE_ON)
        assert [(e.key, e.frame_index, e.estimated) for e in ev_default] == [
            (e.key, e.frame_index, e.estimated) for e in ev_face
        ]

    def test_locate_intermediate_dtl_uses_dedicated_threshold(self, monkeypatch):
        """view=DTL 用 H_DOWNSWING_DTL：阈值调低 → h 单调递减 → ⑤ 更靠后。"""
        sig = _downswing_sweep_signals()
        anchors = (5, 40, 60, 90)
        # 基准：DTL 阈值 == face-on 阈值 → ⑤ 相同
        monkeypatch.setattr(config, "H_DOWNSWING_DTL", config.H_DOWNSWING)
        mid_same = segmenter.locate_intermediate(
            sig, anchors, view=CameraView.DOWN_THE_LINE
        )
        ds_same = mid_same[PhaseKey.DOWNSWING][0]
        assert ds_same == 55
        # 调低 DTL 阈值 → 首次下穿更晚 → ⑤ 更靠后（偏离顶点、接近击球）
        monkeypatch.setattr(config, "H_DOWNSWING_DTL", 0.18)
        mid_low = segmenter.locate_intermediate(
            sig, anchors, view=CameraView.DOWN_THE_LINE
        )
        ds_low = mid_low[PhaseKey.DOWNSWING][0]
        assert ds_low == 59
        assert ds_low > ds_same
        assert ds_low < 60  # ⑤ 仍严格早于 ⑥

    def test_face_on_ignores_dtl_threshold(self, monkeypatch):
        """face-on 恒用 H_DOWNSWING，H_DOWNSWING_DTL 变化零影响（正面零影响）。"""
        sig = _downswing_sweep_signals()
        anchors = (5, 40, 60, 90)
        mid_a = segmenter.locate_intermediate(sig, anchors, view=CameraView.FACE_ON)
        monkeypatch.setattr(config, "H_DOWNSWING_DTL", 0.01)
        mid_b = segmenter.locate_intermediate(sig, anchors, view=CameraView.FACE_ON)
        assert mid_a[PhaseKey.DOWNSWING] == mid_b[PhaseKey.DOWNSWING]
        assert mid_a[PhaseKey.DOWNSWING][0] == 55

    def test_reanchor_impact_forwards_view(self, monkeypatch):
        """reanchor_impact(view=DTL) ⑤ 保持 DTL 阈值；不传 view 保持 face-on。"""
        monkeypatch.setattr(config, "H_DOWNSWING_DTL", 0.18)
        frames = _frames_for_reanchor()
        sig = _downswing_sweep_signals()
        # 用 face-on ⑤=55 组 8 事件（与 locate_intermediate 直调结果一致）
        indices = [5, 9, 10, 40, 55, 60, 70, 90]
        estimated = [False] * 8
        events = segmenter._assemble(frames, indices, estimated)
        new_idx = 61  # 校正把 impact 从 60 移到 61（真实移动，触发重建）

        rebuilt_face = segmenter.reanchor_impact(
            frames, sig, events, new_idx, view=CameraView.FACE_ON
        )
        rebuilt_dtl = segmenter.reanchor_impact(
            frames, sig, events, new_idx, view=CameraView.DOWN_THE_LINE
        )
        assert rebuilt_face is not None and rebuilt_dtl is not None
        ds_face = next(e for e in rebuilt_face if e.key is PhaseKey.DOWNSWING)
        ds_dtl = next(e for e in rebuilt_dtl if e.key is PhaseKey.DOWNSWING)
        assert ds_face.array_index == 55
        assert ds_dtl.array_index == 59
        # 重建后 8 事件仍严格递增
        idxs = [e.array_index for e in rebuilt_dtl]
        assert all(b > a for a, b in zip(idxs, idxs[1:]))


# ---------------------------------------------------------------------------
# ⑥击球机位感知（2026-08 改造：face-on 穿越点±速度峰；DTL 直接用穿越点）
# ---------------------------------------------------------------------------


class TestViewAwareImpact:
    """⑥ 击球机位感知：face-on 保持历史行为；DTL 穿越成功直接用穿越点；兜底共用。

    构造信号：n=100、i_top=40、i_addr=5（``h_addr=h[5]=0.02``），
    下杆窗口 ``[41, hi)``（``hi=86``）。穿越阈值
    ``tol = h_addr + IMPACT_Y_TOL = 0.37``：

    - ``cross=True``：下杆期 ``h`` 从 2.0 线性降到 0.2 → 首次穿越 ``h<=0.37``
      在 i=59；速度峰放 i=61（face-on 半窗 ``[57,62)`` 内取到 61，DTL 直取 59）。
    - ``cross=False``：窗口内 ``h`` 恒 2.0（> tol）→ 永不穿越 → 两机位共用
      速度峰兜底（i=61, estimated=True）。
    """

    @staticmethod
    def _impact_signals(cross: bool = True) -> SwingSignals:
        n = 100
        h = np.full(n, 2.0, dtype=np.float64)
        h[:41] = 0.02
        if cross:
            for i in range(41, 61):
                h[i] = 2.0 - (i - 41) * (1.8 / 19.0)
            h[61:] = 0.2
        speed = np.zeros(n, dtype=np.float64)
        speed[61] = 10.0
        return SwingSignals(
            n=n,
            fps=30.0,
            dt=1.0 / 30.0,
            S=1.0,
            wrist_x=np.zeros(n, dtype=np.float64),
            wrist_y=-h,
            shoulder_mid_y=np.full(n, 0.5, dtype=np.float64),
            hip_mid_y=np.zeros(n, dtype=np.float64),
            h=h,
            speed=speed,
        )

    def test_face_on_default_uses_speed_peak_near_cross(self):
        """face-on（含不传 view）：穿越点 ± 速度峰 → 取窗口内速度峰 61。"""
        sig = self._impact_signals(cross=True)
        i_default, est_default = segmenter.locate_impact(sig, 40, 5)
        i_face, est_face = segmenter.locate_impact(
            sig, 40, 5, view=CameraView.FACE_ON
        )
        assert i_default == i_face == 61  # ≠ 穿越点 59
        assert est_default is False and est_face is False

    def test_dtl_uses_cross_point_directly(self):
        """view=DTL：穿越成功后 i_impact == i_cross（不用速度峰偏移）。"""
        sig = self._impact_signals(cross=True)
        i_impact, est = segmenter.locate_impact(
            sig, 40, 5, view=CameraView.DOWN_THE_LINE
        )
        assert i_impact == 59  # 穿越点（≠ 速度峰 61）
        assert est is False

    def test_dtl_cross_failure_falls_back_to_speed_peak(self):
        """view=DTL 穿越失败：走速度峰兜底（estimated=True），与 face-on 相同。"""
        sig = self._impact_signals(cross=False)
        i_dtl, est_dtl = segmenter.locate_impact(
            sig, 40, 5, view=CameraView.DOWN_THE_LINE
        )
        i_face, est_face = segmenter.locate_impact(sig, 40, 5)
        assert i_dtl == i_face == 61
        assert est_dtl is True and est_face is True

    def test_segment_swing_forwards_view_to_impact(self, monkeypatch, swing_frames):
        """segment_swing 把 view 传给 locate_impact（DTL 分支可达）。"""
        seen: dict = {}
        real = segmenter.locate_impact

        def spy(sig, i_top, i_addr, view=CameraView.FACE_ON):
            seen["view"] = view
            return real(sig, i_top, i_addr, view)

        monkeypatch.setattr(segmenter, "locate_impact", spy)
        events = segmenter.segment_swing(
            swing_frames, FPS, view=CameraView.DOWN_THE_LINE
        )
        assert seen.get("view") is CameraView.DOWN_THE_LINE
        assert len(events) == 8
