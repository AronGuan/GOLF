"""轻量击球帧校正单元测试（ARCHITECTURE-v3-clublite.md §6.1，10 场景）。

合成视频在 conftest 的骨架序列（``build_pose``）基础上自绘"杆 + 球"：
- 杆：从握把（双腕中点）到杆头画亮色杆身线 + 杆头实心圆；
- 球：地面带内白色圆，杆头在已知帧 ``t_c`` 覆盖球点（diff 峰 == 触球帧）。

用例覆盖：
    1. 杆头贴球（球在 t_c 被杆头覆盖）   -> new_array_index 与 t_c 差 ≤ 1
    2. 无球场景（仅杆头贴地线）          -> ball_detected=False 但 available=True
    3. 静止/无挥杆                       -> available=False, method="none"
    4. ROI 全黑（底部遮挡带）            -> available=False，不抛异常
    5. 低对比（运动强度 < 阈值）         -> available=False
    6. _detect_ball 单圆/多圆/无圆       -> 球心 <3px / None / None
    7. plan_refine_frames 窗口边界       -> 候选∈[i_est-back, i_est+fwd] 含前一帧
    8. CLUBLITE_ENABLED=False            -> available=False 且 opens 不增长
    9. reanchor_impact 单调性冲突        -> 返回 None，原 events 不变
    10. MAX_SHIFT_FRAMES 顶盖            -> 不采纳（available=False）
"""

from __future__ import annotations

import os
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np
import pytest

from app import config, frame_reader, geometry, impact_refiner, segmenter
from app.schemas import (
    CameraView,
    PhaseKey,
    SwingEvent,
    VideoMeta,
)

from conftest import (
    DURATION,
    FPS,
    N_FRAMES,
    VIDEO_H,
    VIDEO_W,
    _interp,
    build_pose,
    make_swing_frames,
    make_still_frames,
)

#: 注入真值：杆头覆盖球点的帧（在 conftest 合成挥杆 impact=51 之后 6 帧，
#: 模拟"腕部击球估计偏早"的真实偏差）
T_CONTACT: int = 57


# ---------------------------------------------------------------------------
# 合成视频构造
# ---------------------------------------------------------------------------


def _club_geometry() -> Tuple[int, int, int, int]:
    """返回 ``(ankle_mid_x, ankle_mid_y, body_h_px, roi_top_y)``（像素）。"""
    _, norm0 = build_pose(0.0)
    ankle_mid_x = int(
        (norm0[geometry.L_ANKLE, 0] + norm0[geometry.R_ANKLE, 0]) / 2.0 * VIDEO_W
    )
    ankle_mid_y = int(
        (norm0[geometry.L_ANKLE, 1] + norm0[geometry.R_ANKLE, 1]) / 2.0 * VIDEO_H
    )
    nose_y = int(norm0[geometry.NOSE, 1] * VIDEO_H)
    body_h = abs(ankle_mid_y - nose_y)
    roi_top = int(ankle_mid_y + config.CLUBLITE_ROI_TOP_MARGIN_RATIO * body_h)
    return ankle_mid_x, ankle_mid_y, body_h, roi_top


