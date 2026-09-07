"""HTTP 接口集成测试（架构 ARCHITECTURE.md §4 + ARCHITECTURE-v2.md §6 接口契约）。

上传用例统一使用 ``.tools/_probe/t.mp4``（1.0s 匀速灰阶合成视频），
它会在后台流水线里被 ``probe_video`` 判为 ``BAD_VIDEO``（时长 < 1.5s），
既能覆盖失败链路，又不会加载 MediaPipe 模型、执行很快。

v2 覆盖：PDD 双路径注册、错误码映射（10001/10002/10003/10005/20001/20002）、
``video``/``file`` 双字段名、``camera_view`` 缺省与非法值、legacy 码回滚开关。
"""

from __future__ import annotations

import json
import os
import re
import time

import pytest

from app import config
from app.schemas import CameraView, ErrorCode, TaskStatus
from app.task_store import task_store

PROBE_MP4 = r"E:\project\golf\.tools\_probe\t.mp4"


@pytest.fixture(scope="session")
def probe_bytes() -> bytes:
    """读取合成测试视频。"""
    assert os.path.exists(PROBE_MP4), f"缺少测试素材: {PROBE_MP4}"
    with open(PROBE_MP4, "rb") as handle:
        return handle.read()


def create_task(client, content: bytes, name: str = "swing.mp4",
                ctype: str = "video/mp4", path: str = "/api/v1/tasks",
                field: str = "file", camera_view: str = None):
    """上传并返回响应。"""
    files = {field: (name, content, ctype)}
    data = {}
    if camera_view is not None:
        data["camera_view"] = camera_view
    return client.post(path, files=files, data=data)


def config_api() -> str:
    """接口前缀。"""
    return "/api/v1"


def wait_terminal(client, task_id: str, timeout: float = 30.0) -> dict:
    """轮询直到任务进入终态。"""
    deadline = time.time() + timeout
    payload: dict = {}
    while time.time() < deadline:
        resp = client.get(f"{config_api()}/tasks/{task_id}")
        assert resp.status_code == 200
        payload = resp.json()["data"]
        if payload["status"] in (TaskStatus.SUCCESS.value, TaskStatus.FAILED.value):
            return payload
        time.sleep(0.2)
    return payload


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------


