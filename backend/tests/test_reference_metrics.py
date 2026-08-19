"""``app.reference`` + ``app.metrics`` 单测（架构 ARCHITECTURE.md §8 + v2 §3/§5）。

覆盖：参考表完整性（含 v2 key 对齐 / fn_key 映射）、五态判定边界（judge5）、
指标数值卫生、机位过滤、符号约定（§10.3）、allow_drop 剔除。
"""

from __future__ import annotations

import copy
import math

import numpy as np
import pytest

from app import config, geometry, metrics, reference, segmenter
from app.schemas import (
    CameraView,
    PHASE_ORDER,
    MetricStatus,
    PhaseKey,
)

from conftest import FPS, make_swing_frames

#: 每阶段 MetricSpec 总数（含两机位全部 spec，架构 §3.3 统计表）
PHASE_SPEC_COUNTS = {
    PhaseKey.ADDRESS: 4,
    PhaseKey.TAKEAWAY: 4,
    PhaseKey.BACKSWING: 4,
    PhaseKey.TOP: 5,
    PhaseKey.DOWNSWING: 4,
    PhaseKey.IMPACT: 4,
    PhaseKey.FOLLOW_THROUGH: 4,
    PhaseKey.FINISH: 4,
}

#: 机位过滤后各阶段指标数（架构 §3.3 统计表；DTL ⑤ 已随球杆检测下线，恒 0）
FACE_ON_COUNTS = {
    PhaseKey.ADDRESS: 3,
    PhaseKey.TAKEAWAY: 4,
    PhaseKey.BACKSWING: 4,
    PhaseKey.TOP: 4,
    PhaseKey.DOWNSWING: 4,
    PhaseKey.IMPACT: 3,
    PhaseKey.FOLLOW_THROUGH: 4,
    PhaseKey.FINISH: 4,
}
DTL_COUNTS = {
    PhaseKey.ADDRESS: 2,
    PhaseKey.TAKEAWAY: 2,
    PhaseKey.BACKSWING: 2,
    PhaseKey.TOP: 2,
    PhaseKey.DOWNSWING: 0,  # 球杆增强指标下线后恒 0
    PhaseKey.IMPACT: 1,
    PhaseKey.FOLLOW_THROUGH: 1,
    PhaseKey.FINISH: 1,
}


# ---------------------------------------------------------------------------
# 参考表
# ---------------------------------------------------------------------------


class TestReferenceTable:
    """METRIC_SPECS 静态校验（架构 §8.3 + v2 §3.3）。"""

    def test_all_eight_phases_present(self):
        assert set(reference.METRIC_SPECS) == set(PHASE_ORDER)

    @pytest.mark.parametrize("phase", list(PHASE_ORDER))
    def test_each_phase_spec_count(self, phase):
        specs = reference.METRIC_SPECS[phase]
        assert len(specs) == PHASE_SPEC_COUNTS[phase], f"{phase} 指标数不符"
        keys = [s.key for s in specs]
        assert len(set(keys)) == len(keys), f"{phase} 指标 key 重复: {keys}"

    def test_global_specs_count(self):
        assert len(reference.GLOBAL_SPECS) == 3
        assert [s.key for s in reference.GLOBAL_SPECS] == [
            "tempo_ratio", "swing_duration", "max_head_drift"
        ]

    def test_ref_ranges_are_sane(self):
        for phase, specs in reference.METRIC_SPECS.items():
            for spec in specs:
                assert spec.ref_min <= spec.ref_max, f"{phase}/{spec.key} 区间倒挂"
                assert spec.name, f"{phase}/{spec.key} 缺中文名"
                assert spec.ref_mid == pytest.approx((spec.ref_min + spec.ref_max) / 2)
        for spec in reference.GLOBAL_SPECS:
            assert spec.ref_min <= spec.ref_max

    def test_every_impl_key_has_implementation(self):
        """参考表里出现的每个 impl_key 都必须在 METRIC_FUNCS 中有实现。"""
        missing = [k for k in reference.all_metric_keys() if k not in metrics.METRIC_FUNCS]
        assert missing == [], f"缺少实现: {missing}"

    def test_no_orphan_implementation(self):
        """反向：METRIC_FUNCS 里不应有参考表未使用的孤儿实现。"""
        orphans = [k for k in metrics.METRIC_FUNCS if k not in reference.all_metric_keys()]
        assert orphans == [], f"孤儿实现: {orphans}"

    def test_all_metric_keys_deduped(self):
        keys = reference.all_metric_keys()
        assert len(keys) == len(set(keys))

    @pytest.mark.parametrize("phase", list(PHASE_ORDER))
    def test_every_spec_impl_key_registered(self, phase):
        for spec in reference.METRIC_SPECS[phase]:
            assert spec.impl_key in metrics.METRIC_FUNCS, (
                f"{phase}/{spec.key} impl_key={spec.impl_key} 未注册"
            )

    def test_follow_through_shoulder_turn_maps_to_open(self):
        """🚨 RISK-016 符号回归防线（数据层）：⑦ 的 shoulder_turn impl_key 必须是
        shoulder_open（= −肩转，正值），否则 `< 30` 会在每次挥杆上恒真。"""
        spec = next(
            s for s in reference.METRIC_SPECS[PhaseKey.FOLLOW_THROUGH]
            if s.key == "shoulder_turn"
        )
        assert spec.impl_key == "shoulder_open"

    def test_swing_plane_spec(self):
        spec = next(
            s for s in reference.METRIC_SPECS[PhaseKey.TOP] if s.key == "swing_plane"
        )
        assert spec.views == frozenset({CameraView.DOWN_THE_LINE})
        assert spec.allow_drop is True
        assert (spec.ref_min, spec.ref_max) == (55.0, 65.0)


