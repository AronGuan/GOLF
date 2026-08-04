"""``app.reference`` + ``app.metrics`` 单测（架构文档 §8）。

覆盖：参考表完整性、三态判定边界、指标数值卫生、符号约定（§10.3）。
"""

from __future__ import annotations

import math

import pytest

from app import config, geometry, metrics, reference, segmenter
from app.schemas import PHASE_ORDER, MetricStatus, PhaseKey

from conftest import FPS, make_swing_frames


# ---------------------------------------------------------------------------
# 参考表
# ---------------------------------------------------------------------------


class TestReferenceTable:
    """METRIC_SPECS 静态校验（架构 §8.3：8 阶段 × 4 指标 + 3 项全程）。"""

    def test_all_eight_phases_present(self):
        assert set(reference.METRIC_SPECS) == set(PHASE_ORDER)

    @pytest.mark.parametrize("phase", list(PHASE_ORDER))
    def test_each_phase_has_four_metrics(self, phase):
        specs = reference.METRIC_SPECS[phase]
        assert len(specs) == 4, f"{phase} 指标数不是 4"
        keys = [s.key for s in specs]
        assert len(set(keys)) == 4, f"{phase} 指标 key 重复: {keys}"

    def test_global_specs_count(self):
        assert len(reference.GLOBAL_SPECS) == 3
        assert [s.key for s in reference.GLOBAL_SPECS] == [
            "tempo_ratio", "swing_duration", "max_head_drift_pct"
        ]

    def test_ref_ranges_are_sane(self):
        for phase, specs in reference.METRIC_SPECS.items():
            for spec in specs:
                assert spec.ref_min <= spec.ref_max, f"{phase}/{spec.key} 区间倒挂"
                assert spec.name, f"{phase}/{spec.key} 缺中文名"
                assert spec.ref_mid == pytest.approx((spec.ref_min + spec.ref_max) / 2)
        for spec in reference.GLOBAL_SPECS:
            assert spec.ref_min <= spec.ref_max

    def test_every_key_has_implementation(self):
        """参考表里出现的每个 key 都必须在 METRIC_FUNCS 中有实现。"""
        missing = [k for k in reference.all_metric_keys() if k not in metrics.METRIC_FUNCS]
        assert missing == [], f"缺少实现: {missing}"

    def test_no_orphan_implementation(self):
        """反向：METRIC_FUNCS 里不应有参考表未使用的孤儿实现。"""
        orphans = [k for k in metrics.METRIC_FUNCS if k not in reference.all_metric_keys()]
        assert orphans == [], f"孤儿实现: {orphans}"

    def test_all_metric_keys_deduped(self):
        keys = reference.all_metric_keys()
        assert len(keys) == len(set(keys))


class TestJudge:
    """三态判定边界（架构 §8.4）。"""

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
        """shoulder_square 的参考下界为负（-5 ~ 12）。"""
        assert reference.judge(-6.0, -5.0, 12.0) is MetricStatus.LOW
        assert reference.judge(-5.0, -5.0, 12.0) is MetricStatus.NORMAL
        assert reference.judge(0.0, -5.0, 12.0) is MetricStatus.NORMAL


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ctx(video_meta):
    """基于合成挥杆序列构建的 MetricContext。"""
    frames = make_swing_frames()
    signals = segmenter.build_signals(frames, FPS)
    events = segmenter.segment_swing(frames, FPS, sig=signals)
    return metrics.build_context(frames, events, signals, video_meta)


class TestMetricContext:
    """上下文装配。"""

    def test_scales_positive(self, ctx):
        assert ctx.S == pytest.approx(0.40, abs=0.05), "world 肩宽应约 0.40m"
        assert ctx.S_px > 0
        # 图像肩宽 0.20 * 视频宽 480 = 96px
        assert ctx.S_px == pytest.approx(96.0, rel=0.15)

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
    """8 个阶段的指标数值卫生。"""

    @pytest.mark.parametrize("phase", list(PHASE_ORDER))
    def test_metrics_complete_and_finite(self, ctx, phase):
        ctx.phase = phase
        items = metrics.compute_phase_metrics(ctx)
        specs = reference.METRIC_SPECS[phase]

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
            if spec.unit == reference.UNIT_DEG:
                assert -180.0 <= item.value <= 180.0, f"{phase}/{item.key} 角度越界"

    @pytest.mark.parametrize("phase", list(PHASE_ORDER))
    def test_status_consistent_with_judge(self, ctx, phase):
        ctx.phase = phase
        for item in metrics.compute_phase_metrics(ctx):
            assert item.status is reference.judge(item.value, item.ref_min, item.ref_max)

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


class TestGlobalMetrics:
    """3 项全程指标。"""

    def test_structure(self, ctx):
        gm = metrics.compute_global_metrics(ctx)
        assert len(gm.metrics) == 3
        by_key = {m.key: m.value for m in gm.metrics}
        assert gm.tempo_ratio == by_key["tempo_ratio"]
        assert gm.swing_duration == by_key["swing_duration"]
        assert gm.max_head_drift_pct == by_key["max_head_drift_pct"]
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
        """收杆：hip_to_target / shoulder_open = −turn，向目标打开为正。"""
        ctx.phase = PhaseKey.FINISH
        by_key = {m.key: m.value for m in metrics.compute_phase_metrics(ctx)}
        assert by_key["hip_to_target"] > 0, "收杆髋部未朝向目标，符号可能反了"
        assert by_key["shoulder_open"] > 0, "收杆肩部未打开，符号可能反了"

    def test_pelvis_shift_positive_toward_target(self, ctx):
        """合成序列骨盆整体向 +x（目标方向）移动，收杆位移应为正。"""
        ctx.phase = PhaseKey.FINISH
        value = {m.key: m.value for m in metrics.compute_phase_metrics(ctx)}["pelvis_shift_pct"]
        assert value > 0, f"骨盆位移为负({value})，TARGET_DIR_X 需要重新校准"

    def test_spine_forward_tilt_reasonable_at_address(self, ctx):
        """合成站位：atan2(0.38, 0.55) ≈ 34.6°。"""
        ctx.phase = PhaseKey.ADDRESS
        value = {m.key: m.value for m in metrics.compute_phase_metrics(ctx)}["spine_forward_tilt"]
        assert value == pytest.approx(34.6, abs=3.0)

    def test_stance_width_ratio_matches_geometry(self, ctx):
        """合成双踝间距 0.44m / world 肩宽 0.40m = 1.1。"""
        ctx.phase = PhaseKey.ADDRESS
        value = {m.key: m.value for m in metrics.compute_phase_metrics(ctx)}["stance_width_ratio"]
        assert value == pytest.approx(1.1, abs=0.05)


class TestSanitize:
    """数值卫生（架构 §8.4）。"""

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

    def test_metric_exception_does_not_break_report(self, ctx, monkeypatch):
        """单个指标抛异常时应兜底为参考中值，而不是整份报告失败。"""
        def _boom(_ctx):
            raise RuntimeError("boom")

        monkeypatch.setitem(metrics.METRIC_FUNCS, "knee_flex", _boom)
        ctx.cache.clear()
        ctx.phase = PhaseKey.ADDRESS
        items = {m.key: m for m in metrics.compute_phase_metrics(ctx)}
        assert items["knee_flex"].value == pytest.approx(166.0)  # (160+172)/2
        ctx.cache.clear()


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