def _write_club_video(
    path: str,
    t_c: int = T_CONTACT,
    with_ball: bool = True,
    club_color: Tuple[int, int, int] = (60, 60, 255),
    roi_black: bool = False,
    still: bool = False,
) -> str:
    """写合成"带杆+球"挥杆视频；杆头在 ``t_c`` 帧到达球点。

    Args:
        path: 输出路径。
        t_c: 杆头覆盖球点的帧（真值）。
        with_ball: 是否画白色球。
        club_color: 杆身/杆头颜色（BGR）。低对比用接近背景的暗色。
        roi_black: 把地面 ROI 带涂黑（模拟底部遮挡带）。
        still: 骨架静止 + 不画杆（静止/无挥杆场景）。
    """
    ankle_mid_x, _ankle_mid_y, _body_h, roi_top = _club_geometry()
    ball_x = ankle_mid_x + 40
    ball_y = roi_top + 25

    # 杆头轨迹：t_c 前在 ROI 上方静止，t_c 到达球点，之后静止
    head_keys: List[Tuple[float, Tuple[float, float]]] = [
        (0.0, (ball_x + 160.0, ball_y - 220.0)),
        ((t_c - 2) / FPS, (ball_x + 160.0, ball_y - 220.0)),
        ((t_c - 1) / FPS, (ball_x + 40.0, ball_y - 70.0)),
        (t_c / FPS, (float(ball_x), float(ball_y))),
        ((t_c + 2) / FPS, (float(ball_x), float(ball_y))),
        (DURATION, (float(ball_x), float(ball_y))),
    ]

    writer = cv2.VideoWriter(
        path, cv2.VideoWriter_fourcc(*"mp4v"), FPS, (VIDEO_W, VIDEO_H)
    )
    assert writer.isOpened(), "cv2.VideoWriter 无法创建 mp4（缺少 mp4v 编码器）"

    for k in range(N_FRAMES):
        t = k / FPS
        if still:
            _, norm = build_pose(0.0)
        else:
            _, norm = build_pose(t)
        img = np.full((VIDEO_H, VIDEO_W, 3), (70, 80, 70), dtype=np.uint8)
        if roi_black:
            # 地面带全黑（模拟底部遮挡带）：ROI 内无任何内容
            img[roi_top:, :] = (0, 0, 0)

        pts = {
            i: (int(norm[i, 0] * VIDEO_W), int(norm[i, 1] * VIDEO_H))
            for i in range(geometry.NUM_LANDMARKS)
        }
        for a, b in geometry.SKELETON_EDGES:
            cv2.line(img, pts[a], pts[b], (200, 200, 200), 4, cv2.LINE_AA)
        for i in geometry.CORE_IDS:
            cv2.circle(img, pts[i], 5, (240, 240, 240), -1, cv2.LINE_AA)

        if with_ball and not roi_black:
            cv2.circle(img, (ball_x, ball_y), 12, (255, 255, 255), -1, cv2.LINE_AA)

        if not still and not roi_black:
            grip = (
                int(
                    (norm[geometry.L_WRIST, 0] + norm[geometry.R_WRIST, 0])
                    / 2.0 * VIDEO_W
                ),
                int(
                    (norm[geometry.L_WRIST, 1] + norm[geometry.R_WRIST, 1])
                    / 2.0 * VIDEO_H
                ),
            )
            head = tuple(int(v) for v in _interp(t, head_keys))
            cv2.line(
                img, grip, head, club_color, 5, cv2.LINE_AA
            )
            cv2.circle(img, head, 10, club_color, -1, cv2.LINE_AA)
        writer.write(img)
    writer.release()
    assert os.path.getsize(path) > 0
    return path


def _swing_events() -> List[SwingEvent]:
    """对 conftest 合成挥杆跑真实切分，得到 8 事件（impact 数组下标 51）。"""
    frames = make_swing_frames()
    signals = segmenter.build_signals(frames, FPS, aspect=1.0)
    return segmenter.segment_swing(frames, FPS, sig=signals)


def _swing_signals():
    frames = make_swing_frames()
    return segmenter.build_signals(frames, FPS, aspect=1.0)


# ---------------------------------------------------------------------------
# 用例 1~5：refine_impact 主流程
# ---------------------------------------------------------------------------


