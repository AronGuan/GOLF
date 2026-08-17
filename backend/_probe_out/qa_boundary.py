"""QA 边界/错误路径独立验证（临时探针，不进主链路）。

覆盖清单（team-lead 指派）：
1. 极端输入：全黑帧 / 无运动帧 / 超短视频 / 异常分辨率 -> G0 降级不崩
2. CLUBLITE_ENABLED=False 链路完整（refine 直接 available=False 且 opens 不增）
3. frames_bgr 缺候选帧（共享解码缺帧）-> G0
4. 非法机位（AUTO）-> G0
5. 窗口规划：back/fwd 极大时钳制在合法范围
"""

from __future__ import annotations

import os
import sys

import cv2
import numpy as np

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import config, frame_reader, geometry, impact_refiner, segmenter  # noqa: E402
from app.schemas import CameraView, VideoMeta  # noqa: E402

sys.path.insert(0, os.path.join(BASE_DIR, "tests"))
from conftest import (  # noqa: E402
    FPS,
    N_FRAMES,
    VIDEO_H,
    VIDEO_W,
    make_swing_frames,
    make_still_frames,
)

OUT_DIR = os.path.join(BASE_DIR, "_probe_out")
os.makedirs(OUT_DIR, exist_ok=True)


def _meta(width=VIDEO_W, height=VIDEO_H, n=N_FRAMES, fps=FPS, step=1):
    return VideoMeta(
        fps=fps, duration=n / fps, width=width, height=height,
        frame_count=n, sample_step=step, low_fps=False,
    )


def _swing_ctx():
    frames = make_swing_frames()
    signals = segmenter.build_signals(frames, FPS, aspect=1.0)
    events = segmenter.segment_swing(frames, FPS, sig=signals)
    return frames, signals, events


def _write_video(path, frames_bgr_list):
    writer = cv2.VideoWriter(
        path, cv2.VideoWriter_fourcc(*"mp4v"), FPS,
        (frames_bgr_list[0].shape[1], frames_bgr_list[0].shape[0]),
    )
    assert writer.isOpened(), "cannot open writer"
    for img in frames_bgr_list:
        writer.write(img)
    writer.release()
    assert os.path.getsize(path) > 0


def _all_black():
    return [np.zeros((VIDEO_H, VIDEO_W, 3), dtype=np.uint8) for _ in range(N_FRAMES)]


def _still():
    return [np.full((VIDEO_H, VIDEO_W, 3), (70, 80, 70), dtype=np.uint8) for _ in range(N_FRAMES)]


def run_case(name, fn):
    try:
        fn()
        print(f"[PASS] {name}")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        return False
    return True


