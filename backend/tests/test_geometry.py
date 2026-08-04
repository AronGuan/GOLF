"""``app.geometry`` 纯函数单测（架构文档 §8.1 公式逐条核对）。

全部用手工构造的解析特例，不依赖 MediaPipe、不读视频。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from app import config, geometry


def v(*xyz: float) -> np.ndarray:
    """构造 3 维 float 向量。"""
    return np.array(xyz, dtype=np.float64)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


class TestBasics:
    """midpoint / clamp / shoulder_width。"""

    def test_midpoint(self):
        assert np.allclose(geometry.midpoint(v(0, 0, 0), v(2, 4, 6)), v(1, 2, 3))

    @pytest.mark.parametrize(
        ("value", "low", "high", "expected"),
        [(5.0, 0.0, 1.0, 1.0), (-5.0, 0.0, 1.0, 0.0), (0.5, 0.0, 1.0, 0.5),
         (1.0, 0.0, 1.0, 1.0), (0.0, 0.0, 1.0, 0.0)],
    )
    def test_clamp(self, value, low, high, expected):
        assert geometry.clamp(value, low, high) == pytest.approx(expected)

    def test_shoulder_width(self):
        world = np.zeros((geometry.NUM_LANDMARKS, 3), dtype=np.float64)
        world[geometry.L_SHOULDER] = v(0.2, 0.0, 0.0)
        world[geometry.R_SHOULDER] = v(-0.2, 0.0, 0.0)
        assert geometry.shoulder_width(world) == pytest.approx(0.4)

    def test_landmark_indices_match_blazepose(self):
        """BlazePose 33 点索引必须与架构文档 §10.3 一致。"""
        assert (geometry.L_SHOULDER, geometry.R_SHOULDER) == (11, 12)
        assert (geometry.L_ELBOW, geometry.R_ELBOW) == (13, 14)
        assert (geometry.L_WRIST, geometry.R_WRIST) == (15, 16)
        assert (geometry.L_HIP, geometry.R_HIP) == (23, 24)
        assert geometry.NUM_LANDMARKS == 33
        assert len(geometry.CORE_IDS) == 13


# ---------------------------------------------------------------------------
# angle_3p
# ---------------------------------------------------------------------------


class TestAngle3p:
    """三点夹角，返回 0~180。"""

    def test_right_angle(self):
        assert geometry.angle_3p(v(1, 0, 0), v(0, 0, 0), v(0, 1, 0)) == pytest.approx(90.0)

    def test_straight_line_is_180(self):
        """a-b-c 共线且 b 在中间 -> 180（完全伸直的手臂）。"""
        assert geometry.angle_3p(v(-1, 0, 0), v(0, 0, 0), v(1, 0, 0)) == pytest.approx(180.0)

    def test_folded_is_zero(self):
        """a 与 c 同向 -> 0（完全折叠）。"""
        assert geometry.angle_3p(v(1, 0, 0), v(0, 0, 0), v(2, 0, 0)) == pytest.approx(0.0, abs=1e-6)

    @pytest.mark.parametrize("expected", [30.0, 45.0, 60.0, 120.0, 150.0])
    def test_arbitrary_angle(self, expected):
        rad = math.radians(expected)
        a = v(1, 0, 0)
        c = v(math.cos(rad), math.sin(rad), 0.0)
        assert geometry.angle_3p(a, v(0, 0, 0), c) == pytest.approx(expected, abs=1e-6)

    def test_degenerate_returns_nan(self):
        """任一边长为 0 时返回 NaN（由上层 _sanitize 兜底）。"""
        assert math.isnan(geometry.angle_3p(v(0, 0, 0), v(0, 0, 0), v(1, 0, 0)))

    def test_always_within_0_180(self):
        rng = np.random.default_rng(20260804)
        for _ in range(200):
            a, b, c = (rng.normal(size=3) for _ in range(3))
            value = geometry.angle_3p(a, b, c)
            assert 0.0 - 1e-9 <= value <= 180.0 + 1e-9


# ---------------------------------------------------------------------------
# rotation_xz
# ---------------------------------------------------------------------------


class TestRotationXZ:
    """水平面(x-z)有符号转动角，返回 −180~180，乘 config.ROTATION_SIGN。"""

    def test_no_rotation(self):
        assert geometry.rotation_xz(v(1, 0, 0), v(1, 0, 0)) == pytest.approx(0.0)

    def test_quarter_turn_magnitude_and_sign(self):
        """ref=(1,0,0) -> now=(0,0,1) 原始转角 +90，输出须带 ROTATION_SIGN。"""
        value = geometry.rotation_xz(v(0, 0, 1), v(1, 0, 0))
        assert value == pytest.approx(90.0 * config.ROTATION_SIGN)

    def test_opposite_quarter_turn(self):
        value = geometry.rotation_xz(v(0, 0, -1), v(1, 0, 0))
        assert value == pytest.approx(-90.0 * config.ROTATION_SIGN)

    def test_ignores_y_component(self):
        """只取 (x,z) 分量，y 的变化不应影响结果。"""
        base = geometry.rotation_xz(v(0, 0, 1), v(1, 0, 0))
        assert geometry.rotation_xz(v(0, 99, 1), v(1, -99, 0)) == pytest.approx(base)

    def test_degenerate_returns_nan(self):
        assert math.isnan(geometry.rotation_xz(v(0, 1, 0), v(1, 0, 0)))
        assert math.isnan(geometry.rotation_xz(v(1, 0, 0), v(0, 1, 0)))

    def test_always_within_180(self):
        rng = np.random.default_rng(7)
        for _ in range(200):
            now = rng.normal(size=3)
            ref = rng.normal(size=3)
            value = geometry.rotation_xz(now, ref)
            assert -180.0 - 1e-9 <= value <= 180.0 + 1e-9

    def test_matches_reference_formula(self):
        """与架构文档 §8.1 公式逐项比对。"""
        rng = np.random.default_rng(11)
        for _ in range(50):
            now = rng.normal(size=3)
            ref = rng.normal(size=3)
            cross = ref[0] * now[2] - ref[2] * now[0]
            dot = ref[0] * now[0] + ref[2] * now[2]
            expected = math.degrees(math.atan2(cross, dot)) * config.ROTATION_SIGN
            assert geometry.rotation_xz(now, ref) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# 铅垂夹角
# ---------------------------------------------------------------------------


class TestTilt:
    """前倾 / 侧倾。"""

    def test_forward_tilt_upright_is_zero(self):
        """脊柱铅直（world y 向下为正，肩在髋之上 -> y 为负）。"""
        assert geometry.tilt_from_vertical_yz(v(0, -1, 0)) == pytest.approx(0.0)

    def test_forward_tilt_45(self):
        assert geometry.tilt_from_vertical_yz(v(0, -1, 1)) == pytest.approx(45.0)

    def test_forward_tilt_horizontal_is_90(self):
        assert geometry.tilt_from_vertical_yz(v(0, 0, 1)) == pytest.approx(90.0)

    def test_forward_tilt_always_0_90(self):
        rng = np.random.default_rng(3)
        for _ in range(200):
            value = geometry.tilt_from_vertical_yz(rng.normal(size=3))
            assert 0.0 <= value <= 90.0

    def test_forward_tilt_degenerate_nan(self):
        assert math.isnan(geometry.tilt_from_vertical_yz(v(1, 0, 0)))

    def test_lateral_tilt_upright_is_zero(self):
        assert geometry.tilt_from_vertical_xy(v(0, -1, 0)) == pytest.approx(0.0)

    def test_lateral_tilt_away_from_target_is_positive(self):
        """架构 §8.1：向**远离目标**为正。TARGET_DIR_X=+1 时目标在 +x。"""
        away = geometry.tilt_from_vertical_xy(v(-1 * config.TARGET_DIR_X, -1, 0))
        toward = geometry.tilt_from_vertical_xy(v(1 * config.TARGET_DIR_X, -1, 0))
        assert away == pytest.approx(45.0)
        assert toward == pytest.approx(-45.0)

    def test_lateral_tilt_degenerate_nan(self):
        assert math.isnan(geometry.tilt_from_vertical_xy(v(0, 0, 1)))


# ---------------------------------------------------------------------------
# line_tilt
# ---------------------------------------------------------------------------


class TestLineTilt:
    """两点连线相对水平线的倾角，右侧低于左侧为正（y 向下为正）。"""

    def test_horizontal_is_zero(self):
        assert geometry.line_tilt(v(0, 0, 0), v(1, 0, 0)) == pytest.approx(0.0)

    def test_right_lower_is_positive(self):
        assert geometry.line_tilt(v(0, 0, 0), v(1, 1, 0)) == pytest.approx(45.0)

    def test_right_higher_is_negative(self):
        assert geometry.line_tilt(v(0, 0, 0), v(1, -1, 0)) == pytest.approx(-45.0)

    def test_uses_abs_dx(self):
        """dx 取绝对值 -> 左右点互换不改变符号语义。"""
        assert geometry.line_tilt(v(0, 0, 0), v(-1, 1, 0)) == pytest.approx(45.0)

    def test_coincident_returns_nan(self):
        assert math.isnan(geometry.line_tilt(v(0, 0, 0), v(0, 0, 0)))


# ---------------------------------------------------------------------------
# 位移
# ---------------------------------------------------------------------------


class TestDisplacement:
    """肩宽归一化位移。"""

    def test_norm_disp_pct_3_4_5(self):
        assert geometry.norm_disp_pct(v(3, 4, 0), v(0, 0, 0), 10.0) == pytest.approx(50.0)

    def test_norm_disp_pct_is_unsigned(self):
        assert geometry.norm_disp_pct(v(-3, -4, 0), v(0, 0, 0), 10.0) == pytest.approx(50.0)

    def test_norm_disp_pct_respects_axes(self):
        """只取 axes 指定的分量。"""
        assert geometry.norm_disp_pct(v(3, 4, 99), v(0, 0, 0), 10.0, axes=(0,)) == pytest.approx(30.0)

    def test_norm_disp_pct_zero_scale_nan(self):
        assert math.isnan(geometry.norm_disp_pct(v(1, 1, 0), v(0, 0, 0), 0.0))
        assert math.isnan(geometry.norm_disp_pct(v(1, 1, 0), v(0, 0, 0), float("nan")))

    def test_signed_shift_toward_target_positive(self):
        """架构 §8.1：向目标为正。"""
        value = geometry.signed_shift_pct(v(2, 0, 0), v(0, 0, 0), 10.0)
        assert value == pytest.approx(20.0 * config.TARGET_DIR_X)

    def test_signed_shift_away_from_target_negative(self):
        value = geometry.signed_shift_pct(v(-2, 0, 0), v(0, 0, 0), 10.0)
        assert value == pytest.approx(-20.0 * config.TARGET_DIR_X)

    def test_signed_shift_ignores_y(self):
        assert geometry.signed_shift_pct(v(2, 99, 0), v(0, -99, 0), 10.0) == pytest.approx(
            20.0 * config.TARGET_DIR_X
        )

    def test_signed_shift_zero_scale_nan(self):
        assert math.isnan(geometry.signed_shift_pct(v(1, 0, 0), v(0, 0, 0), 0.0))