# ---------------------------------------------------------------------------
# 三态 / 五态判定
# ---------------------------------------------------------------------------


class TestJudge:
    """三态判定边界（旧语义薄封装，critical=False）。"""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (9.99, MetricStatus.LOW),
            (10.0, MetricStatus.NORMAL),   # 下边界闭区间
            (15.0, MetricStatus.NORMAL),
            (20.0, MetricStatus.NORMAL),   # 上边界闭区间
            (20.01, MetricStatus.HIGH),
            (-999.0, MetricStatus.LOW),
            (999.0, MetricStatus.HIGH),
        ],
    )
    def test_boundaries(self, value, expected):
        assert reference.judge(value, 10.0, 20.0) is expected

    def test_negative_range(self):
        """shoulder_squareness 的参考下界为负（-5 ~ 12）。"""
        assert reference.judge(-6.0, -5.0, 12.0) is MetricStatus.LOW
        assert reference.judge(-5.0, -5.0, 12.0) is MetricStatus.NORMAL
        assert reference.judge(0.0, -5.0, 12.0) is MetricStatus.NORMAL


class TestJudge5:
    """五态判定（架构 §3.5 —— 区间宽度倍数，不用乘法规则）。"""

    def test_normal_within_range(self):
        assert reference.judge5(15.0, 10.0, 20.0) is MetricStatus.NORMAL

    def test_low_and_high(self):
        assert reference.judge5(9.9, 10.0, 20.0) is MetricStatus.LOW
        assert reference.judge5(20.1, 10.0, 20.0) is MetricStatus.HIGH

    def test_critical_band_one_span(self):
        """span=10, pad=10：critical_low < 0；critical_high > 30。"""
        assert reference.judge5(-0.1, 10.0, 20.0) is MetricStatus.CRITICAL_LOW
        assert reference.judge5(0.0, 10.0, 20.0) is MetricStatus.LOW
        assert reference.judge5(30.1, 10.0, 20.0) is MetricStatus.CRITICAL_HIGH
        assert reference.judge5(30.0, 10.0, 20.0) is MetricStatus.HIGH

    def test_critical_disabled(self):
        assert reference.judge5(-0.1, 10.0, 20.0, critical=False) is MetricStatus.LOW
        assert reference.judge5(30.1, 10.0, 20.0, critical=False) is MetricStatus.HIGH

    def test_shoulder_squareness_negative_range_regression(self):
        """乘法规则回归（架构 §3.5 决策理由 #2）：ref_min=-5, value=-4 落在正常区间
        内，绝不能判 critical_low（乘法规则 `-5×0.7=-3.5 > -5` 会误判）。"""
        assert reference.judge5(-4.0, -5.0, 12.0) is MetricStatus.NORMAL
        # 真正的重度偏低：value < -5 - 17 = -22
        assert reference.judge5(-23.0, -5.0, 12.0) is MetricStatus.CRITICAL_LOW

    def test_zero_ref_min_critical_low_unreachable(self):
        """乘法规则缺陷 #1：ref_min=0 时 critical_low 需 value < -span（= -8），
        而 head_drift 等非负指标物理上不可达——宽度倍数规则无需特判。"""
        assert reference.judge5(-8.1, 0.0, 8.0) is MetricStatus.CRITICAL_LOW
        assert reference.judge5(-1.0, 0.0, 8.0) is MetricStatus.LOW
        assert reference.judge5(0.0, 0.0, 8.0) is MetricStatus.NORMAL


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ctx(video_meta):
    """基于合成挥杆序列构建的 face-on MetricContext。"""
    frames = make_swing_frames()
    signals = segmenter.build_signals(frames, FPS)
    events = segmenter.segment_swing(frames, FPS, sig=signals)
    return metrics.build_context(
        frames, events, signals, video_meta, view=CameraView.FACE_ON
    )