class TestRefineImpact:
    """M1 地面 ROI 运动峰校正核心。"""

    def test_ball_covered_moves_impact_to_contact(
        self, tmp_path, video_meta
    ):
        """#1 合成"杆头贴球"视频：new_array_index 与注入真值 t_c 差 ≤ 1 帧。"""
        path = _write_club_video(str(tmp_path / "club_ball.mp4"))
        frames = make_swing_frames()
        events = _swing_events()
        signals = _swing_signals()
        result = impact_refiner.refine_impact(
            path, frames, events, signals, CameraView.FACE_ON, video_meta,
        )
        assert result.available, f"应校正成功: {result}"
        assert abs(result.new_array_index - T_CONTACT) <= 1, (
            f"校正帧 {result.new_array_index} 应贴近真值 {T_CONTACT}"
        )
        assert result.delta_frames > 0, "腕部估计偏早，应后移"
        assert result.ball_detected, "Address 帧应检出白球"
        assert result.method in ("motion", "motion+shaft")

    def test_dtl_view_also_refines(self, tmp_path, video_meta):
        """DTL 机位（全宽 ROI）同样校正成功。"""
        path = _write_club_video(str(tmp_path / "club_dtl.mp4"))
        frames = make_swing_frames()
        events = _swing_events()
        signals = _swing_signals()
        result = impact_refiner.refine_impact(
            path, frames, events, signals, CameraView.DOWN_THE_LINE, video_meta,
        )
        assert result.available
        assert abs(result.new_array_index - T_CONTACT) <= 1
        assert result.delta_frames > 0

    def test_no_ball_still_refines(self, tmp_path, video_meta):
        """#2 无球场景：无白球，仅杆头贴地线，仍靠质心贴地约束校正。"""
        path = _write_club_video(
            str(tmp_path / "club_noball.mp4"), with_ball=False
        )
        frames = make_swing_frames()
        events = _swing_events()
        signals = _swing_signals()
        result = impact_refiner.refine_impact(
            path, frames, events, signals, CameraView.FACE_ON, video_meta,
        )
        assert result.available, f"无球也应能校正: {result}"
        assert not result.ball_detected
        assert abs(result.new_array_index - T_CONTACT) <= 1
        assert result.delta_frames > 0

    def test_still_video_degrades(self, tmp_path, video_meta):
        """#3 静止/无挥杆：available=False，method='none'。"""
        path = _write_club_video(
            str(tmp_path / "still.mp4"), still=True
        )
        frames = make_swing_frames()
        events = _swing_events()
        signals = _swing_signals()
        result = impact_refiner.refine_impact(
            path, frames, events, signals, CameraView.FACE_ON, video_meta,
        )
        assert not result.available
        assert result.method == "none"

    def test_roi_black_degrades(self, tmp_path, video_meta):
        """#4 ROI 全黑（底部遮挡带）：降级 available=False，不抛异常。"""
        path = _write_club_video(
            str(tmp_path / "roi_black.mp4"), roi_black=True
        )
        frames = make_swing_frames()
        events = _swing_events()
        signals = _swing_signals()
        result = impact_refiner.refine_impact(
            path, frames, events, signals, CameraView.FACE_ON, video_meta,
        )
        assert not result.available

    def test_low_contrast_degrades(self, tmp_path, video_meta):
        """#5 低对比（杆身亮度接近背景，灰度差 < DIFF_THRESH）：降级。

        无球场景（有球的话，球被覆盖本身就会产生强 diff），仅杆身贴地线
        亮度 ≈ 背景 -> 帧差阈值下无有效像素 -> G0。
        """
        path = _write_club_video(
            str(tmp_path / "low_contrast.mp4"),
            club_color=(72, 82, 72),  # 灰度 ≈ 78 vs 背景 ≈ 76
            with_ball=False,
        )
        frames = make_swing_frames()
        events = _swing_events()
        signals = _swing_signals()
        result = impact_refiner.refine_impact(
            path, frames, events, signals, CameraView.FACE_ON, video_meta,
        )
        assert not result.available

    def test_enabled_false_no_decode(self, tmp_path, video_meta, monkeypatch):
        """#8 CLUBLITE_ENABLED=False：available=False 且不解码（opens 不增长）。"""
        monkeypatch.setattr(config, "CLUBLITE_ENABLED", False)
        frame_reader.reset_stats()
        path = _write_club_video(str(tmp_path / "off.mp4"))
        frames = make_swing_frames()
        events = _swing_events()
        signals = _swing_signals()
        result = impact_refiner.refine_impact(
            path, frames, events, signals, CameraView.FACE_ON, video_meta,
        )
        assert not result.available
        assert frame_reader.stats()["opens"] == 0, (
            f"关闭时应不解码: {frame_reader.stats()}"
        )

    def test_max_shift_cap_rejects(self, tmp_path, video_meta, monkeypatch):
        """#10 MAX_SHIFT_FRAMES 顶盖：注入远超上限的位移 -> 不采纳。"""
        monkeypatch.setattr(config, "CLUBLITE_MAX_SHIFT_FRAMES", 2)
        path = _write_club_video(str(tmp_path / "cap.mp4"))
        frames = make_swing_frames()
        events = _swing_events()
        signals = _swing_signals()
        result = impact_refiner.refine_impact(
            path, frames, events, signals, CameraView.FACE_ON, video_meta,
        )
        # delta = 57 - 51 = 6 > 2 → 拒绝（保留诊断字段）
        assert result.delta_frames == 6
        assert not result.available
        assert result.method == "none"

    def test_never_raises_on_bad_input(self, video_meta, tmp_path):
        """模块级硬约束：任何失败都不外抛异常。"""
        path = str(tmp_path / "not_a_video.mp4")
        with open(path, "wb") as fh:
            fh.write(b"not a real video")
        frames = make_swing_frames()
        events = _swing_events()
        signals = _swing_signals()
        # 坏视频：grab_frames 抛 AnalysisError，refine 必须吞掉并返回 available=False
        result = impact_refiner.refine_impact(
            path, frames, events, signals, CameraView.FACE_ON, video_meta,
        )
        assert isinstance(result, impact_refiner.ImpactRefineResult)
        assert not result.available
        # 空 frames / 空 events
        result2 = impact_refiner.refine_impact(
            path, [], [], _swing_signals(), CameraView.FACE_ON, video_meta,
        )
        assert not result2.available


