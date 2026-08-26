"""M2：pipeline 接入 SwingNet（DTL 切 SwingNet，face-on 保持规则引擎）的单元测试。

用 ``monkeypatch`` 替换 :class:`app.ai.swingnet_detector.SwingNetDetector`，
**不真正加载 60MB 权重**，秒级、离线、可重复。覆盖：

- DTL 走 SwingNet：8 事件名映射正确、``frame_index`` / ``timestamp`` /
  ``array_index`` 对齐采样序列；
- face-on 走规则引擎：绝不调用 SwingNetDetector（逐字节不变）；
- 三重回退守卫：Impact 置信度低 / 时序乱 / 检测异常 → 回退规则引擎；
- ``SWINGNET_ENABLED=False`` 一键关停 → 回退规则引擎。
"""

from __future__ import annotations

import pytest

from app import config, pipeline, segmenter
from app.schemas import CameraView, PhaseKey, PHASE_ORDER, VideoMeta

import app.ai.swingnet_detector as swingnet_detector_module

from conftest import FPS, make_swing_frames

#: GolfDB 原始事件名（顺序即挥杆时序，与 pipeline._SWINGNET_PHASE_MAP 的 key 顺序一致）
EVENT_NAMES_ORDER = [
    "Address",
    "Toe-up",
    "Mid-backswing",
    "Top",
    "Mid-downswing",
    "Impact",
    "Mid-follow-through",
    "Finish",
]

#: 一套单调递增、落在合成视频帧号范围内的固定事件帧号（sample_step=1 时）
EVENT_FRAMES = {
    "Address": 10,
    "Toe-up": 25,
    "Mid-backswing": 35,
    "Top": 43,
    "Mid-downswing": 50,
    "Impact": 54,
    "Mid-follow-through": 60,
    "Finish": 75,
}


def _result(frame_indices, impact_conf: float = 0.9, other_conf: float = 0.9):
    """把 ``{事件名: 帧号}`` 拼成 SwingNetDetector.detect 的返回结构。"""
    return {
        name: {
            "frame_index": frame_indices[name],
            "confidence": impact_conf if name == "Impact" else other_conf,
        }
        for name in EVENT_NAMES_ORDER
    }


def _patch_swingnet(monkeypatch, result=None, exc=None):
    """替换 SwingNetDetector 为假实现，返回调用计数（``{"n": int}``）。"""
    calls = {"n": 0}

    class _FakeSwingNetDetector:
        def __init__(self, *args, **kwargs):  # noqa: D401 - 测试替身，无真实构造
            pass

        def detect(self, video_path):
            calls["n"] += 1
            if exc is not None:
                raise exc
            return result

    monkeypatch.setattr(swingnet_detector_module, "SwingNetDetector", _FakeSwingNetDetector)
    return calls


def _frame_indices(events):
    return [e.frame_index for e in events]


def _face_on_context(swing_frames, video_meta):
    aspect = video_meta.height / video_meta.width
    signals = segmenter.build_signals(swing_frames, video_meta.fps, aspect=aspect)
    return aspect, signals


def test_swingnet_phase_map_matches_phase_order():
    """事件名 -> PhaseKey 映射顺序必须与 PHASE_ORDER 完全一致。"""
    assert list(pipeline._SWINGNET_PHASE_MAP.keys()) == EVENT_NAMES_ORDER
    assert list(pipeline._SWINGNET_PHASE_MAP.values()) == list(PHASE_ORDER)


def test_detect_dtl_events_swingnet_maps_events(monkeypatch, swing_frames, video_meta):
    """DTL：SwingNet 固定事件帧号 → 8 个 SwingEvent 正确映射（estimated=False）。"""
    _patch_swingnet(monkeypatch, result=_result(EVENT_FRAMES))
    _, signals = _face_on_context(swing_frames, video_meta)

    events = pipeline._detect_dtl_events_swingnet(
        "dummy.mp4", video_meta, swing_frames, signals
    )

    assert events is not None
    assert len(events) == 8
    assert [e.key for e in events] == list(PHASE_ORDER)
    assert [e.index for e in events] == list(range(1, 9))

    by_key = {e.key: e for e in events}
    for key, name in zip(PHASE_ORDER, EVENT_NAMES_ORDER):
        event = by_key[key]
        assert event.frame_index == EVENT_FRAMES[name]
        assert event.estimated is False
        # sample_step=1：array_index == frame_index
        assert event.array_index == EVENT_FRAMES[name]
    assert by_key[PhaseKey.IMPACT].timestamp == pytest.approx(
        EVENT_FRAMES["Impact"] / video_meta.fps, abs=1e-3
    )


def test_detect_dtl_events_swingnet_array_index_with_sample_step(monkeypatch):
    """sample_step>1：frame_index（原视频帧号）floor 映射到采样序列下标。"""
    meta = VideoMeta(
        fps=FPS,
        duration=8.0,
        width=480,
        height=854,
        frame_count=240,
        sample_step=2,
    )
    frames = make_swing_frames(n=120, fps=FPS, step=2)  # frame_index = 0,2,...,238
    fmap = {
        "Address": 20,
        "Toe-up": 51,  # 非采样网格帧号：floor -> 25（frames[25].frame_index=50）
        "Mid-backswing": 70,
        "Top": 86,
        "Mid-downswing": 101,
        "Impact": 109,  # 非采样网格：floor -> 54
        "Mid-follow-through": 120,
        "Finish": 150,
    }
    _patch_swingnet(monkeypatch, result=_result(fmap))

    events = pipeline._detect_dtl_events_swingnet("dummy.mp4", meta, frames, None)

    assert events is not None
    by_key = {e.key: e for e in events}
    assert by_key[PhaseKey.ADDRESS].array_index == 20 // 2
    assert by_key[PhaseKey.TAKEAWAY].array_index == 51 // 2
    assert by_key[PhaseKey.IMPACT].array_index == 109 // 2
    assert by_key[PhaseKey.ADDRESS].frame_index == 20
    assert by_key[PhaseKey.ADDRESS].timestamp == pytest.approx(20 / FPS, abs=1e-3)