@pytest.fixture(scope="module")
def ctx_dtl(video_meta):
    """同一合成序列的 DTL MetricContext（用于机位分派 / 过滤测试）。"""
    frames = make_swing_frames()
    signals = segmenter.build_signals(frames, FPS)
    events = segmenter.segment_swing(frames, FPS, sig=signals)
    return metrics.build_context(
        frames, events, signals, video_meta, view=CameraView.DOWN_THE_LINE
    )


class TestMetricContext:
    """上下文装配。"""

    def test_scales_positive(self, ctx):
        assert ctx.S == pytest.approx(0.40, abs=0.05), "world 肩宽应约 0.40m"
        assert ctx.S_px > 0
        # 图像肩宽 0.20 * 视频宽 480 = 96px
        assert ctx.S_px == pytest.approx(96.0, rel=0.15)

    def test_face_on_scale_px_is_shoulder_width(self, ctx):
        """face-on：scale_px = 图像肩宽。"""
        assert ctx.scale_px == pytest.approx(ctx.S_px)

    def test_dtl_scale_px_is_height_times_ratio(self, ctx_dtl):
        """DTL：scale_px = 图像身高 × SHOULDER_TO_HEIGHT_RATIO（双肩投影被压缩）。"""
        assert ctx_dtl.body_h_px > 0
        assert ctx_dtl.scale_px == pytest.approx(
            ctx_dtl.body_h_px * config.SHOULDER_TO_HEIGHT_RATIO
        )
        assert ctx_dtl.scale_px != pytest.approx(ctx_dtl.S_px)

    def test_frame_lookup(self, ctx):
        for key in PHASE_ORDER:
            event = ctx.event_of(key)
            frame = ctx.frame_of(key)
            assert frame is ctx.frames[event.array_index]

    def test_unknown_phase_raises(self, ctx):
        class _Fake:
            pass

        with pytest.raises(KeyError):
            ctx.event_of(_Fake())


class TestPhaseMetrics:
    """8 个阶段的指标数值卫生（机位过滤后）。"""

    @pytest.mark.parametrize("phase", list(PHASE_ORDER))
    def test_metrics_complete_and_finite(self, ctx, phase):
        ctx.phase = phase
        items = metrics.compute_phase_metrics(ctx)
        specs = [s for s in reference.METRIC_SPECS[phase] if s.supports(ctx.view)]

        assert len(items) == len(specs)
        assert [m.key for m in items] == [s.key for s in specs]

        for item, spec in zip(items, specs):
            assert item.value is not None
            assert isinstance(item.value, float)
            assert not math.isnan(item.value), f"{phase}/{item.key} 为 NaN"
            assert not math.isinf(item.value), f"{phase}/{item.key} 为 inf"
            assert item.name == spec.name
            assert item.unit == spec.unit
            assert item.ref_min == spec.ref_min
            assert item.ref_max == spec.ref_max
            assert item.description == spec.description
            if spec.unit == reference.UNIT_DEG:
                assert -180.0 <= item.value <= 180.0, f"{phase}/{item.key} 角度越界"

    @pytest.mark.parametrize("phase", list(PHASE_ORDER))
    def test_status_consistent_with_judge5(self, ctx, phase):
        ctx.phase = phase
        specs = [s for s in reference.METRIC_SPECS[phase] if s.supports(ctx.view)]
        for item, spec in zip(metrics.compute_phase_metrics(ctx), specs):
            assert item.status is reference.judge5(
                item.value, item.ref_min, item.ref_max, spec.critical
            )

    def test_values_rounded_to_one_decimal(self, ctx):
        for phase in PHASE_ORDER:
            ctx.phase = phase
            for item in metrics.compute_phase_metrics(ctx):
                assert item.value == pytest.approx(round(item.value, 1))

    def test_cache_returns_same_value(self, ctx):
        ctx.phase = PhaseKey.TOP
        first = {m.key: m.value for m in metrics.compute_phase_metrics(ctx)}
        second = {m.key: m.value for m in metrics.compute_phase_metrics(ctx)}
        assert first == second


