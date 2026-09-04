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
    11. CLUBLITE_IMPACT_OFFSET（v2）     -> new = motion_peak + offset；0 回滚 v1
    12. delta==0 无操作校正（v2）        -> 偏移把运动峰拉回原估计不降级
    13. 物理下界（v2）                   -> 偏移不早于 top+min_gap（G0 兜底）
    14. D 方案锚点邻域（v3，2026-08）    -> _anchor_neighborhood / _select_best
        纯函数 + 合成视频不回归（横扫式运动峰偏晚修正，接口契约零变化）
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
    FrameLandmarks,
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


@pytest.fixture(autouse=True)
def _physical_guards_off(monkeypatch):
    """默认**关闭** 2026-09-04 新增的物理窗口守卫。

    合成挥杆的事件分布本身不物理：top=42、refine 目标 56（= ``T_CONTACT``
    57 加 ``CLUBLITE_IMPACT_OFFSET`` -1），下杆 (56-42)/30 = **0.467s**，
    远超真实下杆 0.20~0.30s。若启用守卫会被一致拒绝，反而掩盖本文件真正
    要测的 refine 核心逻辑（M1 运动峰 / M2 杆身 / D 方案锚点）。

    守卫的专项验证见 :class:`TestPhysicalGuard`。
    """
    monkeypatch.setattr(config, "CLUBLITE_MIN_FOLLOW_THROUGH_SEC", 0.0)
    monkeypatch.setattr(config, "CLUBLITE_MAX_DOWNSTROKE_SEC", 999.0)


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
        # D 方案：运动峰 = 57（peak_delta = 57-51 = 6 > MAX=2 → 拒绝）；
        # CLUBLITE_IMPACT_OFFSET=-1 后最终 new = 56，诊断 delta_frames = 5
        assert result.motion_peak_index == 57, f"运动峰应落在 57: {result}"
        assert result.delta_frames == 5
        assert not result.available
        assert result.method == "none"

    def test_anchor_keeps_contact_frame(self, tmp_path, video_meta):
        """#14 D 方案：合成杆+球视频锚点==运动峰，选帧不回归（仍贴近真值 t_c）。"""
        path = _write_club_video(str(tmp_path / "club_anchor.mp4"))
        frames = make_swing_frames()
        events = _swing_events()
        signals = _swing_signals()
        result = impact_refiner.refine_impact(
            path, frames, events, signals, CameraView.FACE_ON, video_meta,
        )
        assert result.available, f"应校正成功: {result}"
        assert abs(result.new_array_index - T_CONTACT) <= 1
        # 契约：new == 峰 + CLUBLITE_IMPACT_OFFSET（锚点法把峰拉回接触后，
        # -1 偏移再微调到视觉接触帧）
        assert result.new_array_index == (
            result.motion_peak_index + config.CLUBLITE_IMPACT_OFFSET
        )
        # 合成视频 M2 杆身检测不稳定（骨架掩膜可能过滤杆身线），锚点存在时
        # 须贴近真值；不存在时锚点路径由纯函数用例 + 真实视频探针覆盖
        if result.shaft_lowest_index is not None:
            assert abs(result.shaft_lowest_index - T_CONTACT) <= 1

    def test_impact_offset_applied_to_peak(self, tmp_path, video_meta):
        """#11 v2：CLUBLITE_IMPACT_OFFSET=-1 时 new = motion_peak + offset。"""
        path = _write_club_video(str(tmp_path / "club_offset.mp4"))
        frames = make_swing_frames()
        events = _swing_events()
        signals = _swing_signals()
        result = impact_refiner.refine_impact(
            path, frames, events, signals, CameraView.FACE_ON, video_meta,
        )
        assert result.available, f"合成杆+球视频应校正成功: {result}"
        assert result.motion_peak_index is not None
        assert result.new_array_index == (
            result.motion_peak_index + config.CLUBLITE_IMPACT_OFFSET
        )
        assert result.delta_frames == (
            result.new_array_index - result.old_array_index
        )

    def test_impact_offset_zero_restores_v1(self, tmp_path, video_meta, monkeypatch):
        """#11 v2：CLUBLITE_IMPACT_OFFSET=0 回滚到 v1 行为（new == motion_peak）。"""
        monkeypatch.setattr(config, "CLUBLITE_IMPACT_OFFSET", 0)
        path = _write_club_video(str(tmp_path / "club_off0.mp4"))
        frames = make_swing_frames()
        events = _swing_events()
        signals = _swing_signals()
        result = impact_refiner.refine_impact(
            path, frames, events, signals, CameraView.FACE_ON, video_meta,
        )
        assert result.available
        assert result.motion_peak_index is not None
        assert result.new_array_index == result.motion_peak_index

    def test_offset_zero_delta_adopted_as_noop(self, tmp_path, video_meta, monkeypatch):
        """#12 v2：偏移把运动峰拉回原估计（delta==0）→ 合法无操作校正，不降级。

        模拟真实视频"正面1"场景：运动峰 = 原估计 + 1，偏移 -1 后落回原估计
        （delta=0）。此时算法确认原 impact 即视觉接触帧，应照常 available=True
        （reanchor 幂等返回原 events），而不是被 MIN_SHIFT 判成 G0。
        """
        path = _write_club_video(str(tmp_path / "club_delta0.mp4"))
        frames = make_swing_frames()
        events = _swing_events()
        signals = _swing_signals()
        base = impact_refiner.refine_impact(
            path, frames, events, signals, CameraView.FACE_ON, video_meta,
        )
        assert base.available and base.motion_peak_index is not None
        peak = base.motion_peak_index
        old = base.old_array_index
        # 偏移量 = 把运动峰恰好拉回原估计
        monkeypatch.setattr(config, "CLUBLITE_IMPACT_OFFSET", old - peak)
        result = impact_refiner.refine_impact(
            path, frames, events, signals, CameraView.FACE_ON, video_meta,
        )
        assert result.available, f"delta==0 不应因 MIN_SHIFT 降级: {result}"
        assert result.new_array_index == old
        assert result.delta_frames == 0

    def test_offset_cannot_breach_min_gap(self, tmp_path, video_meta, monkeypatch):
        """#13 v2：偏移把 impact 推到 top+min_gap 之前 → G0（不返回非法下标）。"""
        monkeypatch.setattr(config, "CLUBLITE_IMPACT_OFFSET", -100)
        path = _write_club_video(str(tmp_path / "club_lower.mp4"))
        frames = make_swing_frames()
        events = _swing_events()
        signals = _swing_signals()
        result = impact_refiner.refine_impact(
            path, frames, events, signals, CameraView.FACE_ON, video_meta,
        )
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
# 方案 A：M2 全窗口化（_shaft_scan_window / _pick_full_window_anchor）
# ---------------------------------------------------------------------------