def test_segment_events_dtl_uses_swingnet(monkeypatch, swing_frames, video_meta):
    """DTL：``_segment_events`` 优先走 SwingNet，返回 used_swingnet=True。"""
    calls = _patch_swingnet(monkeypatch, result=_result(EVENT_FRAMES))
    aspect, signals = _face_on_context(swing_frames, video_meta)

    events, used_swingnet = pipeline._segment_events(
        "dummy.mp4", video_meta, swing_frames, signals, aspect, CameraView.DOWN_THE_LINE
    )

    assert used_swingnet is True
    assert calls["n"] == 1
    assert _frame_indices(events) == [EVENT_FRAMES[n] for n in EVENT_NAMES_ORDER]


def test_segment_events_face_on_uses_rule_engine_not_swingnet(
    monkeypatch, swing_frames, video_meta
):
    """face-on：绝不调用 SwingNetDetector，事件与规则引擎完全一致。"""
    calls = _patch_swingnet(monkeypatch, result=_result(EVENT_FRAMES))
    aspect, signals = _face_on_context(swing_frames, video_meta)

    events, used_swingnet = pipeline._segment_events(
        "dummy.mp4", video_meta, swing_frames, signals, aspect, CameraView.FACE_ON
    )

    assert used_swingnet is False
    assert calls["n"] == 0
    reference = segmenter.segment_swing(
        swing_frames, video_meta.fps, sig=signals, aspect=aspect, view=CameraView.FACE_ON
    )
    assert _frame_indices(events) == _frame_indices(reference)


def test_segment_events_dtl_fallback_on_low_impact_confidence(
    monkeypatch, swing_frames, video_meta
):
    """回退守卫①：Impact 置信度 < SWINGNET_MIN_IMPACT_CONF → 回退规则引擎。"""
    calls = _patch_swingnet(monkeypatch, result=_result(EVENT_FRAMES, impact_conf=0.1))
    aspect, signals = _face_on_context(swing_frames, video_meta)

    events, used_swingnet = pipeline._segment_events(
        "dummy.mp4", video_meta, swing_frames, signals, aspect, CameraView.DOWN_THE_LINE
    )

    assert used_swingnet is False
    assert calls["n"] == 1
    reference = segmenter.segment_swing(
        swing_frames, video_meta.fps, sig=None, aspect=aspect, view=CameraView.DOWN_THE_LINE
    )
    assert _frame_indices(events) == _frame_indices(reference)


def test_segment_events_dtl_fallback_on_non_monotonic(
    monkeypatch, swing_frames, video_meta
):
    """回退守卫②：8 事件 frame_index 不单调递增 → 回退规则引擎。"""
    fmap = dict(EVENT_FRAMES)
    fmap["Finish"] = 20  # 时序乱（Finish 跑到 Top 之前）
    calls = _patch_swingnet(monkeypatch, result=_result(fmap))
    aspect, signals = _face_on_context(swing_frames, video_meta)

    events, used_swingnet = pipeline._segment_events(
        "dummy.mp4", video_meta, swing_frames, signals, aspect, CameraView.DOWN_THE_LINE
    )

    assert used_swingnet is False
    assert calls["n"] == 1
    reference = segmenter.segment_swing(
        swing_frames, video_meta.fps, sig=None, aspect=aspect, view=CameraView.DOWN_THE_LINE
    )
    assert _frame_indices(events) == _frame_indices(reference)


def test_segment_events_dtl_fallback_on_exception(monkeypatch, swing_frames, video_meta):
    """回退守卫③：SwingNet 抛异常（权重缺失等）→ 回退规则引擎，主链路不崩。"""
    calls = _patch_swingnet(monkeypatch, exc=FileNotFoundError("权重不存在"))
    aspect, signals = _face_on_context(swing_frames, video_meta)

    events, used_swingnet = pipeline._segment_events(
        "dummy.mp4", video_meta, swing_frames, signals, aspect, CameraView.DOWN_THE_LINE
    )

    assert used_swingnet is False
    assert calls["n"] == 1
    assert len(events) == 8


def test_detect_dtl_events_swingnet_missing_event_falls_back(
    monkeypatch, swing_frames, video_meta
):
    """事件不全（缺 Finish）→ ``_detect_dtl_events_swingnet`` 返回 None。"""
    result = _result(EVENT_FRAMES)
    result.pop("Finish")
    _patch_swingnet(monkeypatch, result=result)

    events = pipeline._detect_dtl_events_swingnet("dummy.mp4", video_meta, swing_frames, None)

    assert events is None


def test_segment_events_dtl_swingnet_disabled(monkeypatch, swing_frames, video_meta):
    """SWINGNET_ENABLED=False：DTL 也回退规则引擎，且不调用 SwingNetDetector。"""
    calls = _patch_swingnet(monkeypatch, result=_result(EVENT_FRAMES))
    monkeypatch.setattr(config, "SWINGNET_ENABLED", False)
    aspect, signals = _face_on_context(swing_frames, video_meta)

    events, used_swingnet = pipeline._segment_events(
        "dummy.mp4", video_meta, swing_frames, signals, aspect, CameraView.DOWN_THE_LINE
    )

    assert used_swingnet is False
    assert calls["n"] == 0
    assert len(events) == 8