def main() -> int:
    frames, signals, events = _swing_ctx()
    meta = _meta()
    n_pass = 0
    n_total = 0

    def c(name, fn):
        nonlocal n_pass, n_total
        n_total += 1
        n_pass += int(run_case(name, fn))

    # 1a. 全黑帧视频
    def case_black():
        path = os.path.join(OUT_DIR, "qa_black.mp4")
        _write_video(path, _all_black())
        r = impact_refiner.refine_impact(path, frames, events, signals, CameraView.FACE_ON, meta)
        assert not r.available, r
        assert r.method == "none", r
    c("全黑帧 -> G0", case_black)

    # 1b. 无运动帧（静止站立视频）
    def case_still():
        path = os.path.join(OUT_DIR, "qa_still.mp4")
        _write_video(path, _still())
        r = impact_refiner.refine_impact(path, frames, events, signals, CameraView.FACE_ON, meta)
        assert not r.available, r
    c("无运动帧 -> G0", case_still)

    # 1c. 超短视频（3 帧，低于切分需要）
    def case_tiny():
        path = os.path.join(OUT_DIR, "qa_tiny.mp4")
        _write_video(path, [np.full((VIDEO_H, VIDEO_W, 3), 70, dtype=np.uint8)] * 3)
        r = impact_refiner.refine_impact(
            path, frames[:10], events, signals, CameraView.FACE_ON, meta
        )
        # 即使 events 合法，帧数不足也应降级不崩
        assert isinstance(r, impact_refiner.ImpactRefineResult)
    c("超短视频 -> 不崩", case_tiny)

    # 1d. 异常分辨率（5x5 极小帧）
    def case_weird_res():
        small = [np.full((5, 5, 3), 70, dtype=np.uint8) for _ in range(30)]
        path = os.path.join(OUT_DIR, "qa_weird.mp4")
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 30, (5, 5))
        for img in small:
            writer.write(img)
        writer.release()
        tiny_meta = _meta(width=5, height=5, n=30)
        r = impact_refiner.refine_impact(
            path, frames[:30], events, signals, CameraView.FACE_ON, tiny_meta
        )
        assert isinstance(r, impact_refiner.ImpactRefineResult)
    c("异常分辨率 -> 不崩", case_weird_res)

    # 2. CLUBLITE_ENABLED=False
    def case_disabled(monkeypatch=None):
        old = config.CLUBLITE_ENABLED
        config.CLUBLITE_ENABLED = False
        try:
            frame_reader.reset_stats()
            path = os.path.join(OUT_DIR, "qa_disabled.mp4")
            # 复用黑帧即可：关闭时不应解码
            _write_video(path, _all_black())
            r = impact_refiner.refine_impact(path, frames, events, signals, CameraView.FACE_ON, meta)
            assert not r.available, r
            assert frame_reader.stats()["opens"] == 0, frame_reader.stats()
        finally:
            config.CLUBLITE_ENABLED = old
    c("CLUBLITE_ENABLED=False -> G0 且 opens=0", case_disabled)

    # 3. frames_bgr 缺候选帧（共享解码缺帧 -> G0，不崩）
    def case_missing_frames():
        cand, decode = impact_refiner.plan_refine_frames(events, signals, meta, frames=frames)
        partial = {f: np.full((VIDEO_H, VIDEO_W, 3), 70, dtype=np.uint8) for f in decode if f % 2 == 0}
        r = impact_refiner.refine_impact(
            "unused.mp4", frames, events, signals, CameraView.FACE_ON, meta,
            frames_bgr=partial,
        )
        assert isinstance(r, impact_refiner.ImpactRefineResult)
    c("frames_bgr 缺候选帧 -> G0 不崩", case_missing_frames)

    # 4. 非法机位（AUTO）-> G0
    def case_auto_view():
        path = os.path.join(OUT_DIR, "qa_auto.mp4")
        _write_video(path, _all_black())
        r = impact_refiner.refine_impact(path, frames, events, signals, CameraView.AUTO, meta)
        assert not r.available, r
    c("机位 AUTO -> G0", case_auto_view)

    # 5. 窗口规划钳制：back/fwd 极大/极小
    def case_window_clamp():
        cand, decode = impact_refiner.plan_refine_frames(
            events, signals, meta, frames=frames, back_sec=999.0, fwd_sec=999.0
        )
        assert cand and decode
        assert min(cand) >= 0 and max(cand) < N_FRAMES
        cand0, decode0 = impact_refiner.plan_refine_frames(
            events, signals, meta, frames=frames, back_sec=0.0, fwd_sec=0.0
        )
        assert cand0 and decode0
    c("窗口规划钳制", case_window_clamp)

    # 6. reanchor_impact：空/非法输入 -> None 不崩
    def case_reanchor_bad():
        assert segmenter.reanchor_impact([], signals, events, 5) is None
        assert segmenter.reanchor_impact(frames, signals, [], 5) is None
    c("reanchor_impact 非法输入 -> None", case_reanchor_bad)

    print(f"\n边界/错误路径: {n_pass}/{n_total} 通过")
    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    raise SystemExit(main())