class TestViewFilter:
    """机位过滤（AC-09 / AC-10，架构 §3.3 统计表 + 决策 2）。"""

    @pytest.mark.parametrize("phase", list(PHASE_ORDER))
    def test_face_on_counts(self, ctx, phase):
        ctx.phase = phase
        items = metrics.compute_phase_metrics(ctx)
        assert len(items) == FACE_ON_COUNTS[phase], f"face-on {phase} 指标数不符"

    @pytest.mark.parametrize("phase", list(PHASE_ORDER))
    def test_dtl_counts(self, ctx_dtl, phase):
        ctx_dtl.phase = phase
        items = metrics.compute_phase_metrics(ctx_dtl)
        expected = DTL_COUNTS[phase]
        if isinstance(expected, tuple):
            assert len(items) in expected, f"dtl {phase} 指标数 {len(items)} 不在 {expected}"
        else:
            assert len(items) == expected, f"dtl {phase} 指标数不符"

    def test_face_on_has_no_dtl_only_metrics(self, ctx):
        for phase in PHASE_ORDER:
            ctx.phase = phase
            keys = {m.key for m in metrics.compute_phase_metrics(ctx)}
            assert "swing_plane" not in keys, "face-on 不应出现 swing_plane (AC-09)"
            assert "spine_tilt_fwd" not in keys, "face-on 不应出现 spine_tilt_fwd"
            assert "spine_tilt_change" not in keys, "face-on 不应出现 spine_tilt_change"

    def test_dtl_has_swing_plane_and_spine_tilt_change(self, ctx_dtl):
        """DTL 专属指标出现（AC-10）。"""
        ctx_dtl.phase = PhaseKey.TOP
        top_keys = {m.key for m in metrics.compute_phase_metrics(ctx_dtl)}
        assert "swing_plane" in top_keys, "DTL TOP 应出现 swing_plane (AC-10)"
        ctx_dtl.phase = PhaseKey.IMPACT
        impact_keys = {m.key for m in metrics.compute_phase_metrics(ctx_dtl)}
        assert "spine_tilt_change" in impact_keys, "DTL IMPACT 应出现 spine_tilt_change"

    def test_dtl_downswing_empty_after_club_offline(self, ctx_dtl):
        """决策 2（2026-08 更新）：⑤ 随球杆检测下线后，DTL 下杆恒 0 项。"""
        ctx_dtl.phase = PhaseKey.DOWNSWING
        items = metrics.compute_phase_metrics(ctx_dtl)
        assert items == []


class TestGlobalMetrics:
    """3 项全程指标。"""

    def test_structure(self, ctx):
        gm = metrics.compute_global_metrics(ctx)
        assert len(gm.metrics) == 3
        by_key = {m.key: m.value for m in gm.metrics}
        assert gm.tempo_ratio == by_key["tempo_ratio"]
        assert gm.swing_duration == by_key["swing_duration"]
        assert gm.max_head_drift_pct == by_key["max_head_drift"]
        for item in gm.metrics:
            assert math.isfinite(item.value)

    def test_swing_duration_matches_events(self, ctx):
        gm = metrics.compute_global_metrics(ctx)
        f_addr = ctx.event_of(PhaseKey.ADDRESS).frame_index
        f_finish = ctx.event_of(PhaseKey.FINISH).frame_index
        assert gm.swing_duration == pytest.approx(
            round((f_finish - f_addr) / FPS, 1), abs=0.11
        )
        assert gm.swing_duration > 0

    def test_tempo_ratio_positive(self, ctx):
        gm = metrics.compute_global_metrics(ctx)
        assert gm.tempo_ratio > 0

    def test_max_head_drift_non_negative(self, ctx):
        gm = metrics.compute_global_metrics(ctx)
        assert gm.max_head_drift_pct >= 0.0