class TestM2FullWindow:
    """方案 A（2026-09-04）：锚点来源扩大到整个 refine 窗口。"""

    @staticmethod
    def _line_frame(grip, head):
        """画一张 400×600 图，含 grip->head 的竖直杆身线。"""
        img = np.full((400, 600, 3), 70, dtype=np.uint8)
        cv2.line(
            img,
            (int(grip[0]), int(grip[1])),
            (int(head[0]), int(head[1])),
            (60, 60, 255),
            6,
            cv2.LINE_AA,
        )
        return img

    @staticmethod
    def _mk_frame(frame_index, grip_px, w=600, h=400):
        """构造 FrameLandmarks，双腕中点 = grip_px（供 _shaft_scan_window 取握把）。"""
        norm = np.zeros((geometry.NUM_LANDMARKS, 3), dtype=np.float64)
        gx, gy = grip_px[0] / w, grip_px[1] / h
        norm[geometry.L_WRIST] = [gx, gy, 0.0]
        norm[geometry.R_WRIST] = [gx, gy, 0.0]
        return FrameLandmarks(
            frame_index=frame_index,
            timestamp=frame_index / 30.0,
            detected=True,
            norm=norm,
            world=np.zeros((geometry.NUM_LANDMARKS, 3), dtype=np.float64),
            visibility=np.ones(geometry.NUM_LANDMARKS, dtype=np.float64),
        )

    # ---- _pick_full_window_anchor（纯逻辑）----

    def test_pick_anchor_prefers_lowest_with_motion(self):
        shaft_ys = {0: 300.0, 1: 340.0, 2: 200.0}
        raw_motion = np.array([5.0, 4.0, 6.0])
        off = impact_refiner._pick_full_window_anchor(shaft_ys, raw_motion, 0.2)
        assert off == 1  # 340 最大且 motion 4.0 >= 0.2*6

    def test_pick_anchor_skips_lowest_when_static(self):
        shaft_ys = {0: 340.0, 1: 300.0}
        raw_motion = np.array([0.5, 5.0])  # offset 0 静止（0.5 < 0.2*5）
        off = impact_refiner._pick_full_window_anchor(shaft_ys, raw_motion, 0.2)
        assert off == 1  # 跳过最低但静止的 0

    def test_pick_anchor_none_when_all_below_threshold(self):
        """杆身检出帧的运动强度均低于门槛（强运动在别的帧）-> None。

        这正是运动支持门槛要防御的：Hough 在静止帧检出假阳性，而真实运动
        在窗口内其他帧，此时锚点不可信，应回退 Top-K。
        """
        shaft_ys = {0: 340.0}  # 只在 offset 0 检出杆身
        raw_motion = np.array([0.1, 5.0])  # 最大强度 5.0 在 offset 1
        assert (
            impact_refiner._pick_full_window_anchor(shaft_ys, raw_motion, 0.2)
            is None
        )

    def test_pick_anchor_none_when_empty(self):
        assert (
            impact_refiner._pick_full_window_anchor({}, np.array([1.0]), 0.2)
            is None
        )

    # ---- _shaft_scan_window ----

    def test_scan_window_returns_empty_for_no_frames(self):
        assert (
            impact_refiner._shaft_scan_window(
                [], {}, [], [], 600, 400, 200.0, CameraView.FACE_ON
            )
            == {}
        )

    def test_scan_window_uses_per_frame_grip(self):
        """逐帧扫描，各自帧用自己的握把定位杆头端点。"""
        grip0 = np.array([300.0, 150.0])
        head0 = np.array([300.0, 330.0])
        grip1 = np.array([200.0, 150.0])
        head1 = np.array([200.0, 300.0])
        frames = [
            self._mk_frame(100, grip0),
            self._mk_frame(101, grip1),
        ]
        frames_bgr = {
            100: self._line_frame(grip0, head0),
            101: self._line_frame(grip1, head1),
        }
        result = impact_refiner._shaft_scan_window(
            [100, 101],
            frames_bgr,
            frames,
            [0, 1],
            600,
            400,
            200.0,
            CameraView.FACE_ON,
        )
        assert set(result.keys()) == {0, 1}
        assert abs(result[0] - head0[1]) < 8
        assert abs(result[1] - head1[1]) < 8


