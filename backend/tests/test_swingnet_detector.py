"""SwingNetDetector 单元测试（M1 封装，不接 pipeline）。

torch 权重加载慢（60MB），常规用例走轻量验证、不真正加载权重；
真实推理用例标记 opt-in（``GOLF_RUN_SWINGNET_SLOW=1``），默认跳过以保持
全量 ``pytest backend/tests -q`` 快速且全绿。
"""

from __future__ import annotations

import os

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
