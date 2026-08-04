"""HTTP 接口集成测试（架构文档 §4），使用 FastAPI TestClient，不起 uvicorn 进程。

上传用例统一使用 ``.tools/_probe/t.mp4``（1.0s 匀速灰阶合成视频），
它会在后台流水线里被 ``probe_video`` 判为 ``BAD_VIDEO``（时长 < 1.5s），
既能覆盖失败链路，又不会加载 MediaPipe 模型、执行很快。
"""

from __future__ import annotations

import os
import re
import time

import pytest

from app import config
from app.schemas import ErrorCode, TaskStatus

PROBE_MP4 = r"E:\project\golf\.tools\_probe\t.mp4"


@pytest.fixture(scope="session")
def probe_bytes() -> bytes:
    """读取合成测试视频。"""
    assert os.path.exists(PROBE_MP4), f"缺少测试素材: {PROBE_MP4}"
    with open(PROBE_MP4, "rb") as handle:
        return handle.read()


def create_task(client, content: bytes, name: str = "swing.mp4",
                ctype: str = "video/mp4"):
    """上传并返回响应。"""
    return client.post(
        f"{config_api()}/tasks", files={"file": (name, content, ctype)}
    )


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
        assert body["message"] == "ok"
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
    """``POST /api/v1/tasks``。"""

    def test_create_success(self, api_client, probe_bytes):
        resp = create_task(api_client, probe_bytes)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["code"] == 0
        assert body["message"] == "ok"
        task_id = body["data"]["task_id"]
        assert re.fullmatch(r"[0-9a-f]{12}", task_id), task_id
        assert body["data"]["status"] == TaskStatus.PENDING.value

    def test_task_dir_created(self, api_client, probe_bytes):
        task_id = create_task(api_client, probe_bytes).json()["data"]["task_id"]
        assert (config.DATA_DIR / task_id).is_dir()

    @pytest.mark.parametrize("name", ["swing.mov", "swing.avi", "swing.txt", "a.MP4.zip"])
    def test_reject_non_mp4_extension(self, api_client, probe_bytes, name):
        resp = create_task(api_client, probe_bytes, name=name)
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 4001
        assert body["data"] is None
        assert "mp4" in body["message"]

    def test_accept_uppercase_extension(self, api_client, probe_bytes):
        resp = create_task(api_client, probe_bytes, name="SWING.MP4")
        assert resp.status_code == 201

    def test_reject_bad_content_type(self, api_client, probe_bytes):
        resp = create_task(api_client, probe_bytes, name="swing.mp4", ctype="image/png")
        assert resp.status_code == 400
        assert resp.json()["code"] == 4001

    def test_reject_empty_file(self, api_client):
        resp = create_task(api_client, b"")
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 4001
        assert "空" in body["message"]

    def test_reject_oversize(self, api_client):
        """> 20MB 必须 4001（架构 §4.2）。"""
        oversize = b"\x00" * (config.MAX_UPLOAD_BYTES + 1024 * 1024)
        resp = create_task(api_client, oversize)
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 4001
        assert "20MB" in body["message"]

    def test_rejected_upload_leaves_no_task(self, api_client, probe_bytes):
        """校验失败必须回滚任务目录，不留垃圾。"""
        before = set(os.listdir(config.DATA_DIR))
        create_task(api_client, probe_bytes, name="bad.mov")
        after = set(os.listdir(config.DATA_DIR))
        assert after == before


# ---------------------------------------------------------------------------
# 查询状态
# ---------------------------------------------------------------------------


class TestTaskStatus:
    """``GET /api/v1/tasks/{task_id}``。"""

    def test_unknown_task_404(self, api_client):
        resp = api_client.get("/api/v1/tasks/deadbeefcafe")
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == 4004
        assert body["data"] is None

    def test_status_payload_schema(self, api_client, probe_bytes):
        task_id = create_task(api_client, probe_bytes).json()["data"]["task_id"]
        resp = api_client.get(f"/api/v1/tasks/{task_id}")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert set(data) == {
            "task_id", "status", "progress", "step",
            "message", "error_code", "error_message",
        }
        assert data["task_id"] == task_id
        assert data["status"] in {s.value for s in TaskStatus}
        assert isinstance(data["progress"], int) and 0 <= data["progress"] <= 100
        assert isinstance(data["step"], int) and 1 <= data["step"] <= 4
        assert isinstance(data["message"], str) and data["message"]

    def test_bad_video_reports_chinese_error(self, api_client, probe_bytes):
        """t.mp4 时长 1.0s < 1.5s -> BAD_VIDEO + 中文文案（架构 §4.7）。"""
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


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------


class TestTaskResult:
    """``GET /api/v1/tasks/{task_id}/result``。"""

    def test_unknown_task_404(self, api_client):
        resp = api_client.get("/api/v1/tasks/deadbeefcafe/result")
        assert resp.status_code == 404
        assert resp.json()["code"] == 4004

    def test_unfinished_or_failed_returns_4009(self, api_client, probe_bytes):
        task_id = create_task(api_client, probe_bytes).json()["data"]["task_id"]
        wait_terminal(api_client, task_id)
        resp = api_client.get(f"/api/v1/tasks/{task_id}/result")
        assert resp.status_code == 409
        body = resp.json()
        assert body["code"] == 4009
        assert body["data"] is None


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
        assert body["code"] == 4004
        assert body["data"] is None