class TestSignConventions:
    """架构 §10.3 符号约定 —— 校准结论必须在合成右手挥杆上成立。"""

    def test_shoulder_turn_positive_at_top(self, ctx):
        """顶点肩转应为 +70~+90（校准口径）。"""
        ctx.phase = PhaseKey.TOP
        value = {m.key: m.value for m in metrics.compute_phase_metrics(ctx)}["shoulder_turn"]
        assert value > 0, f"顶点肩转为负({value})，ROTATION_SIGN 需要重新校准"
        assert 60.0 <= value <= 95.0, f"顶点肩转 {value} 超出合理区间"

    def test_hip_turn_positive_at_top(self, ctx):
        ctx.phase = PhaseKey.TOP
        value = {m.key: m.value for m in metrics.compute_phase_metrics(ctx)}["hip_turn"]
        assert value > 0

    def test_x_factor_is_shoulder_minus_hip(self, ctx):
        ctx.phase = PhaseKey.TOP
        by_key = {m.key: m.value for m in metrics.compute_phase_metrics(ctx)}
        assert by_key["x_factor"] == pytest.approx(
            round(by_key["shoulder_turn"] - by_key["hip_turn"], 1), abs=0.2
        )

    def test_open_angles_are_negated_turn_at_finish(self, ctx):
        """收杆：hip_toward_target / shoulder_total_open = −turn，向目标打开为正。"""
        ctx.phase = PhaseKey.FINISH
        by_key = {m.key: m.value for m in metrics.compute_phase_metrics(ctx)}
        assert by_key["hip_toward_target"] > 0, "收杆髋部未朝向目标，符号可能反了"
        assert by_key["shoulder_total_open"] > 0, "收杆肩部未打开，符号可能反了"

    def test_follow_through_shoulder_turn_via_open_maps_to_open_angle(self, ctx):
        """⑦ shoulder_turn 走 shoulder_open 实现：值恒等于 −肩转（fn_key 映射）。

        ⚠️ 2026-08 ⑦ 判据为「h 局部最小点 + FOLLOWTHROUGH_RISE 上升阈值」
        （方案 B；合成挥杆下 ⑦ ≈ impact+8）后，合成挥杆在 ⑦ 处肩部尚未充分
        打开，开放角 ≈ +28°——物理真实（杆身刚略上扬肩还没转完），不代表
        映射失效。映射是否生效只看 ``shoulder_turn == -m_shoulder_turn``
        是否成立。
        """
        ctx.phase = PhaseKey.FOLLOW_THROUGH
        by_key = {m.key: m.value for m in metrics.compute_phase_metrics(ctx)}
        value = by_key["shoulder_turn"]
        raw_turn = metrics.m_shoulder_turn(ctx)
        assert value == pytest.approx(-raw_turn, abs=0.2), (
            f"⑦ shoulder_turn 未走 shoulder_open(-肩转)，fn_key 映射失效: "
            f"value={value} raw={raw_turn}"
        )

    def test_pelvis_shift_positive_toward_target(self, ctx):
        """合成序列骨盆整体向 +x（目标方向）移动，收杆位移应为正。"""
        ctx.phase = PhaseKey.FINISH
        value = {m.key: m.value for m in metrics.compute_phase_metrics(ctx)}["pelvis_shift"]
        assert value > 0, f"骨盆位移为负({value})，TARGET_DIR_X 需要重新校准"

    def test_spine_forward_tilt_reasonable_at_address(self, ctx):
        """face-on 投影面（y-z）：合成站位 atan2(0.38, 0.55) ≈ 34.6°。"""
        addr_frame = ctx.addr
        assert metrics._spine_forward_tilt_at(
            addr_frame, CameraView.FACE_ON
        ) == pytest.approx(34.6, abs=3.0)

    def test_spine_forward_tilt_dtl_uses_xy_plane(self, ctx):
        """DTL 投影面（x-y）：构造纯 x 方向前倾的脊柱向量，face-on 测不到、
        DTL 测得到（架构 §5.6 A7）。"""
        addr_frame = ctx.addr
        # 把脊柱向量改成纯 x 倾斜：(0.40, -0.55, 0)
        frame = addr_frame
        sh_mid = geometry.midpoint(
            frame.world[geometry.L_SHOULDER], frame.world[geometry.R_SHOULDER]
        )
        hip_mid = geometry.midpoint(
            frame.world[geometry.L_HIP], frame.world[geometry.R_HIP]
        )
        # 直接构造 spine_vec 的手工 frame：双肩中点 = 双髋中点 + (0.40, -0.55, 0)
        new_frame = copy.copy(frame)
        new_frame.world = frame.world.copy()
        for idx in (geometry.L_SHOULDER, geometry.R_SHOULDER):
            new_frame.world[idx] = new_frame.world[idx] - sh_mid + (hip_mid + np.array([0.40, -0.55, 0.0]))

        face_on = metrics._spine_forward_tilt_at(new_frame, CameraView.FACE_ON)
        dtl = metrics._spine_forward_tilt_at(new_frame, CameraView.DOWN_THE_LINE)
        assert math.isfinite(face_on) and abs(face_on) < 1e-6, "纯 x 前倾在 face-on 应≈0"
        assert dtl == pytest.approx(math.degrees(math.atan2(0.40, 0.55)), abs=2.0), (
            "纯 x 前倾在 DTL 应≈atan2(0.40, 0.55)"
        )

    def test_stance_width_ratio_matches_geometry(self, ctx):
        """合成双踝间距 0.44m / world 肩宽 0.40m = 1.1。"""
        ctx.phase = PhaseKey.ADDRESS
        value = {m.key: m.value for m in metrics.compute_phase_metrics(ctx)}["stance_width_ratio"]
        assert value == pytest.approx(1.1, abs=0.05)


