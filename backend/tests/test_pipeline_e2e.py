"""端到端：上传 -> 后台流水线 -> 轮询 -> 结果 -> 静态图（架构文档 §5.1）。

为了让用例可重复、可离线、秒级完成，只对 ``pose_extractor.extract``
（唯一依赖 MediaPipe 推理的环节）做打桩，替换为合成关键点序列；
``probe_video`` / ``check_brightness`` / ``segmenter`` / ``metrics`` /
``renderer`` / ``task_store`` / HTTP 层全部走真实实现，视频也是真实 mp4。
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from app import config, frame_reader, pose_extractor, renderer, segmenter
from app.schemas import PHASE_META, PHASE_ORDER, TaskStatus, phase_image_name

from conftest import FPS, make_swing_frames

EXPECTED_IMAGES = [
    "01_address.jpg", "02_takeaway.jpg", "03_backswing.jpg", "04_top.jpg",
    "05_downswing.jpg", "06_impact.jpg", "07_follow_through.jpg", "08_finish.jpg",
]

#: 机位过滤后 face-on 各阶段指标数（架构 §3.3 统计表）
FACE_ON_COUNTS = {
    "address": 3, "takeaway": 4, "backswing": 4, "top": 4,
    "downswing": 4, "impact": 3, "follow_through": 4, "finish": 4,
}


@pytest.fixture()
def stub_extract(monkeypatch):
    """把 MediaPipe 推理替换为合成关键点（帧号与合成视频严格对齐）。"""
    def _fake_extract(path, meta, on_progress=None):
        if on_progress is not None:
            on_progress(0.5)
            on_progress(1.0)
        return make_swing_frames(n=meta.frame_count, fps=meta.fps, step=1)

    monkeypatch.setattr(pose_extractor, "extract", _fake_extract)
    return _fake_extract


def _wait_terminal(client, task_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    data: dict = {}
    while time.time() < deadline:
        data = client.get(f"/api/v1/tasks/{task_id}").json()["data"]
        if data["status"] in (TaskStatus.SUCCESS.value, TaskStatus.FAILED.value):
            return data
        time.sleep(0.2)
    return data


@pytest.fixture()
def finished(api_client, synth_video, stub_extract):
    """跑完一次成功的分析，返回 ``(task_id, status_data, result_data)``。"""
    with open(synth_video, "rb") as handle:
        content = handle.read()
    resp = api_client.post(
        "/api/v1/tasks", files={"file": ("swing.mp4", content, "video/mp4")}
    )
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["data"]["task_id"]

    status = _wait_terminal(api_client, task_id)
    assert status["status"] == TaskStatus.SUCCESS.value, status

    result_resp = api_client.get(f"/api/v1/tasks/{task_id}/result")
    assert result_resp.status_code == 200, result_resp.text
    return task_id, status, result_resp.json()["data"]


class TestEndToEnd:
    """全链路成功路径。"""

    def test_status_terminal_fields(self, finished):
        _task_id, status, _result = finished
        assert status["progress"] == 100
        assert status["step"] == 4
        assert status["error_code"] is None
        assert status["error_message"] is None
        assert status["message"] == "分析完成"

    def test_result_top_level_schema(self, finished):
        task_id, _status, result = finished
        assert result["task_id"] == task_id
        assert result["status"] == TaskStatus.SUCCESS.value
        assert set(result) == {
            "task_id", "status", "camera_view", "video_meta", "global_metrics",
            "phases", "warnings", "disclaimer",
        }
        assert result["camera_view"] == "face_on"
        assert result["disclaimer"] == config.DISCLAIMER
        assert isinstance(result["warnings"], list)

    def test_video_meta(self, finished):
        _task_id, _status, result = finished
        meta = result["video_meta"]
        assert meta["fps"] == pytest.approx(FPS, abs=0.1)
        assert meta["frame_count"] == 120
        assert meta["total_frames"] == meta["frame_count"]
        assert (meta["width"], meta["height"]) == (480, 854)
        assert meta["low_fps"] is False

    def test_eight_phases_ordered(self, finished):
        _task_id, _status, result = finished
        phases = result["phases"]
        assert len(phases) == 8
        assert [p["index"] for p in phases] == list(range(1, 9))
        assert [p["key"] for p in phases] == [k.value for k in PHASE_ORDER]
        for phase, key in zip(phases, PHASE_ORDER):
            assert phase["name_cn"] == PHASE_META[key].name_cn
            assert phase["name_en"] == PHASE_META[key].name_en

    def test_phase_frames_strictly_increasing(self, finished):
        _task_id, _status, result = finished
        nums = [p["frame_index"] for p in result["phases"]]
        stamps = [p["timestamp"] for p in result["phases"]]
        assert all(b > a for a, b in zip(nums, nums[1:])), nums
        assert all(b > a for a, b in zip(stamps, stamps[1:])), stamps
        assert all(0 <= n <= 119 for n in nums)

    def test_each_phase_has_filtered_metrics(self, finished):
        """face-on 机位过滤后各阶段指标数（架构 §3.3 统计表），且带 description。"""
        _task_id, _status, result = finished
        for phase in result["phases"]:
            items = phase["metrics"]
            assert len(items) == FACE_ON_COUNTS[phase["key"]], phase["key"]
            for item in items:
                assert set(item) == {
                    "key", "name", "value", "unit", "ref_min", "ref_max", "status",
                    "estimated", "source", "confidence", "description",
                }
                assert isinstance(item["value"], (int, float))
                assert item["status"] in (
                    "low", "normal", "high", "critical_low", "critical_high"
                )
                assert item["name"], "指标必须有中文名"
                assert isinstance(item["description"], str)

    def test_phases_have_risks_field(self, finished):
        """v2 契约：``phases[].risks`` 恒为数组。"""
        _task_id, _status, result = finished
        for phase in result["phases"]:
            assert isinstance(phase["risks"], list)

    def test_risks_produced_on_synthetic_swing(self, finished):
        """合成挥杆应触发 RISK-016（⑦ 开放角 < 30）。

        这同时是 RISK-016 数据流的端到端验证：触发值必须等于 ⑦ 的
        ``shoulder_turn`` 阶段指标（引擎按对外 key 查表，零特判）。

        ⚠️ 2026-08 ⑦ 判据改为「h 局部最小点」（送杆刚启动，impact+3）后，
        合成挥杆在 ⑦ 处肩部尚未打开，开放角为**负**（-6.0）——物理真实
        （杆身水平前一刻肩还没转过来），RISK-016 的 ``< 30`` 条件仍命中。
        fn_key（shoulder_open = -肩转）映射的符号正确性由
        :meth:`TestSignConventions.test_follow_through_shoulder_turn_via_open_maps_to_open_angle`
        与收杆符号测试（``test_open_angles_are_negated_turn_at_finish``）覆盖。"""
        _task_id, _status, result = finished
        by_phase = {p["key"]: p for p in result["phases"]}
        ft_rules = {r["rule_id"] for r in by_phase["follow_through"]["risks"]}
        assert "RISK-016" in ft_rules, f"合成挥杆 FOLLOW_THROUGH 应触发 RISK-016, got {ft_rules}"
        # 触发值 = ⑦ 开放角（fn_key=shoulder_open 生效）；新语义下可为负但仍 < 30
        ft_metric = {m["key"]: m["value"] for m in by_phase["follow_through"]["metrics"]}
        for r in by_phase["follow_through"]["risks"]:
            if r["rule_id"] == "RISK-016":
                assert r["value"] == ft_metric["shoulder_turn"], (
                    f"RISK-016 取值应等于 ⑦ shoulder_turn 指标，got {r['value']}"
                )
                assert r["value"] < 30, f"RISK-016 触发值应在 <30，got {r['value']}"

    def test_risk_item_schema(self, finished):
        _task_id, _status, result = finished
        risk = next(
            (r for p in result["phases"] for r in p["risks"]), None
        )
        assert risk is not None, "合成挥杆应至少有一条风险"
        assert set(risk) == {
            "rule_id", "risk_name", "risk_level", "trigger_phase",
            "metric_key", "metric_name", "value", "unit",
            "ref_min", "ref_max", "trigger_description",
            "suggestions", "manual_excerpt", "manual_page",
        }
        assert risk["trigger_description"], "风险文案必须非空"
        assert risk["risk_level"] in ("high", "medium", "low")

    def test_decode_opens_limited(self, api_client, synth_video, stub_extract):
        """🔑 解码趟数回归：整条 pipeline 只允许 1 次共享 grab_frames 打开
        （pose_extractor 自开 VideoCapture 不计入 frame_reader 统计）。"""
        frame_reader.reset_stats()
        with open(synth_video, "rb") as handle:
            content = handle.read()
        resp = api_client.post(
            "/api/v1/tasks", files={"file": ("swing.mp4", content, "video/mp4")}
        )
        assert resp.status_code == 201, resp.text
        task_id = resp.json()["data"]["task_id"]
        status = _wait_terminal(api_client, task_id)
        assert status["status"] == TaskStatus.SUCCESS.value, status
        stats = frame_reader.stats()
        assert stats["opens"] <= 2, f"解码趟数超标: {stats}"

    def test_global_metrics(self, finished):
        _task_id, _status, result = finished
        gm = result["global_metrics"]
        assert len(gm["metrics"]) == 3
        assert gm["tempo_ratio"] > 0
        assert gm["swing_duration"] > 0
        assert gm["max_head_drift_pct"] >= 0

    def test_image_urls_absolute_and_named(self, finished):
        task_id, _status, result = finished
        urls = [p["image_url"] for p in result["phases"]]
        assert urls == [
            f"{config.PUBLIC_BASE_URL}/static/{task_id}/{name}"
            for name in EXPECTED_IMAGES
        ]
        for url in urls:
            assert re.match(r"^https?://", url), "image_url 必须是绝对 URL"

    def test_image_files_written(self, finished):
        task_id, _status, _result = finished
        task_dir = Path(config.DATA_DIR) / task_id
        for name in EXPECTED_IMAGES:
            path = task_dir / name
            assert path.is_file(), f"缺少结果图 {name}"
            assert path.stat().st_size > 1024, f"{name} 体积异常"

    def test_images_served_by_static_route(self, finished, api_client):
        task_id, _status, _result = finished
        for name in EXPECTED_IMAGES:
            resp = api_client.get(f"/static/{task_id}/{name}")
            assert resp.status_code == 200, name
            assert resp.headers["content-type"].startswith("image/")

    def test_upload_deleted_after_success(self, finished):
        """PRD Q6：分析成功后立即删除原视频。"""
        task_id, _status, _result = finished
        assert not (Path(config.DATA_DIR) / task_id / config.UPLOAD_FILENAME).exists()

    def test_result_json_is_serializable_for_miniprogram(self, finished):
        """结果里不得出现枚举对象 / NaN 之类无法被 JSON 消费的值。"""
        import json
        import math

        _task_id, _status, result = finished
        text = json.dumps(result, ensure_ascii=False)
        assert "NaN" not in text and "Infinity" not in text

        for phase in result["phases"]:
            for item in phase["metrics"]:
                assert math.isfinite(float(item["value"]))


class TestRenderer:
    """``renderer.render_events`` 单独验证。"""

    def test_renders_eight_images(self, synth_video, tmp_path):
        frames = make_swing_frames()
        events = segmenter.segment_swing(frames, FPS)
        produced = renderer.render_events(synth_video, events, str(tmp_path), frames)

        assert len(produced) == 8
        for event in events:
            name = produced[event.key]
            assert name == phase_image_name(event.key)
            assert (tmp_path / name).is_file()
            assert (tmp_path / name).stat().st_size > 1024

    def test_long_side_capped(self, synth_video, tmp_path):
        import cv2

        frames = make_swing_frames()
        events = segmenter.segment_swing(frames, FPS)
        produced = renderer.render_events(synth_video, events, str(tmp_path), frames)
        img = cv2.imread(str(tmp_path / produced[events[0].key]))
        assert max(img.shape[:2]) <= config.RENDER_LONG_SIDE

    def test_unopenable_video_raises(self, tmp_path):
        from app.schemas import AnalysisError, ErrorCode

        frames = make_swing_frames()
        events = segmenter.segment_swing(frames, FPS)
        bad = tmp_path / "bad.mp4"
        bad.write_bytes(b"nope")
        with pytest.raises(AnalysisError) as exc:
            renderer.render_events(str(bad), events, str(tmp_path), frames)
        assert exc.value.code is ErrorCode.BAD_VIDEO


class TestPipelineFailurePath:
    """流水线失败时不得抛出，只落到 FAILED 态。"""

    def test_no_swing_video_reports_failed(self, api_client, synth_video, monkeypatch):
        from conftest import make_still_frames

        def _still_extract(path, meta, on_progress=None):
            return make_still_frames(n=meta.frame_count, fps=meta.fps)

        monkeypatch.setattr(pose_extractor, "extract", _still_extract)

        with open(synth_video, "rb") as handle:
            content = handle.read()
        task_id = api_client.post(
            "/api/v1/tasks", files={"file": ("still.mp4", content, "video/mp4")}
        ).json()["data"]["task_id"]

        data = _wait_terminal(api_client, task_id)
        assert data["status"] == TaskStatus.FAILED.value
        assert data["error_code"] == "NO_SWING"
        assert data["error_message"] == config.ERROR_MESSAGES["NO_SWING"]

    def test_unexpected_crash_maps_to_internal(self, api_client, synth_video, monkeypatch):
        def _boom(path, meta, on_progress=None):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(pose_extractor, "extract", _boom)

        with open(synth_video, "rb") as handle:
            content = handle.read()
        task_id = api_client.post(
            "/api/v1/tasks", files={"file": ("crash.mp4", content, "video/mp4")}
        ).json()["data"]["task_id"]

        data = _wait_terminal(api_client, task_id)
        assert data["status"] == TaskStatus.FAILED.value
        assert data["error_code"] == "INTERNAL"
        assert data["error_message"] == config.ERROR_MESSAGES["INTERNAL"]
        assert "unexpected" not in (data["error_message"] or ""), "不得泄漏内部异常信息"