# ---------------------------------------------------------------------------
# D 方案：M2 杆身最低点先验锚点邻域（横扫式运动峰偏晚修正）
# ---------------------------------------------------------------------------


class TestAnchorSelection:
    """D 方案（2026-08 用户拍板）：锚点邻域选帧 + 回退路径。"""

    def test_anchor_neighborhood_restricts_to_window(self):
        """横扫帧（远离锚点）被排除在邻域外。"""
        # 候选偏移 [0,1,2,4] -> array [10,11,12,13,20]；20 为横扫帧（远离锚点）
        candidates = [0, 1, 2, 4]
        cand_indices = [10, 11, 12, 13, 20]
        # 锚点 = 偏移 2（array 12，杆头端点 y 最大 340）
        shaft_ys = {1: 300.0, 2: 340.0, 4: 200.0}
        nb = impact_refiner._anchor_neighborhood(
            candidates, cand_indices, shaft_ys, 8, 22, 3
        )
        assert nb is not None
        sel, anchor_array, win_lo, win_hi = nb
        assert anchor_array == 12
        assert win_lo == 9 and win_hi == 15
        assert sel == [0, 1, 2]  # 横扫帧（array 20）不在邻域 [9,15] 内

    def test_anchor_window_clamped_to_search_bounds(self):
        """邻域 clamp 到搜索区间 [lo, hi]。"""
        candidates = [0, 1, 2]
        cand_indices = [9, 10, 11]
        shaft_ys = {1: 300.0}
        nb = impact_refiner._anchor_neighborhood(
            candidates, cand_indices, shaft_ys, 9, 11, 3
        )
        assert nb is not None
        sel, anchor_array, win_lo, win_hi = nb
        assert anchor_array == 10
        assert win_lo == 9 and win_hi == 11
        assert sel == [0, 1, 2]

    def test_anchor_neighborhood_none_without_shaft(self):
        """M2 不可用（shaft_ys 为空）-> None（调用方回退全窗口）。"""
        assert (
            impact_refiner._anchor_neighborhood([0, 1], [10, 11], {}, 8, 12, 3)
            is None
        )

    def test_anchor_uses_offset_indexed_cand_indices(self):
        """回归：cand_indices 按灰度帧偏移索引（修复 cand_indices[k] -> [c]）。"""
        candidates = [5, 10]  # 灰度帧偏移
        cand_indices = [111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121]
        shaft_ys = {5: 1279.0, 10: 1279.0}  # 平票取先者 = 偏移 5（array 116）
        nb = impact_refiner._anchor_neighborhood(
            candidates, cand_indices, shaft_ys, 111, 121, 3
        )
        assert nb is not None
        sel, anchor_array, win_lo, win_hi = nb
        assert anchor_array == 116
        assert win_lo == 113 and win_hi == 119
        assert sel == [0]  # 只有偏移 5（array 116）在邻域内，横扫帧 121 出局

    def test_select_best_prefers_anchor_window_over_far_sweep(self):
        """锚点邻域内选帧：全局横扫帧 score 最高也被邻域排除。"""
        candidates = [0, 1, 2, 4]
        cand_indices = [10, 11, 12, 13, 20]
        scores = [0.5, 0.6, 0.7, 1.0]  # 横扫帧（array 20）全局最高
        shaft_ys = {1: 300.0, 2: 340.0, 4: 200.0}
        nb = impact_refiner._anchor_neighborhood(
            candidates, cand_indices, shaft_ys, 8, 22, 3
        )
        assert nb is not None
        sel, _, _, _ = nb
        best_offset, _best_k, best_score = impact_refiner._select_best(
            candidates, scores, shaft_ys, sel
        )
        assert best_offset == 2  # 邻域内最优（array 12），非全局横扫帧 20
        assert best_score == 0.7

    def test_select_best_full_window_without_anchor(self):
        """回退路径：M2 不可用 / 开关关闭 -> 全候选集按 score 选（横扫帧胜出）。"""
        candidates = [0, 1, 2, 4]
        scores = [0.5, 0.6, 0.7, 1.0]
        best_offset, _best_k, best_score = impact_refiner._select_best(
            candidates, scores, {}, list(range(len(candidates)))
        )
        assert best_offset == 4
        assert best_score == 1.0

    def test_select_best_tie_breaker_prefers_lower_shaft(self):
        """平票 tie-breaker：都有杆身时优先杆头更贴地（y 更大）。"""
        candidates = [0, 1]
        scores = [0.5, 0.5]
        shaft_ys = {0: 300.0, 1: 350.0}
        best_offset, _best_k, _best_score = impact_refiner._select_best(
            candidates, scores, shaft_ys, [0, 1]
        )
        assert best_offset == 1  # y 350 > 300，更贴地

    def test_select_best_empty_selection(self):
        """空 selection -> (-1, -1, 0.0)（调用方 G0）。"""
        assert impact_refiner._select_best([0, 1], [0.5, 0.6], {}, []) == (
            -1, -1, 0.0,
        )

    def test_refine_anchor_switch_off_falls_back(self, tmp_path, video_meta, monkeypatch):
        """CLUBLITE_USE_ANCHOR=False 时走全窗口逻辑（合成视频仍校正成功）。"""
        monkeypatch.setattr(config, "CLUBLITE_USE_ANCHOR", False)
        path = _write_club_video(str(tmp_path / "club_noanchor.mp4"))
        frames = make_swing_frames()
        events = _swing_events()
        signals = _swing_signals()
        result = impact_refiner.refine_impact(
            path, frames, events, signals, CameraView.FACE_ON, video_meta,
        )
        assert result.available
        assert abs(result.new_array_index - T_CONTACT) <= 1

    def test_anchor_window_credible_accepts_competitive_window(self):
        """锚点邻域最优与全窗口最优相当 -> 可信（新样本 22030124 场景）。"""
        scores = [0.6063, 0.6437]
        assert impact_refiner._anchor_window_credible(scores, [0], 0.7)
        # 邻域含全窗口最优 -> ratio=1.0，必然可信（no-op 样本）
        assert impact_refiner._anchor_window_credible(scores, [0, 1], 0.7)

    def test_anchor_window_credible_rejects_weak_anchor(self):
        """锚点邻域最优远低于全窗口最优 -> 不可信，回退（假锚点守卫）。"""
        # 0bb16a97: 邻域 0.07 vs 全窗口 0.618（ratio 0.11）
        assert not impact_refiner._anchor_window_credible([0.0698, 0.6179], [0], 0.7)
        # a4fba3d2: 邻域 0.013 vs 全窗口 0.035（ratio 0.36）
        assert not impact_refiner._anchor_window_credible([0.0126, 0.0348], [0], 0.7)
        # 1446d1b9: 邻域 0.27 vs 全窗口 0.49（ratio 0.55）
        assert not impact_refiner._anchor_window_credible([0.2702, 0.4897], [0], 0.7)

    def test_anchor_window_credible_empty_or_zero(self):
        """空邻域 / 全零得分 -> 不可信。"""
        assert not impact_refiner._anchor_window_credible([0.5], [], 0.7)
        assert not impact_refiner._anchor_window_credible([0.0, 0.0], [0], 0.7)