class TestSwingPlane:
    """④ swing_plane（纯 MediaPipe，DTL 专属）。"""

    def test_synthetic_value_finite_and_acute(self, ctx_dtl):
        ctx_dtl.phase = PhaseKey.TOP
        items = {m.key: m for m in metrics.compute_phase_metrics(ctx_dtl)}
        assert "swing_plane" in items
        value = items["swing_plane"].value
        assert math.isfinite(value)
        assert 0.0 <= value <= 90.0
        assert not items["swing_plane"].estimated

    def test_visibility_guard_drops_metric(self, ctx_dtl):
        """左肩/左腕可见度 < 0.5 -> nan -> allow_drop 剔除（不造假绿值）。"""
        ctx_dtl.cache.clear()  # 清掉前序用例缓存的 swing_plane 值，强制重算
        ctx_dtl.phase = PhaseKey.TOP
        frame = ctx_dtl.frame_of(PhaseKey.TOP)
        frame.visibility = frame.visibility.copy()
        frame.visibility[geometry.L_WRIST] = 0.1
        items = metrics.compute_phase_metrics(ctx_dtl)
        assert all(m.key != "swing_plane" for m in items), "可见度不足应剔除 swing_plane"
        assert any("可见度不足" in w for w in ctx_dtl.warnings)
        ctx_dtl.cache.clear()


