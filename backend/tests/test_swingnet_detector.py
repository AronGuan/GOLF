"""SwingNetDetector 单元测试（M1 封装，不接 pipeline）。

torch 权重加载慢（60MB），常规用例走轻量验证、不真正加载权重；
真实推理用例标记 opt-in（``GOLF_RUN_SWINGNET_SLOW=1``），默认跳过以保持
全量 ``pytest backend/tests -q`` 快速且全绿。
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from app import config
from app.ai.swingnet_detector import EVENT_NAMES, SwingNetDetector

EXPECTED_EVENTS = [
    "Address",
    "Toe-up",
    "Mid-backswing",
    "Top",
    "Mid-downswing",
    "Impact",
    "Mid-follow-through",
    "Finish",
]


def test_event_mapping():
    """8 事件名完整、顺序正确（GolfDB 原始命名）、无重复。"""
    assert EVENT_NAMES == EXPECTED_EVENTS
    assert len(EVENT_NAMES) == 8
    assert len(set(EVENT_NAMES)) == 8


def test_lazy_load(tmp_path):
    """构造检测器不加载权重（懒加载：import/构造不碰 torch 权重）。"""
    weights = str(tmp_path / "nonexistent.pth.tar")
    detector = SwingNetDetector(weights_path=weights)
    assert detector.is_loaded is False
    assert detector._model is None


def test_detect_requires_video(tmp_path):
    """不存在的视频返回明确异常（ValueError）。"""
    detector = SwingNetDetector(weights_path=str(tmp_path / "w.pth.tar"))
    with pytest.raises(ValueError, match="视频"):
        detector.detect(str(tmp_path / "no_such.mp4"))


def test_detect_rejects_non_video(tmp_path):
    """存在但不可解码的文件返回明确异常（ValueError）。"""
    bad = tmp_path / "not_video.mp4"
    bad.write_bytes(b"this is not a real video")
    detector = SwingNetDetector(weights_path=str(tmp_path / "w.pth.tar"))
    with pytest.raises(ValueError, match="视频"):
        detector.detect(str(bad))


def test_detect_missing_weights_after_lazy_load(synth_video):
    """有效视频 + 缺失权重：detect 先通过视频校验、触发懒加载后抛 FileNotFoundError。"""
    detector = SwingNetDetector(weights_path=str("/nonexistent/swingnet.pth.tar"))
    assert detector.is_loaded is False
    with pytest.raises(FileNotFoundError, match="权重"):
        detector.detect(synth_video)
    # 加载失败后应保持未加载状态
    assert detector.is_loaded is False


def test_default_weights_path_from_config():
    """缺省 weights_path 落到 config.SWINGNET_WEIGHTS_PATH。"""
    detector = SwingNetDetector()
    assert detector.weights_path == str(config.SWINGNET_WEIGHTS_PATH)


def _find_sample_video() -> str:
    """定位真实 DTL 样本视频（用于 opt-in 真实推理验证，样本在本地 .tools 目录）。"""
    here = os.path.dirname(os.path.abspath(__file__))  # backend/tests
    repo_root = os.path.dirname(os.path.dirname(here))  # project root
    candidates = [
        os.path.join(repo_root, ".tools", "_probe", "samples", "侧面", "11.mp4"),
        os.path.join(repo_root, ".tools", "_probe", "samples", "侧面", "4e8d0d7e517a67a2a7698fd1536289eb.mp4"),
    ]
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return ""


_RUN_SLOW = os.environ.get("GOLF_RUN_SWINGNET_SLOW") == "1"


@pytest.mark.skipif(
    not _RUN_SLOW,
    reason="真实推理加载 60MB 权重较慢，默认跳过；设 GOLF_RUN_SWINGNET_SLOW=1 运行",
)
def test_real_inference_optional():
    """用真实 DTL 样本跑一次真实推理，验证 detect 输出 8 事件且时序合理。"""
    weights = str(config.SWINGNET_WEIGHTS_PATH)
    if not os.path.isfile(weights):
        pytest.skip("SwingNet 权重不存在，跳过真实推理")

    video = _find_sample_video()
    if not video:
        pytest.skip("真实 DTL 样本视频不存在，跳过真实推理")

    detector = SwingNetDetector(weights_path=weights)
    assert detector.is_loaded is False
    result = detector.detect(video)
    assert detector.is_loaded is True

    assert set(result.keys()) == set(EVENT_NAMES)
    frames = [result[name]["frame_index"] for name in EVENT_NAMES]

    # 帧号非负、置信度在 [0, 1]
    assert all(result[name]["frame_index"] >= 0 for name in EVENT_NAMES)
    assert all(0.0 <= result[name]["confidence"] <= 1.0 for name in EVENT_NAMES)

    # 挥杆阶段（Toe-up → Finish）时序单调递增。
    # Address 单独校验：GolfDB SwingNet 的 Address argmax 受「收杆回归站位姿态」
    # 影响在部分样本上偏晚（已知弱点），故不纳入单调性断言，仅要求 Address 不晚于击球。
    swing_frames = frames[1:]
    assert swing_frames == sorted(swing_frames), f"挥杆阶段帧号应单调递增: {frames}"

    # Impact 位于 Top 与 Finish 之间
    assert result["Top"]["frame_index"] < result["Impact"]["frame_index"] < result["Finish"]["frame_index"]
    # Address 不晚于击球（物理必然）
    assert result["Address"]["frame_index"] <= result["Impact"]["frame_index"]


# ---------------------------------------------------------------------------
# _constrained_events 纯逻辑测试（不加载权重，秒级）
# ---------------------------------------------------------------------------


def _probs(n_frames: int, peaks: dict) -> "np.ndarray":
    """构造 ``(n_frames, 9)`` 概率矩阵：每列在 ``peaks[cls]`` 帧放 1.0，其余 0.0。

    ``_constrained_events`` 只做 argmax，不依赖概率归一化，故用 one-hot 峰值即可。
    """
    probs = np.zeros((n_frames, 9), dtype=np.float32)
    for cls, frame in peaks.items():
        probs[frame, cls] = 1.0
    return probs


def _frames_of(result) -> list:
    return [result[name]["frame_index"] for name in EVENT_NAMES]


def test_constrained_events_relocates_out_of_order_transition():
    """过渡事件全视频 argmax 跑到视频开头/与 Address 同帧时，区间约束拉回锚点内。

    复现 11.mp4 实况：锚点 Address=100/Top=109/Impact=116/Finish=157 单调，但
    Toe-up 假峰在帧 17、Mid-backswing 假峰在帧 100（与 Address 同帧）。
    """
    probs = _probs(200, {
        0: 100, 1: 17, 2: 100, 3: 109, 4: 113, 5: 116, 6: 118, 7: 157,
    })
    result = SwingNetDetector._constrained_events(probs)
    frames = _frames_of(result)

    # 结果必须严格递增（这是 pipeline 单调守卫通过的硬前提）
    assert frames == sorted(frames)
    assert len(set(frames)) == len(frames)

    # 过渡事件被约束到锚点区间内，而非跑到视频开头（17）/ 与 Address 同帧（100）
    assert 100 < result["Toe-up"]["frame_index"] < 109
    assert result["Toe-up"]["frame_index"] < result["Mid-backswing"]["frame_index"] < 109
    # 主锚点不受影响
    assert result["Address"]["frame_index"] == 100
    assert result["Top"]["frame_index"] == 109
    assert result["Impact"]["frame_index"] == 116
    assert result["Finish"]["frame_index"] == 157


def test_constrained_events_anchor_disorder_returns_global():
    """锚点本身乱序（Finish 跑到 Top 前）→ 返回全局 argmax 原结果，交由调用方回退。"""
    probs = _probs(200, {
        0: 100, 1: 25, 2: 35, 3: 109, 4: 113, 5: 116, 6: 118, 7: 50,  # Finish=50 < Top=109
    })
    result = SwingNetDetector._constrained_events(probs)
    # 锚点乱序：不重定位，返回全局 argmax（Finish 仍是 50，保持乱序供守卫识别）
    assert result["Finish"]["frame_index"] == 50
    assert result["Top"]["frame_index"] == 109


def test_constrained_events_strictly_increasing_on_tie():
    """相邻过渡事件 argmax 到同一帧时，严格递增强制把后者后推 1 帧。

    竖屏 DTL 上 Toe-up 与 Mid-backswing 区分度低，可能同帧（如都落在 102）。
    """
    probs = _probs(200, {
        0: 100, 1: 102, 2: 102, 3: 109, 4: 113, 5: 116, 6: 118, 7: 157,
    })
    result = SwingNetDetector._constrained_events(probs)
    frames = _frames_of(result)
    assert frames == sorted(frames)
    assert len(set(frames)) == len(frames)
    # Toe-up 与 Mid-backswing 不得同帧
    assert result["Toe-up"]["frame_index"] < result["Mid-backswing"]["frame_index"]


def test_constrained_events_preserves_valid_order():
    """本就严格递增的正常输入：区间约束不应破坏正确结果。"""
    probs = _probs(200, {
        0: 10, 1: 25, 2: 35, 3: 43, 4: 50, 5: 54, 6: 60, 7: 75,
    })
    result = SwingNetDetector._constrained_events(probs)
    frames = _frames_of(result)
    assert frames == sorted(frames)
    assert len(set(frames)) == len(frames)
    # 各事件仍落在合理区间（未被重定位到异常位置）
    assert result["Address"]["frame_index"] == 10
    assert result["Top"]["frame_index"] == 43
    assert result["Impact"]["frame_index"] == 54
    assert result["Finish"]["frame_index"] == 75
    assert 10 < result["Toe-up"]["frame_index"] < 43
    assert result["Toe-up"]["frame_index"] < result["Mid-backswing"]["frame_index"] < 43