class TestHealth:
    """``GET /api/v1/health``。"""

    def test_health_ok(self, api_client):
        resp = api_client.get("/api/v1/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        assert body["message"] == "success"
        assert body["data"]["status"] == "ok"
        assert body["data"]["mediapipe"] == "0.10.14"

    def test_declared_version_matches_installed(self):
        """健康检查下发的版本必须和真实安装版本一致（环境硬约束）。"""
        import mediapipe

        assert mediapipe.__version__ == "0.10.14"
        assert config.MEDIAPIPE_VERSION == mediapipe.__version__

    def test_legacy_solutions_pose_available(self):
        """必须走 legacy ``mp.solutions.pose``，禁止 tasks API。"""
        import mediapipe as mp

        assert hasattr(mp.solutions, "pose")
        assert hasattr(mp.solutions.pose, "Pose")

    def test_cors_enabled(self, api_client):
        resp = api_client.get(
            "/api/v1/health", headers={"Origin": "http://localhost:5173"}
        )
        assert resp.headers.get("access-control-allow-origin") == "*"


# ---------------------------------------------------------------------------
# 创建任务
# ---------------------------------------------------------------------------


class TestCreateTask:
    """``POST /api/v1/tasks``（旧路径）与 ``POST /api/v1/task/create``（PDD 主路径）。"""

    @pytest.mark.parametrize("path", ["/api/v1/tasks", "/api/v1/task/create"])
    def test_create_success(self, api_client, probe_bytes, path):
        resp = create_task(api_client, probe_bytes, path=path)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["code"] == 0
        assert body["message"] == "success"
        task_id = body["data"]["task_id"]
        assert re.fullmatch(r"[0-9a-f]{12}", task_id), task_id
        assert body["data"]["status"] == TaskStatus.PENDING.value

    @pytest.mark.parametrize("path", ["/api/v1/tasks", "/api/v1/task/create"])
    def test_task_dir_created(self, api_client, probe_bytes, path):
        task_id = create_task(api_client, probe_bytes, path=path).json()["data"]["task_id"]
        assert (config.DATA_DIR / task_id).is_dir()

    @pytest.mark.parametrize("name", ["swing.avi", "swing.txt", "a.MP4.zip"])
    def test_reject_non_mp4_extension(self, api_client, probe_bytes, name):
        resp = create_task(api_client, probe_bytes, name=name)
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == config.PDD_CODE_BAD_FORMAT  # 10002
        assert body["data"] is None
        assert "mp4" in body["message"]

    def test_accept_mov_extension(self, api_client, probe_bytes):
        """PDD 放开 .mov（v2 契约变更）。"""
        resp = create_task(api_client, probe_bytes, name="swing.mov")
        assert resp.status_code == 201, resp.text

    def test_accept_uppercase_extension(self, api_client, probe_bytes):
        resp = create_task(api_client, probe_bytes, name="SWING.MP4")
        assert resp.status_code == 201

    def test_reject_bad_content_type(self, api_client, probe_bytes):
        resp = create_task(api_client, probe_bytes, name="swing.mp4", ctype="image/png")
        assert resp.status_code == 400
        assert resp.json()["code"] == config.PDD_CODE_BAD_FORMAT

    def test_reject_empty_file(self, api_client):
        resp = create_task(api_client, b"")
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == config.PDD_CODE_BAD_FORMAT
        assert "空" in body["message"]

    def test_reject_oversize(self, api_client):
        """> 40MB 必须 10001（PDD 文件过大）。"""
        oversize = b"\x00" * (config.MAX_UPLOAD_BYTES + 1024 * 1024)
        resp = create_task(api_client, oversize)
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == config.PDD_CODE_FILE_TOO_LARGE  # 10001
        assert "40MB" in body["message"]

    def test_rejected_upload_leaves_no_task(self, api_client, probe_bytes):
        """校验失败必须回滚任务目录，不留垃圾。"""
        before = set(os.listdir(config.DATA_DIR))
        create_task(api_client, probe_bytes, name="bad.avi")
        after = set(os.listdir(config.DATA_DIR))
        assert after == before

    def test_missing_file_field_returns_10002(self, api_client):
        """``video`` / ``file`` 双字段都缺失 -> 10002（格式不支持）。"""
        resp = api_client.post("/api/v1/tasks")
        assert resp.status_code == 400
        assert resp.json()["code"] == config.PDD_CODE_BAD_FORMAT


class TestFieldNameCompat:
    """``video``（PDD）为主、``file``（旧）兼容。"""

    def test_video_field_accepted(self, api_client, probe_bytes):
        resp = create_task(api_client, probe_bytes, field="video")
        assert resp.status_code == 201, resp.text

    def test_file_field_still_accepted(self, api_client, probe_bytes):
        resp = create_task(api_client, probe_bytes, field="file")
        assert resp.status_code == 201, resp.text

    def test_video_preferred_over_file(self, api_client, probe_bytes):
        """同时给两个字段时以 ``video`` 为准（不抛错即可）。"""
        files = {
            "video": ("a.mp4", probe_bytes, "video/mp4"),
            "file": ("b.mp4", probe_bytes, "video/mp4"),
        }
        resp = api_client.post("/api/v1/tasks", files=files)
        assert resp.status_code == 201, resp.text


class TestCameraView:
    """``camera_view`` 默认 AUTO；缺省/非法值按 AUTO 落值不硬拒（2026-09-04 改）。

    变更说明：之前默认 ``face_on`` 导致 DTL 视频被错判，ClubProbe 等机位相关
    链路永远不触发。改默认 AUTO 后由 :func:`app.view_detector.resolve` 自动
    判定（实测 9/9 命中），让真实 DTL 视频能自动识别。"""

    def test_default_auto(self, api_client, probe_bytes):
        resp = create_task(api_client, probe_bytes)  # 不传 camera_view
        assert resp.status_code == 201, resp.text
        task_id = resp.json()["data"]["task_id"]
        state = task_store.get(task_id)
        assert state is not None
        assert state.camera_view is CameraView.AUTO

    def test_explicit_down_the_line(self, api_client, probe_bytes):
        resp = create_task(api_client, probe_bytes, camera_view="down_the_line")
        assert resp.status_code == 201, resp.text
        state = task_store.get(resp.json()["data"]["task_id"])
        assert state.camera_view is CameraView.DOWN_THE_LINE

    def test_explicit_face_on(self, api_client, probe_bytes):
        resp = create_task(api_client, probe_bytes, camera_view="face_on")
        assert resp.status_code == 201, resp.text
        state = task_store.get(resp.json()["data"]["task_id"])
        assert state.camera_view is CameraView.FACE_ON

    def test_auto_accepted_internally(self, api_client, probe_bytes):
        """``auto`` 内部可接受；由 view_detector 在 pipeline 内解析为具体机位。"""
        resp = create_task(api_client, probe_bytes, camera_view="auto")
        assert resp.status_code == 201, resp.text
        state = task_store.get(resp.json()["data"]["task_id"])
        assert state.camera_view is CameraView.AUTO

    def test_invalid_value_falls_back_auto(self, api_client, probe_bytes):
        """非法值（如 ``side_view``）一律回退 AUTO，让后端自动判定兜底。"""
        resp = create_task(api_client, probe_bytes, camera_view="side_view")
        assert resp.status_code == 201, resp.text
        state = task_store.get(resp.json()["data"]["task_id"])
        assert state.camera_view is CameraView.AUTO


# ---------------------------------------------------------------------------
# 查询状态
# ---------------------------------------------------------------------------


class TestTaskStatus:
    """``GET /api/v1/tasks/{task_id}``（旧）与 ``/api/v1/task/status/{task_id}``（PDD）。"""

    def test_unknown_task_404(self, api_client):
        resp = api_client.get("/api/v1/tasks/deadbeefcafe")
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == config.PDD_CODE_TASK_NOT_FOUND  # 20001
        assert body["data"] is None

    def test_status_payload_schema(self, api_client, probe_bytes):
        task_id = create_task(api_client, probe_bytes).json()["data"]["task_id"]
        resp = api_client.get(f"/api/v1/tasks/{task_id}")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert set(data) == {
            "task_id", "status", "progress", "step",
            "message", "error_code", "error_message", "step_text",
        }
        assert data["task_id"] == task_id
        assert data["status"] in {s.value for s in TaskStatus}
        assert isinstance(data["progress"], int) and 0 <= data["progress"] <= 100
        assert isinstance(data["step"], int) and 1 <= data["step"] <= 4
        assert isinstance(data["message"], str) and data["message"]
        assert isinstance(data["step_text"], str)
        # step_text 应来自 config.STEP_TEXTS
        assert data["step_text"] == config.STEP_TEXTS.get(data["step"], "") or data[
            "step_text"
        ]

    def test_bad_video_reports_chinese_error(self, api_client, probe_bytes):
        """t.mp4 时长 1.0s < 1.5s -> BAD_VIDEO + 中文文案（业务失败在 data 内）。"""
        task_id = create_task(api_client, probe_bytes).json()["data"]["task_id"]
        data = wait_terminal(api_client, task_id)
        assert data["status"] == TaskStatus.FAILED.value
        assert data["error_code"] in {
            ErrorCode.BAD_VIDEO.value, ErrorCode.TOO_DARK.value,
            ErrorCode.NO_PERSON.value, ErrorCode.NO_SWING.value,
            ErrorCode.LOW_QUALITY.value,
        }, data
        assert data["error_message"] == config.ERROR_MESSAGES[data["error_code"]]
        assert re.search(r"[\u4e00-\u9fa5]", data["error_message"]), "文案必须是中文"

    def test_orientation_video_reports_chinese_error(
        self, api_client, probe_bytes, monkeypatch
    ):
        """模拟 90° 横拍视频上传 -> 任务 FAILED + BAD_ORIENTATION + 中文文案。

        monkeypatch ``pose_extractor.read_orientation`` 恒返回 90（不依赖真实 90°
        视频素材），确认上传后 probe 阶段即拒绝、前端轮询能看到竖拍提示文案。
        """
        from app import pose_extractor

        monkeypatch.setattr(pose_extractor, "read_orientation", lambda cap: 90)
        task_id = create_task(api_client, probe_bytes).json()["data"]["task_id"]
        data = wait_terminal(api_client, task_id)
        assert data["status"] == TaskStatus.FAILED.value
        assert data["error_code"] == ErrorCode.BAD_ORIENTATION.value
        assert data["error_message"] == config.ERROR_MESSAGES["BAD_ORIENTATION"]
        assert re.search(r"[\u4e00-\u9fa5]", data["error_message"]), "文案必须是中文"


class TestDualPath:
    """PDD 主路径与旧路径行为等价。"""

    def test_status_paths_equivalent(self, api_client, probe_bytes):
        task_id = create_task(api_client, probe_bytes).json()["data"]["task_id"]
        old = api_client.get(f"/api/v1/tasks/{task_id}").json()
        new = api_client.get(f"/api/v1/task/status/{task_id}").json()
        assert old == new
        assert old["code"] == 0

    def test_result_paths_equivalent(self, api_client, probe_bytes):
        task_id = create_task(api_client, probe_bytes).json()["data"]["task_id"]
        wait_terminal(api_client, task_id)
        old = api_client.get(f"/api/v1/tasks/{task_id}/result")
        new = api_client.get(f"/api/v1/task/result/{task_id}")
        # 任务失败时两条路径都应返回 20002（任务尚未完成）
        assert old.status_code == new.status_code
        assert old.json()["code"] == new.json()["code"] == config.PDD_CODE_TASK_PENDING

    def test_pdd_path_unknown_task_404(self, api_client):
        resp = api_client.get("/api/v1/task/status/deadbeefcafe")
        assert resp.status_code == 404
        assert resp.json()["code"] == config.PDD_CODE_TASK_NOT_FOUND


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------


class TestTaskResult:
    """``GET /api/v1/tasks/{task_id}/result``。"""

    def test_unknown_task_404(self, api_client):
        resp = api_client.get("/api/v1/tasks/deadbeefcafe/result")
        assert resp.status_code == 404
        assert resp.json()["code"] == config.PDD_CODE_TASK_NOT_FOUND

    def test_unfinished_or_failed_returns_20002(self, api_client, probe_bytes):
        task_id = create_task(api_client, probe_bytes).json()["data"]["task_id"]
        wait_terminal(api_client, task_id)
        resp = api_client.get(f"/api/v1/tasks/{task_id}/result")
        assert resp.status_code == 409
        body = resp.json()
        assert body["code"] == config.PDD_CODE_TASK_PENDING  # 20002
        assert body["data"] is None


# ---------------------------------------------------------------------------
# 错误码映射 / legacy 回滚
# ---------------------------------------------------------------------------


class TestOperationLogAudit:
    """操作记录（operation_logs）的路由层写入。

    与 4 张 task 业务表正交：本表记「用户做了什么」，业务表记「分析结果是什么」。
    2026-09-07 补齐 login / upload / view_result 三个 action（此前只有
    update_avatar / update_nickname 落地）。
    """

    @staticmethod
    def _capture(monkeypatch):
        """把 audit 的 DB 换成记录型假对象，返回 captured 列表。"""
        from app import audit

        captured = []

        class FakeDB:
            def execute(self, sql, args=None):
                captured.append((sql, args))
                return 1

        monkeypatch.setattr(audit, "db", FakeDB())
        return captured

    def test_upload_writes_log_with_task_id(self, api_client, probe_bytes, monkeypatch):
        """上传成功 -> 写 action='upload'，并关联 task_id。"""
        from app import auth

        captured = self._capture(monkeypatch)
        monkeypatch.setattr(auth, "openid_from_headers", lambda h: "oUPLOAD123")

        task_id = create_task(api_client, probe_bytes).json()["data"]["task_id"]

        logs = [a for s, a in captured if "INSERT INTO operation_logs" in s]
        assert logs, "上传成功必须写一条操作记录"
        args = logs[0]
        assert args[0] == "oUPLOAD123"          # openid
        assert args[1] == "upload"              # action
        assert args[2] == "上传分析"             # action_name
        assert args[3] == task_id               # task_id 关联（关键：能回溯到任务）
        assert json.loads(args[4])["bytes"] > 0  # detail 带文件大小

    def test_anonymous_upload_still_writes_log(self, api_client, probe_bytes, monkeypatch):
        """未登录上传 -> 仍写记录（openid=NULL），「匿名上传量」要看板用。"""
        from app import auth

        captured = self._capture(monkeypatch)
        monkeypatch.setattr(auth, "openid_from_headers", lambda h: None)

        create_task(api_client, probe_bytes)

        logs = [a for s, a in captured if "INSERT INTO operation_logs" in s]
        assert logs, "匿名上传也要记（schema 允许 openid NULL）"
        assert logs[0][0] is None

    def test_view_result_writes_log(self, api_client, monkeypatch):
        """查看结果 -> 写 action='view_result'。"""
        from app import auth
        from app import main as main_module
        from app.schemas import TaskStatus

        captured = self._capture(monkeypatch)
        monkeypatch.setattr(auth, "openid_from_headers", lambda h: "oVIEW123")

        # 伪造一个已成功完成的任务（真实跑分析太慢且与本用例无关）。
        # 注意：main.py 是 ``from .task_store import task_store``——导入的是
        # **实例**而非模块，所以必须打在 ``main.task_store`` 上。
        class FakeResult:
            def model_dump(self, mode="json"):
                return {"ok": True}

        class FakeState:
            task_id = "deadbeefcafe"
            status = TaskStatus.SUCCESS
            openid = "oVIEW123"
            result = FakeResult()

        monkeypatch.setattr(main_module.task_store, "get", lambda tid: FakeState())

        resp = api_client.get("/api/v1/tasks/deadbeefcafe/result")
        assert resp.status_code == 200, resp.text

        logs = [a for s, a in captured if "INSERT INTO operation_logs" in s]
        assert logs, "查看结果必须写一条操作记录"
        args = logs[0]
        assert args[1] == "view_result"
        assert args[2] == "查看结果"
        assert args[3] == "deadbeefcafe"

    def test_view_result_unknown_task_writes_no_log(self, api_client, monkeypatch):
        """任务不存在 -> 4004 早退，不应写日志（避免脏数据）。"""
        from app import main as main_module

        captured = self._capture(monkeypatch)
        monkeypatch.setattr(main_module.task_store, "get", lambda tid: None)

        resp = api_client.get("/api/v1/tasks/nosuch/result")
        # 4004「任务不存在」映射 HTTP 404，业务码在 body 里（20001）
        assert resp.status_code == 404
        assert resp.json()["code"] == 20001

        logs = [a for s, a in captured if "INSERT INTO operation_logs" in s]
        assert not logs, "任务不存在时早退，不应写操作记录"


class TestErrorCodeMapping:
    """对外 PDD 码（10001/10002/10003/20001/20002）。"""

    def test_oversize_10001(self, api_client):
        oversize = b"\x00" * (config.MAX_UPLOAD_BYTES + 1024 * 1024)
        resp = create_task(api_client, oversize)
        assert resp.json()["code"] == 10001

    def test_bad_format_10002(self, api_client, probe_bytes):
        resp = create_task(api_client, probe_bytes, name="bad.avi")
        assert resp.json()["code"] == 10002

    def test_duration_10003_via_handler(self):
        """10003（时长超范围）只在 AnalysisError 显式携带时可达；直接测响应层映射。"""
        from app.main import err

        response = err(4001, "时长超范围", config.PDD_CODE_BAD_DURATION)
        body = response.body.decode("utf-8")
        assert '"code":10003' in body
        assert response.status_code == 400

    def test_orientation_10005_via_handler(self):
        """10005（方向异常）只在 AnalysisError 显式携带时可达；直接测响应层映射。"""
        from app.main import err

        response = err(
            4001, config.ERROR_MESSAGES["BAD_ORIENTATION"],
            config.PDD_CODE_BAD_ORIENTATION,
        )
        body = response.body.decode("utf-8")
        assert '"code":10005' in body
        assert response.status_code == 400

    def test_task_not_found_20001(self, api_client):
        resp = api_client.get("/api/v1/tasks/deadbeefcafe")
        assert resp.json()["code"] == 20001

    def test_task_pending_20002(self, api_client, probe_bytes):
        task_id = create_task(api_client, probe_bytes).json()["data"]["task_id"]
        wait_terminal(api_client, task_id)
        resp = api_client.get(f"/api/v1/tasks/{task_id}/result")
        assert resp.json()["code"] == 20002

    def test_internal_5000_maps_10004(self, api_client, monkeypatch):
        """请求处理中的未处理异常 -> 5000 内部码、对外 10004（响应层兜底）。"""
        from fastapi.testclient import TestClient

        from app.main import app

        def _boom(task_id):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(task_store, "get", _boom)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/tasks/deadbeefcafe")
        assert resp.status_code == 500
        body = resp.json()
        assert body["code"] == config.PDD_CODE_INTERNAL  # 10004
        assert "unexpected" not in (body["message"] or ""), "不得泄漏内部异常信息"


class TestLegacyCodeStyle:
    """``config.API_CODE_STYLE="legacy"`` 时对外回落旧码（线上回滚开关）。"""

    @pytest.fixture(autouse=True)
    def _legacy(self, monkeypatch):
        monkeypatch.setattr(config, "API_CODE_STYLE", "legacy")
        yield

    def test_unknown_task_returns_4004(self, api_client):
        resp = api_client.get("/api/v1/tasks/deadbeefcafe")
        assert resp.json()["code"] == 4004

    def test_bad_format_returns_4001(self, api_client, probe_bytes):
        resp = create_task(api_client, probe_bytes, name="bad.avi")
        assert resp.json()["code"] == 4001

    def test_oversize_returns_4001(self, api_client):
        oversize = b"\x00" * (config.MAX_UPLOAD_BYTES + 1024 * 1024)
        resp = create_task(api_client, oversize)
        assert resp.json()["code"] == 4001

    def test_pending_returns_4009(self, api_client, probe_bytes):
        task_id = create_task(api_client, probe_bytes).json()["data"]["task_id"]
        wait_terminal(api_client, task_id)
        resp = api_client.get(f"/api/v1/tasks/{task_id}/result")
        assert resp.json()["code"] == 4009


# ---------------------------------------------------------------------------
# 静态资源与兜底
# ---------------------------------------------------------------------------


class TestStaticAndFallback:
    """``/static`` 与统一异常包。"""

    def test_missing_static_image_404(self, api_client):
        resp = api_client.get("/static/deadbeefcafe/04_top.jpg")
        assert resp.status_code == 404

    def test_existing_static_image_200(self, api_client):
        """在任务目录里放一张真图，验证静态路由可访问。"""
        import cv2
        import numpy as np

        task_dir = config.DATA_DIR / "statictest01"
        task_dir.mkdir(parents=True, exist_ok=True)
        img = np.full((32, 32, 3), 200, dtype=np.uint8)
        assert cv2.imwrite(str(task_dir / "04_top.jpg"), img)

        resp = api_client.get("/static/statictest01/04_top.jpg")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/")
        assert len(resp.content) > 0

    def test_unknown_route_uses_unified_envelope(self, api_client):
        resp = api_client.get("/api/v1/not-exists")
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == config.PDD_CODE_TASK_NOT_FOUND  # 20001
        assert body["data"] is None


class TestLoginDegradationHTTPStatus:
    """登录失败的**降级响应必须 HTTP 200**，不能是 500。

    背景（2026-09-07 线上事故）：
        登录失败时后端 ``err(5000, ...)`` 走 ``_CODE_TO_HTTP[5000] = 500``，
        而微信 ``wx.request`` 只在 2xx/3xx 时进 ``success`` 回调——HTTP 500
        会直接落到 ``fail``，前端收到的是「网络连接失败，请检查后端服务是否已启动」，
        根本读不到业务码 10004，**降级为匿名用户的逻辑从未被执行**。

    修复：``err()`` 新增 ``http_status`` 参数，登录降级显式传 200。
    本组用例同时锁住「其它 5000 真异常仍必须 500」，防止把修复扩大化。
    """

    def test_login_failure_returns_http_200(self, api_client, monkeypatch):
        """登录失败 -> HTTP 200 + 业务码 10004（前端才进得了解降级分支）。"""
        from app import auth as auth_module

        monkeypatch.setattr(auth_module, "login", lambda *a, **k: None)

        resp = api_client.post("/api/v1/auth/login", json={"code": "any-code"})

        assert resp.status_code == 200, (
            "登录降级必须 HTTP 200；返回 500 会让 wx.request 走 fail 回调，"
            "前端读不到业务码 10004，匿名降级形同虚设"
        )
        body = resp.json()
        assert body["code"] == config.PDD_CODE_INTERNAL  # 10004
        assert body["data"] is None

    def test_login_failure_message_mentions_guest(self, api_client, monkeypatch):
        """降级文案要让用户知道「仍可继续用」，不是报错口吻。"""
        from app import auth as auth_module

        monkeypatch.setattr(auth_module, "login", lambda *a, **k: None)

        body = api_client.post("/api/v1/auth/login", json={"code": "x"}).json()
        assert "游客" in (body["message"] or "")

    def test_other_5000_errors_still_return_500(self, monkeypatch):
        """防回归：登录之外的 5000（真内部错误）**必须**保持 HTTP 500。

        注意用 ``raise_server_exceptions=False`` 的 TestClient——否则异常直接
        冒泡，走不到 FastAPI 的兜底处理器（与 test_internal_5000_maps_10004
        同一手法）。
        """
        from fastapi.testclient import TestClient

        from app import auth as auth_module
        from app.main import app

        def _boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(auth_module, "openid_from_headers", _boom)

        # /auth/me 内部异常 -> 兜底 5000 -> HTTP 500（不得被降级修复波及）
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get(
                "/api/v1/auth/me", headers={"Authorization": "Bearer t"}
            )
        assert resp.status_code == 500
        assert resp.json()["code"] == config.PDD_CODE_INTERNAL

    def test_err_default_status_still_from_mapping(self):
        """``err()`` 不传 http_status 时，状态码仍由内部码映射决定。"""
        from app.main import err

        assert err(0, "ok").status_code == 200
        assert err(4001, "bad").status_code == 400
        assert err(4004, "nf").status_code == 404
        assert err(5000, "boom").status_code == 500, "真异常默认仍须 500"

    def test_err_http_status_override_only_when_explicit(self):
        """只有显式传 http_status 才会覆盖（降级专用，不默认生效）。"""
        from app.main import err

        resp = err(5000, "登录失败，将以游客身份继续",
                   config.PDD_CODE_INTERNAL, http_status=200)
        assert resp.status_code == 200
        assert resp.body  # 有响应体