class TestSanitize:
    """数值卫生（架构 §8.4 + §5.5 allow_drop 豁免）。"""

    def test_nan_falls_back_to_ref_mid_with_warning(self, ctx):
        spec = reference.MetricSpec("t", "测试项", reference.UNIT_DEG, 10.0, 20.0)
        ctx.warnings.clear()
        assert metrics._sanitize(float("nan"), spec, ctx) == pytest.approx(15.0)
        assert any("测试项" in w for w in ctx.warnings)

    def test_inf_falls_back_to_ref_mid(self, ctx):
        spec = reference.MetricSpec("t", "测试项", reference.UNIT_DEG, 10.0, 20.0)
        assert metrics._sanitize(float("inf"), spec, ctx) == pytest.approx(15.0)
        assert metrics._sanitize(float("-inf"), spec, ctx) == pytest.approx(15.0)

    def test_none_falls_back_to_ref_mid(self, ctx):
        spec = reference.MetricSpec("t", "测试项", reference.UNIT_PCT, 0.0, 8.0)
        assert metrics._sanitize(None, spec, ctx) == pytest.approx(4.0)

    def test_angle_clamped_to_180(self, ctx):
        spec = reference.MetricSpec("t", "测试项", reference.UNIT_DEG, 10.0, 20.0)
        assert metrics._sanitize(999.0, spec, ctx) == pytest.approx(180.0)
        assert metrics._sanitize(-999.0, spec, ctx) == pytest.approx(-180.0)

    def test_non_angle_not_clamped(self, ctx):
        spec = reference.MetricSpec("t", "测试项", reference.UNIT_PCT, 0.0, 8.0)
        assert metrics._sanitize(999.0, spec, ctx) == pytest.approx(999.0)

    def test_rounded_to_one_decimal(self, ctx):
        spec = reference.MetricSpec("t", "测试项", reference.UNIT_PCT, 0.0, 8.0)
        assert metrics._sanitize(1.2345, spec, ctx) == pytest.approx(1.2)

    def test_allow_drop_nan_returns_none(self, ctx):
        spec = reference.MetricSpec(
            "t", "测试项", reference.UNIT_DEG, 10.0, 20.0, allow_drop=True
        )
        ctx.warnings.clear()
        assert metrics._sanitize(float("nan"), spec, ctx) is None
        assert any("跳过" in w for w in ctx.warnings)

    def test_allow_drop_valid_value_kept(self, ctx):
        spec = reference.MetricSpec(
            "t", "测试项", reference.UNIT_DEG, 10.0, 20.0, allow_drop=True
        )
        assert metrics._sanitize(15.4, spec, ctx) == pytest.approx(15.4)

    def test_metric_exception_does_not_break_report(self, ctx, monkeypatch):
        """单个指标抛异常时应兜底为参考中值，而不是整份报告失败。"""
        def _boom(_ctx):
            raise RuntimeError("boom")

        monkeypatch.setitem(metrics.METRIC_FUNCS, "knee_flex", _boom)
        ctx.cache.clear()
        ctx.phase = PhaseKey.ADDRESS
        items = {m.key: m for m in metrics.compute_phase_metrics(ctx)}
        assert items["knee_flexion"].value == pytest.approx(166.0)  # (160+172)/2
        ctx.cache.clear()

    def test_allow_drop_exception_returns_none(self, ctx_dtl, monkeypatch):
        """allow_drop 指标抛异常 -> 剔除而非填中值。"""
        def _boom(_ctx):
            raise RuntimeError("boom")

        monkeypatch.setitem(metrics.METRIC_FUNCS, "swing_plane", _boom)
        ctx_dtl.cache.clear()
        ctx_dtl.phase = PhaseKey.TOP
        items = metrics.compute_phase_metrics(ctx_dtl)
        assert all(m.key != "swing_plane" for m in items)
        ctx_dtl.cache.clear()


class TestImageShoulderWidth:
    """位移标尺（metrics.image_shoulder_width_px）。"""

    def test_empty_frames_fallback(self, video_meta):
        assert metrics.image_shoulder_width_px([], video_meta) == pytest.approx(1.0)

    def test_uses_address_frame(self, video_meta):
        frames = make_swing_frames()
        value = metrics.image_shoulder_width_px(frames, video_meta, ref_index=0)
        assert value == pytest.approx(0.20 * video_meta.width, rel=0.1)

    def test_compressed_address_falls_back(self, video_meta):
        """Address 帧肩线被压缩（非正面机位）时退回 90 分位。"""
        frames = make_swing_frames()
        frames[0].norm = frames[0].norm.copy()
        frames[0].norm[geometry.L_SHOULDER, :2] = frames[0].norm[geometry.R_SHOULDER, :2]
        value = metrics.image_shoulder_width_px(frames, video_meta, ref_index=0)
        assert value > 0.5 * 0.20 * video_meta.width