# ---------------------------------------------------------------------------
# 用例 6：_detect_ball
# ---------------------------------------------------------------------------


class TestDetectBall:
    """球点检测：单圆采信 / 多圆歧义 / 无圆 None。"""

    def test_single_white_circle(self):
        img = np.full((120, 240, 3), 70, dtype=np.uint8)
        cv2.circle(img, (120, 60), 12, (255, 255, 255), -1)
        center = impact_refiner._detect_ball(img, (0, 0, 240, 120))
        assert center is not None
        assert abs(center[0] - 120) < 3 and abs(center[1] - 60) < 3

    def test_multiple_circles_ambiguous(self):
        img = np.full((120, 240, 3), 70, dtype=np.uint8)
        cv2.circle(img, (60, 60), 12, (255, 255, 255), -1)
        cv2.circle(img, (180, 60), 12, (255, 255, 255), -1)
        assert impact_refiner._detect_ball(img, (0, 0, 240, 120)) is None

    def test_no_ball(self):
        img = np.full((120, 240, 3), 70, dtype=np.uint8)
        assert impact_refiner._detect_ball(img, (0, 0, 240, 120)) is None


# ---------------------------------------------------------------------------
# 用例 7：plan_refine_frames 窗口边界
# ---------------------------------------------------------------------------


class TestPlanRefineFrames:
    """窗口规划：边界 / 含前一帧 / 去重升序 / 不越界。"""

    def test_window_bounds_and_previous(self, video_meta):
        frames = make_swing_frames()
        events = _swing_events()
        signals = _swing_signals()
        impact = next(e for e in events if e.key is PhaseKey.IMPACT)
        back_n = max(1, int(round(config.CLUBLITE_SEARCH_BACK_SEC * signals.fps_eff)))
        fwd_n = max(1, int(round(config.CLUBLITE_SEARCH_FWD_SEC * signals.fps_eff)))
        cand, decode = impact_refiner.plan_refine_frames(
            events, signals, video_meta, frames=frames
        )
        assert cand == sorted(set(cand))
        assert decode == sorted(set(decode))
        lo = max(0, impact.array_index - back_n)
        hi = min(signals.n - 1, impact.array_index + fwd_n)
        # 候选帧 = 窗口内采样帧（step=1 时 == array 下标）
        assert min(cand) == lo
        assert max(cand) == hi
        # 含前一帧（差分起点）
        assert (lo - 1) in decode
        # Address 帧（球点检测参考）在解码集内
        addr = next(e for e in events if e.key is PhaseKey.ADDRESS)
        assert addr.frame_index in decode
        # 不越界
        assert all(0 <= f < N_FRAMES for f in cand + decode)

    def test_empty_events(self, video_meta):
        cand, decode = impact_refiner.plan_refine_frames([], _swing_signals(), video_meta)
        assert cand == [] and decode == []