# ---------------------------------------------------------------------------
# QA P1 回归：reanchor 后所有事件帧 ∈ 解码帧集（无渲染 fallback）
# ---------------------------------------------------------------------------


class TestDecodeCoverage:
    """解码并集覆盖 reanchor 全部可能产出的事件帧（opens=1 修复）。"""

    def test_plan_reanchor_frames_covers_all_candidates(self, video_meta):
        """窗口候选及其偏移调整帧（v2）reanchor 的 8 事件帧都应在解码并集内。

        v2 说明：CLUBLITE_IMPACT_OFFSET=-1 让实际校正下标可能落在
        ``cand_indices[best] - 1``（候选前 1 采样帧），plan_reanchor_frames
        的搜索集已扩展覆盖该调整目标 —— 本用例对「候选 ∪ 候选-1」逐一验证。
        """
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
        # 实际校正只可能落在「窗口候选」或「候选前 1 采样帧」（偏移目标）上
        search_indices: set = set()
        for cand_frame in cand:
            array_index = index_to_array.get(cand_frame)
            if array_index is None:
                continue
            search_indices.add(array_index)
            if array_index - 1 >= 0:
                search_indices.add(array_index - 1)
        for array_index in sorted(search_indices):
            rebuilt = segmenter.reanchor_impact(
                frames, signals, events, array_index
            )
            if rebuilt is None:
                continue
            for e in rebuilt:
                assert e.frame_index in union, (
                    f"下标 {array_index} 的 reanchor 事件帧 "
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


# ---------------------------------------------------------------------------
# 2026-09-04 新增：物理窗口守卫
# ---------------------------------------------------------------------------


class TestPhysicalGuard:
    """下杆上界 + 送杆下界守卫。

    合成挥杆：top=42、impact（locate_impact 原值）=51、finish=73、fps=30。
    refine 目标 = ``T_CONTACT``(57) + ``CLUBLITE_IMPACT_OFFSET``(-1) = **56**。

    - 下杆 (56-42)/30 = **0.467s** > 0.40s 上限 -> 下杆守卫应拒绝
    - 送杆 (73-56)/30 = 0.567s > 0.25s 下限 -> 送杆守卫放行

    .. note::
       本类的两个守卫共用 ``refine_impact`` 里同一段判定
       （``if not (lower_ok and follow_ok and down_ok)``）。这里用合成数据
       覆盖**下杆上界**；**送杆下界**依赖 finish 与 refine 目标贴近的样本，
       由真实素材端到端验证（横屏 impact=38 / finish=41，送杆仅 0.10s）。
    """

    def _refine(self, tmp_path, video_meta, name):
        path = _write_club_video(str(tmp_path / name))
        return impact_refiner.refine_impact(
            path,
            make_swing_frames(),
            _swing_events(),
            _swing_signals(),
            CameraView.FACE_ON,
            video_meta,
        )

    def test_downstroke_upper_bound_rejects(self, tmp_path, video_meta, monkeypatch):
        """下杆 0.467s 超过 0.40s 硬上限 -> G0，保持 locate_impact 原值。"""
        monkeypatch.setattr(config, "CLUBLITE_MAX_DOWNSTROKE_SEC", 0.40)
        monkeypatch.setattr(config, "CLUBLITE_MIN_FOLLOW_THROUGH_SEC", 0.25)
        result = self._refine(tmp_path, video_meta, "guard_reject.mp4")
        assert not result.available
        assert result.method == "none"
        # 被拒前算出的目标帧仍保留在结果里，便于线上排查
        assert result.new_array_index == 56
        # 拒绝 = 保持 locate_impact 原值，不做任何位移
        assert result.old_array_index == 51

    def test_guard_off_restores_refine(self, tmp_path, video_meta, monkeypatch):
        """守卫放宽到不生效时，恢复原有 refine 行为（回归保护）。"""
        monkeypatch.setattr(config, "CLUBLITE_MAX_DOWNSTROKE_SEC", 999.0)
        monkeypatch.setattr(config, "CLUBLITE_MIN_FOLLOW_THROUGH_SEC", 0.0)
        result = self._refine(tmp_path, video_meta, "guard_off.mp4")
        assert result.available
        assert result.new_array_index == 56
        assert result.delta_frames == 5


# ---------------------------------------------------------------------------
# M3：fresh _detect_hough 杆头最低点击球帧校正（2026-09-04 用户拍板「方案 A」）
# ---------------------------------------------------------------------------


class TestM3FreshAnchor:
    """M3：以「杆头离地面最近的点」直接作为击球帧。

    与 M2 全窗口化（``_shaft_scan_window`` / 简化 ``_shaft_lowest_y``）的
    本质区别：改用路径 A 的完整 ``_detect_hough``（ROI + 扇形 + body_mask +
    skeleton 四道过滤），并以 fresh 模式（fan/dir_tol 拉满）逐帧独立检测。
    """

    @staticmethod
    def _line_frame(grip, head):
        """画一张 400×600 图，含 grip->head 的竖直杆身线。"""
        img = np.full((400, 600, 3), 70, dtype=np.uint8)
        cv2.line(
            img,
            (int(grip[0]), int(grip[1])),
            (int(head[0]), int(head[1])),
            (60, 60, 255),
            6,
            cv2.LINE_AA,
        )
        return img

    @staticmethod
    def _mk_frame(frame_index, grip_px, w=600, h=400):
        """构造 FrameLandmarks，双腕中点 = grip_px。"""
        norm = np.zeros((geometry.NUM_LANDMARKS, 3), dtype=np.float64)
        gx, gy = grip_px[0] / w, grip_px[1] / h
        norm[geometry.L_WRIST] = [gx, gy, 0.0]
        norm[geometry.R_WRIST] = [gx, gy, 0.0]
        return FrameLandmarks(
            frame_index=frame_index,
            timestamp=frame_index / 30.0,
            detected=True,
            norm=norm,
            world=np.zeros((geometry.NUM_LANDMARKS, 3), dtype=np.float64),
            visibility=np.ones(geometry.NUM_LANDMARKS, dtype=np.float64),
        )

    def test_scan_window_fresh_empty(self):
        assert (
            impact_refiner._shaft_scan_window_fresh(
                [], {}, [], [], 600, 400, 200.0, CameraView.FACE_ON
            )
            == {}
        )

    def test_scan_window_fresh_detects_shaft(self):
        """fresh 扫描能检出杆身线，杆头端点 y 落在握把下方（y 更大）。"""
        grip = np.array([300.0, 150.0])
        head = np.array([300.0, 330.0])
        frames = [self._mk_frame(100, grip)]
        frames_bgr = {100: self._line_frame(grip, head)}
        result = impact_refiner._shaft_scan_window_fresh(
            [100], frames_bgr, frames, [0], 600, 400, 200.0,
            CameraView.FACE_ON,
        )
        assert 0 in result
        assert result[0] > grip[1], "杆头应在握把下方"
        assert abs(result[0] - head[1]) < 25

    def test_lowest_point_disabled_returns_none(self, tmp_path, video_meta, monkeypatch):
        """显式关闭 M3 -> 返回 None（调用方保持原值）。"""
        monkeypatch.setattr(config, "CLUBLITE_M3_FRESH_ANCHOR", False)
        path = _write_club_video(str(tmp_path / "m3_off.mp4"))
        result = impact_refiner.refine_impact_lowest_point(
            path, make_swing_frames(), _swing_events(), _swing_signals(),
            CameraView.FACE_ON, video_meta,
        )
        assert result is None

    def test_lowest_point_orchestrates(self, tmp_path, video_meta, monkeypatch):
        """M3 编排（mock 扫描/挑锚）：最低点 -> 返回校正后下标。

        合成视频的「杆」贴着腿、长度与杆长先验不符，会被 ``_detect_hough`` 的
        骨架共线过滤误杀（真实 11.mp4 上 49/49 命中，见 .workbuddy 探针），
        故这里 mock 掉扫描与挑锚两步，专测编排：窗口规划 -> 扫描 -> 挑锚 ->
        物理守卫 -> 位移守卫 -> 返回。
        """
        monkeypatch.setattr(config, "CLUBLITE_M3_FRESH_ANCHOR", True)
        path = _write_club_video(str(tmp_path / "m3_mock.mp4"))
        frames = make_swing_frames()
        events = _swing_events()
        signals = _swing_signals()
        impact = next(e for e in events if e.key is PhaseKey.IMPACT)

        monkeypatch.setattr(
            impact_refiner, "_shaft_scan_window_fresh",
            lambda *a, **k: {0: 100.0, 1: 200.0, 2: 300.0, 3: 900.0},
        )
        # 最低点放偏移 3（array = 窗口起点 49 + 3 = 52 = impact + 1）
        monkeypatch.setattr(
            impact_refiner, "_pick_full_window_anchor",
            lambda shaft_ys, raw_motion, ratio: 3,
        )
        result = impact_refiner.refine_impact_lowest_point(
            path, frames, events, signals, CameraView.FACE_ON, video_meta,
        )
        assert result == impact.array_index + 1

    def test_lowest_point_min_shift_guard(self, tmp_path, video_meta, monkeypatch):
        """M3 位移守卫：delta=0（最低点 == 原 impact）-> None（不采纳）。"""
        monkeypatch.setattr(config, "CLUBLITE_M3_FRESH_ANCHOR", True)
        path = _write_club_video(str(tmp_path / "m3_shift.mp4"))
        frames = make_swing_frames()
        events = _swing_events()
        signals = _swing_signals()
        impact = next(e for e in events if e.key is PhaseKey.IMPACT)
        monkeypatch.setattr(
            impact_refiner, "_shaft_scan_window_fresh",
            lambda *a, **k: {2: 300.0},
        )
        monkeypatch.setattr(
            impact_refiner, "_pick_full_window_anchor",
            lambda shaft_ys, raw_motion, ratio: 2,
        )
        result = impact_refiner.refine_impact_lowest_point(
            path, frames, events, signals, CameraView.FACE_ON, video_meta,
        )
        # 偏移 2 -> array 51 == impact，delta=0 < MIN_SHIFT -> None
        assert result is None

    def test_lowest_point_never_raises(self, tmp_path, video_meta, monkeypatch):
        """模块级硬约束：M3 开关开启时坏输入也不外抛异常。"""
        monkeypatch.setattr(config, "CLUBLITE_M3_FRESH_ANCHOR", True)
        path = str(tmp_path / "bad.mp4")
        with open(path, "wb") as fh:
            fh.write(b"not a real video")
        result = impact_refiner.refine_impact_lowest_point(
            path, make_swing_frames(), _swing_events(), _swing_signals(),
            CameraView.FACE_ON, video_meta,
        )
        assert result is None


class TestRefineAnchorNeighborhood:
    """方案 B：几何锚点邻域 ±1 精修（_refine_anchor_neighborhood）。

    门控判据：锚点 y 优势（margin）≥ 门槛 → 信几何保持锚点；否则最低点落在
    两帧之间，用邻域 ±1 motion 峰精修。门槛 = y_margin_ratio × club_len_px。
    """

    def test_strong_geometry_keeps_anchor(self):
        """几何信号强（margin ≥ 门槛）→ 保持锚点，不被邻域 motion 峰带偏。

        对应 11.mp4 / 470057ac：锚点已命中真值，motion 峰反而偏 1 帧。
        """
        shaft_ys = {3: 900.0, 2: 800.0, 4: 810.0}
        raw_motion = np.array([0.0, 1.0, 2.0, 3.0, 9.9, 1.0])  # 峰在 off=4
        off = impact_refiner._refine_anchor_neighborhood(
            3, shaft_ys, raw_motion, club_len_px=200.0, y_margin_ratio=0.20,
        )
        # margin = 900 - 810 = 90 ≥ 0.20*200 = 40 → 保持 3
        assert off == 3

    def test_weak_geometry_refines_to_motion_peak(self):
        """几何信号弱（margin < 门槛）→ 邻域 ±1 motion 峰精修。

        对应 c6f67f38：杆头最低点 178 偏早 1 帧，击球瞬间 179 由运动峰表征。
        """
        shaft_ys = {3: 835.0, 2: 798.0, 4: 819.0}
        raw_motion = np.array([0.0, 1.0, 4.7, 9.1, 9.3, 1.0])  # 峰在 off=4
        off = impact_refiner._refine_anchor_neighborhood(
            3, shaft_ys, raw_motion, club_len_px=200.0, y_margin_ratio=0.20,
        )
        # margin = 835 - 819 = 16 < 40 → 精修到 off=4
        assert off == 4

    def test_off_missing_in_shaft_ys_returns_off(self):
        """锚点不在 shaft_ys 中 → 原样返回（不越界）。"""
        raw_motion = np.array([1.0, 2.0, 3.0])
        assert (
            impact_refiner._refine_anchor_neighborhood(
                1, {2: 100.0}, raw_motion, 200.0, 0.20,
            )
            == 1
        )

    def test_no_neighbor_detection_returns_off(self):
        """邻域帧无杆检测 → 保守保持锚点（不做无依据的精修）。"""
        raw_motion = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        assert (
            impact_refiner._refine_anchor_neighborhood(
                2, {2: 500.0}, raw_motion, 200.0, 0.20,
            )
            == 2
        )

    def test_boundary_clamp(self):
        """off=0 边界：邻域 clamp 到 [0,1]，motion 峰不越界。"""
        shaft_ys = {0: 500.0, 1: 499.0}
        raw_motion = np.array([0.0, 5.0, 9.0])  # 峰在 off=2（越界，应被 clamp）
        off = impact_refiner._refine_anchor_neighborhood(
            0, shaft_ys, raw_motion, club_len_px=200.0, y_margin_ratio=0.20,
        )
        # margin = 500 - 499 = 1 < 40 → 精修；邻域 [0,1] argmax=1 → off=1
        assert off == 1


# ---------------------------------------------------------------------------
# Pipeline helper：M3 在 SwingNet / 规则引擎两条路径上的统一入口
# （2026-09-04 接线：M3 优先于 CLUBLITE 主流程）
# ---------------------------------------------------------------------------


class TestPipelineM3Hook:
    """``pipeline._try_m3_lowest_point``：M3 在两条路径上的复用 helper。

    设计约束（2026-09-04 用户拍板）：
    - 命中（M3 函数返 idx + reanchor 返 events）→ 返回新 events；
    - 任一步失败（M3 返回 None / reanchor 返回 None）→ 返回 None（pipeline 走 fallback）；
    - 任何异常被吞，返回 None（不阻断主链路）。
    """

    def test_helper_returns_new_events_on_success(self, monkeypatch):
        """M3 命中 + reanchor 成功 -> 返回新 events。"""
        from app import pipeline

        new_events_obj = object()

        monkeypatch.setattr(
            impact_refiner, "refine_impact_lowest_point",
            lambda *a, **k: 100,
        )
        monkeypatch.setattr(
            segmenter, "reanchor_impact",
            lambda *a, **k: new_events_obj,
        )

        result = pipeline._try_m3_lowest_point(
            video_path="", frames=[], events=[],
            signals=object(), view=CameraView.DOWN_THE_LINE, meta=object(),
            frames_bgr={}, task_id="t1",
        )
        assert result is new_events_obj

    def test_helper_returns_none_when_lowest_point_fails(self, monkeypatch):
        """M3 函数返 None -> helper 返 None（不调 reanchor）。"""
        from app import pipeline

        reanchor_called = {"n": 0}
        monkeypatch.setattr(
            impact_refiner, "refine_impact_lowest_point",
            lambda *a, **k: None,
        )
        monkeypatch.setattr(
            segmenter, "reanchor_impact",
            lambda *a, **k: (reanchor_called.__setitem__("n", reanchor_called["n"] + 1) or object()),
        )

        result = pipeline._try_m3_lowest_point(
            video_path="", frames=[], events=[],
            signals=object(), view=CameraView.DOWN_THE_LINE, meta=object(),
            frames_bgr={}, task_id="t1",
        )
        assert result is None
        assert reanchor_called["n"] == 0

    def test_helper_returns_none_when_reanchor_fails(self, monkeypatch):
        """M3 命中但 reanchor 返 None -> helper 返 None。"""
        from app import pipeline

        monkeypatch.setattr(
            impact_refiner, "refine_impact_lowest_point",
            lambda *a, **k: 100,
        )
        monkeypatch.setattr(
            segmenter, "reanchor_impact",
            lambda *a, **k: None,
        )

        result = pipeline._try_m3_lowest_point(
            video_path="", frames=[], events=[],
            signals=object(), view=CameraView.DOWN_THE_LINE, meta=object(),
            frames_bgr={}, task_id="t1",
        )
        assert result is None

    def test_helper_swallows_exceptions(self, monkeypatch):
        """任何异常被吞 -> helper 返 None（不阻断 pipeline）。"""
        from app import pipeline

        def _boom(*a, **k):
            raise RuntimeError("M3 internal boom")
        monkeypatch.setattr(
            impact_refiner, "refine_impact_lowest_point", _boom,
        )

        result = pipeline._try_m3_lowest_point(
            video_path="", frames=[], events=[],
            signals=object(), view=CameraView.DOWN_THE_LINE, meta=object(),
            frames_bgr={}, task_id="t1",
        )
        assert result is None

    def test_helper_skips_face_on(self, monkeypatch):
        """face-on 机位 -> helper 直接返 None（不调 M3，保持 CLUBLITE 主流程）。"""
        from app import pipeline

        m3_called = {"n": 0}
        def _spy(*a, **k):
            m3_called["n"] += 1
            return 100
        monkeypatch.setattr(
            impact_refiner, "refine_impact_lowest_point", _spy,
        )
        monkeypatch.setattr(
            segmenter, "reanchor_impact", lambda *a, **k: object(),
        )

        result = pipeline._try_m3_lowest_point(
            video_path="", frames=[], events=[],
            signals=object(), view=CameraView.FACE_ON, meta=object(),
            frames_bgr={}, task_id="t1",
        )
        assert result is None
        assert m3_called["n"] == 0

    def test_helper_runs_on_dtl(self, monkeypatch):
        """DTL 机位 -> helper 走 M3（不因机位被 gate 掉）。"""
        from app import pipeline

        new_events_obj = object()
        monkeypatch.setattr(
            impact_refiner, "refine_impact_lowest_point",
            lambda *a, **k: 100,
        )
        monkeypatch.setattr(
            segmenter, "reanchor_impact",
            lambda *a, **k: new_events_obj,
        )

        result = pipeline._try_m3_lowest_point(
            video_path="", frames=[], events=[],
            signals=object(), view=CameraView.DOWN_THE_LINE, meta=object(),
            frames_bgr={}, task_id="t1",
        )
        assert result is new_events_obj
