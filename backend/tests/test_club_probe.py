"""ClubProbe（方案 A 球杆观测器）单元测试。

重点覆盖**降级路径**：这是本模块的核心设计约束——任何异常都不能中断
分析主链路，必须回退到 L1 代理指标。

- 权重缺失 / onnxruntime 未装 / 推理异常 -> 全阶段 unavailable
- 机位门控（face-on 不跑）
- 总开关关闭
- 质量三闸门（min_kp / 骨架长度 / 基线长度）
- 预算守卫
- 配置里的非法阶段名容错
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.ai.club_probe import ClubObservation, ClubProbe  # noqa: E402
from app.schemas import CameraView, PhaseKey, SwingEvent  # noqa: E402


class _FakeDetector:
    """可控的假检测器：预置 (bbox, 关键点) 或抛异常。"""

    def __init__(self, result=None, raise_on_detect=False, keypoints=None):
        self.result = result or []
        self.raise_on_detect = raise_on_detect
        self.keypoints = keypoints or []
        self.is_det_ready = True

    def detect_full(self, image, score_thr=None, kp_thr=None):
        if self.raise_on_detect:
            raise RuntimeError("boom")
        return self.result


def _events() -> list:
    return [
        SwingEvent(
            key=k, index=i, frame_index=i * 10, timestamp=i * 0.33,
            array_index=i * 10, estimated=False,
        )
        for i, k in enumerate(PhaseKey)
    ]


def _frames() -> dict:
    return {i * 10: np.zeros((720, 1280, 3), np.uint8) for i in range(8)}


def _obs(available: bool, **kw) -> ClubObservation:
    return ClubObservation(available=available, **kw)


class TestDegradation:
    """降级路径：任何失败都不得外抛。"""

    def test_detector_not_ready_returns_all_unavailable(self, monkeypatch):
        det = _FakeDetector()
        det.is_det_ready = False
        probe = ClubProbe(detector=det)
        result = probe.observe(_frames(), _events(), CameraView.DOWN_THE_LINE)

        assert set(result) == set(PhaseKey)          # 键集合恒为全部 8 阶段
        assert all(not o.available for o in result.values())
        assert all(not o.accepted for o in result.values())

    def test_detect_exception_returns_all_unavailable(self):
        det = _FakeDetector(raise_on_detect=True)
        probe = ClubProbe(detector=det)
        result = probe.observe(_frames(), _events(), CameraView.DOWN_THE_LINE)

        assert all(not o.available for o in result.values())

    def test_empty_frames_returns_all_unavailable(self):
        probe = ClubProbe(detector=_FakeDetector())
        result = probe.observe({}, _events(), CameraView.DOWN_THE_LINE)
        assert all(not o.available for o in result.values())

    def test_missing_event_frame_skipped(self):
        """白名单阶段的帧不在解码集内 -> 跳过，不崩。"""
        det = _FakeDetector()
        probe = ClubProbe(detector=det)
        # 只给 finish 的帧（PhaseKey.FINISH 是第 8 个，frame_index=70）
        frames = {70: np.zeros((720, 1280, 3), np.uint8)}
        result = probe.observe(frames, _events(), CameraView.DOWN_THE_LINE)

        assert all(not o.available for o in result.values())


class TestGating:
    """门控：机位 / 总开关 / 阶段白名单。"""

    def test_face_on_skipped(self):
        det = _FakeDetector()
        probe = ClubProbe(detector=det)
        result = probe.observe(_frames(), _events(), CameraView.FACE_ON)
        assert all(not o.available for o in result.values())

    def test_disabled_by_config(self, monkeypatch):
        monkeypatch.setattr(config, "CLUB_ONNX_ENABLED", False)
        probe = ClubProbe(detector=_FakeDetector())
        result = probe.observe(_frames(), _events(), CameraView.DOWN_THE_LINE)
        assert all(not o.available for o in result.values())

    def test_empty_phase_whitelist(self, monkeypatch):
        monkeypatch.setattr(config, "CLUB_ONNX_PHASES", ())
        probe = ClubProbe(detector=_FakeDetector())
        assert probe._phases == ()
        result = probe.observe(_frames(), _events(), CameraView.DOWN_THE_LINE)
        assert all(not o.available for o in result.values())

    def test_invalid_phase_name_ignored(self, monkeypatch):
        """配置里写错阶段名 -> 跳过该条，不崩、不影响其他阶段。"""
        monkeypatch.setattr(config, "CLUB_ONNX_PHASES", ("top", "not_a_phase"))
        probe = ClubProbe()
        assert probe._phases == (PhaseKey.TOP,)


class TestQualityGate:
    """质量三闸门（2026-09-04 实测标定）。"""

    @staticmethod
    def _gate(**kw) -> tuple:
        return ClubProbe._quality_gate(
            kw.get("length", 200.0),
            kw.get("baseline", 120.0),
            kw.get("min_kp", 0.8),
        )

    def test_all_pass(self):
        ok, reason = self._gate()
        assert ok is True
        assert reason == ""

    def test_min_kp_gate(self):
        ok, reason = self._gate(min_kp=config.CLUB_ONNX_MIN_KP_SCORE - 0.01)
        assert ok is False
        assert "min_kp" in reason

    def test_skeleton_length_gate(self):
        """挡掉"杆头特写"式误检（实测误检帧骨架仅 39.8px）。"""
        ok, reason = self._gate(length=config.CLUB_ONNX_MIN_SKELETON_PX - 1.0)
        assert ok is False
        assert "skeleton_len" in reason

    def test_baseline_length_gate(self):
        """挡掉 5 点挤成一团的情况（实测 address 帧基线仅 3.8px -> 角度 90°）。"""
        ok, reason = self._gate(baseline=config.CLUB_ONNX_MIN_BASELINE_PX - 1.0)
        assert ok is False
        assert "baseline_len" in reason

    def test_boundary_values_pass(self):
        """边界值不应被拒绝（>= 判定）。"""
        ok, _ = self._gate(
            length=config.CLUB_ONNX_MIN_SKELETON_PX,
            baseline=config.CLUB_ONNX_MIN_BASELINE_PX,
            min_kp=config.CLUB_ONNX_MIN_KP_SCORE,
        )
        assert ok is True


class TestGeometry:

    def test_shaft_angle_horizontal(self):
        """水平杆身 -> 0°。"""
        kps = {
            "shaft": (100.0, 200.0, 0.9),
            "hosel": (300.0, 200.0, 0.9),
        }
        ang = ClubProbe._shaft_angle(kps)
        assert ang == pytest.approx(0.0, abs=0.6)

    def test_shaft_angle_45deg(self):
        kps = {
            "shaft": (100.0, 300.0, 0.9),
            "hosel": (200.0, 200.0, 0.9),   # 右上 45°
        }
        ang = ClubProbe._shaft_angle(kps)
        assert ang == pytest.approx(45.0, abs=0.6)

    def test_shaft_angle_missing_point(self):
        assert ClubProbe._shaft_angle({"shaft": (0.0, 0.0, 0.9)}) != ClubProbe._shaft_angle(
            {"shaft": (0.0, 0.0, 0.9)}
        )  # nan != nan
        import math

        assert math.isnan(ClubProbe._shaft_angle({"shaft": (0.0, 0.0, 0.9)}))

    def test_skeleton_length_full_chain(self):
        kps = {
            "shaft": (0.0, 0.0, 0.9),
            "hosel": (30.0, 0.0, 0.9),
            "heel": (60.0, 0.0, 0.9),
            "toe_down": (90.0, 0.0, 0.9),
            "toe_up": (120.0, 0.0, 0.9),
        }
        assert ClubProbe._skeleton_length(kps) == pytest.approx(120.0, abs=0.01)

    def test_skeleton_length_broken_chain(self):
        """缺一段 -> 0（而非部分长度），作为质量判据。"""
        kps = {"shaft": (0.0, 0.0, 0.9), "hosel": (30.0, 0.0, 0.9)}
        assert ClubProbe._skeleton_length(kps) == 0.0

    def test_baseline_length(self):
        kps = {"shaft": (0.0, 0.0, 0.9), "hosel": (30.0, 40.0, 0.9)}
        assert ClubProbe._baseline_length(kps) == pytest.approx(50.0, abs=0.01)


class TestBudget:

    def test_budget_exhausted_aborts(self, monkeypatch):
        """预算设为 0 -> 第一个阶段后即放弃（回退代理，不崩）。"""
        probe = ClubProbe(detector=_FakeDetector(), budget_sec=0.0)
        probe._t0 = 0.0  # 强制"已耗时极长"
        import time

        probe._t0 = time.time() - 999.0
        result = probe.observe(_frames(), _events(), CameraView.DOWN_THE_LINE)
        assert all(not o.available for o in result.values())


class TestObservationContract:

    def test_default_observation_is_unavailable(self):
        obs = ClubObservation()
        assert obs.available is False
        assert obs.accepted is False
        assert obs.kp("shaft") is None

    def test_kp_lookup(self):
        obs = ClubObservation(
            keypoints={"shaft": (1.0, 2.0, 0.9)}
        )
        assert obs.kp("shaft") == (1.0, 2.0, 0.9)
        assert obs.kp("hosel") is None