# ---------------------------------------------------------------------------
# 用例 9：reanchor_impact
# ---------------------------------------------------------------------------


class TestReanchorImpact:
    """用校正后的击球帧重建 8 事件（纯函数）。"""

    def test_valid_reanchor(self):
        frames = make_swing_frames()
        signals = _swing_signals()
        events = _swing_events()
        impact = next(e for e in events if e.key is PhaseKey.IMPACT)
        new_idx = impact.array_index + 3
        rebuilt = segmenter.reanchor_impact(frames, signals, events, new_idx)
        assert rebuilt is not None
        assert len(rebuilt) == 8
        new_impact = next(e for e in rebuilt if e.key is PhaseKey.IMPACT)
        assert new_impact.array_index == new_idx
        assert new_impact.estimated is False, "校正后 impact 不应是估算"
        indices = [e.array_index for e in rebuilt]
        assert all(b > a for a, b in zip(indices, indices[1:])), indices

    def test_conflict_beyond_finish_returns_none(self):
        frames = make_swing_frames()
        signals = _swing_signals()
        events = _swing_events()
        finish = next(e for e in events if e.key is PhaseKey.FINISH)
        assert segmenter.reanchor_impact(frames, signals, events, finish.array_index) is None
        top = next(e for e in events if e.key is PhaseKey.TOP)
        assert segmenter.reanchor_impact(frames, signals, events, top.array_index) is None

    def test_conflict_does_not_mutate_original(self):
        frames = make_swing_frames()
        signals = _swing_signals()
        events = _swing_events()
        original = [e.array_index for e in events]
        finish = next(e for e in events if e.key is PhaseKey.FINISH)
        segmenter.reanchor_impact(frames, signals, events, finish.array_index)
        assert [e.array_index for e in events] == original


# ---------------------------------------------------------------------------
# M2：杆身端点验证
# ---------------------------------------------------------------------------


class TestShaftLowestY:
    """简化 Hough 杆身端点验证（P1 加分项）。"""

    def test_finds_lowest_endpoint(self):
        img = np.full((400, 600, 3), 70, dtype=np.uint8)
        grip = np.array([300.0, 150.0])
        head = np.array([300.0, 330.0])
        cv2.line(
            img,
            (int(grip[0]), int(grip[1])),
            (int(head[0]), int(head[1])),
            (60, 60, 255),
            6,
            cv2.LINE_AA,
        )
        landmark_px = np.zeros((geometry.NUM_LANDMARKS, 2), dtype=np.float64)
        low_y = impact_refiner._shaft_lowest_y(
            img, landmark_px, grip, 200.0, CameraView.FACE_ON
        )
        assert low_y is not None
        assert abs(low_y - head[1]) < 8, f"杆头端点 y 应 ≈ {head[1]}, got {low_y}"

    def test_no_line_returns_none(self):
        img = np.full((400, 600, 3), 70, dtype=np.uint8)
        landmark_px = np.zeros((geometry.NUM_LANDMARKS, 2), dtype=np.float64)
        assert (
            impact_refiner._shaft_lowest_y(
                img, landmark_px, np.array([300.0, 150.0]), 200.0,
                CameraView.FACE_ON,
            )
            is None
        )


# ---------------------------------------------------------------------------
# QA P1 回归：reanchor 后所有事件帧 ∈ 解码帧集（无渲染 fallback）
# ---------------------------------------------------------------------------


class TestDecodeCoverage:
    """解码并集覆盖 reanchor 全部可能产出的事件帧（opens=1 修复）。"""

    def test_plan_reanchor_frames_covers_all_candidates(self, video_meta):
        """对窗口内每个候选下标 reanchor，其 8 事件帧都应在解码并集内。"""
        frames = make_swing_frames()
        events = _swing_events()
        signals = _swing_signals()
        cand, decode = impact_refiner.plan_refine_frames(
            events, signals, video_meta, frames=frames
        )
        possible = impact_refiner.plan_reanchor_frames(
            events, signals, video_meta, frames=frames, cand_frames=cand
        )
        union = set(decode) | set(possible)
        index_to_array = {f.frame_index: i for i, f in enumerate(frames)}
        for cand_frame in cand:
            array_index = index_to_array.get(cand_frame)
            if array_index is None:
                continue
            rebuilt = segmenter.reanchor_impact(
                frames, signals, events, array_index
            )
            if rebuilt is None:
                continue
            for e in rebuilt:
                assert e.frame_index in union, (
                    f"候选 {array_index} 的 reanchor 事件帧 "
                    f"{e.key.value}={e.frame_index} 不在解码并集"
                )

    def test_pipeline_flow_no_render_fallback(self, tmp_path, video_meta):
        """端到端：club 视频 refine + reanchor 后 8 事件帧全在解码集。

        模拟 pipeline step4a 的解码顺序（event ∪ window ∪ possible），
        断言校正后每个事件帧都在 ``frames_bgr`` 里 —— renderer 不会 fallback。
        """
        import cv2

        path = _write_club_video(str(tmp_path / "club_coverage.mp4"))
        frames = make_swing_frames()
        events = _swing_events()
        signals = _swing_signals()
        cand, decode = impact_refiner.plan_refine_frames(
            events, signals, video_meta, frames=frames
        )
        possible = impact_refiner.plan_reanchor_frames(
            events, signals, video_meta, frames=frames, cand_frames=cand
        )
        event_frames = [e.frame_index for e in events]
        frames_bgr = frame_reader.grab_frames(
            path,
            sorted(set(event_frames) | set(decode) | set(possible)),
        )
        refine = impact_refiner.refine_impact(
            path, frames, events, signals, CameraView.FACE_ON, video_meta,
            frames_bgr=frames_bgr,
        )
        assert refine.available, f"合成杆+球视频应校正成功: {refine}"
        new_events = segmenter.reanchor_impact(
            frames, signals, events, refine.new_array_index
        )
        assert new_events is not None
        for e in new_events:
            assert e.frame_index in frames_bgr, (
                f"{e.key.value}={e.frame_index} 未解码 -> renderer 会 fallback 错帧"
            )
        # 校正后 follow_through 若移动，其真帧必须已解码（P1 核心断言）
        old_ft = next(e for e in events if e.key is PhaseKey.FOLLOW_THROUGH)
        new_ft = next(e for e in new_events if e.key is PhaseKey.FOLLOW_THROUGH)
        if new_ft.frame_index != old_ft.frame_index:
            assert new_ft.frame_index in frames_bgr
            # 帧内容确实是 follow_through 真帧（非相邻兜底）
            assert cv2.absdiff(
                frames_bgr[new_ft.frame_index], frames_bgr[old_ft.frame_index]
            ).sum() > 0


# ---------------------------------------------------------------------------
# 渲染标注（可选 P2，默认关闭）
# ---------------------------------------------------------------------------


class TestRendererMarker:
    """CLUBLITE_DRAW_MARKER=False 时输出与现状一致（逐字节）。"""

    def test_marker_off_byte_identical(self, tmp_path, synth_video):
        from app import renderer
        from conftest import make_swing_frames

        frames = make_swing_frames()
        events = segmenter.segment_swing(frames, FPS)
        produced = renderer.render_events(
            synth_video, events, str(tmp_path / "plain"), frames
        )
        produced_marker = renderer.render_events(
            synth_video, events, str(tmp_path / "with_marker_arg"), frames,
            markers={next(e for e in events if e.key is PhaseKey.IMPACT).frame_index: (100, 100)},
        )
        for key, name in produced.items():
            a = (tmp_path / "plain" / name).read_bytes()
            b = (tmp_path / "with_marker_arg" / name).read_bytes()
            assert a == b, f"{key}: DRAW_MARKER=False 时应逐字节一致"
