"""FastAPI 应用入口（架构 ARCHITECTURE.md §4 + ARCHITECTURE-v2.md §6 接口契约）。

启动::

    cd E:/project/golf/backend
    E:/project/golf/.tools/python312/python.exe -m uvicorn app.main:app \
        --host 0.0.0.0 --port 8000

v2 接口契约（架构 §6）：
- **双路径注册**：PDD 主路径 ``/api/v1/task/create|status|result`` + 旧路径兼容别名
  ``/tasks``（灰度期双活，不破坏已上线小程序）；
- **错误码映射**：对外发 PDD 码（10001~10004 / 20001 / 20002），内部保留现有语义码
  （0 / 4001 / 4004 / 4009 / 5000），由 ``config.API_CODE_STYLE`` 一键回滚；
- **字段兼容**：``step`` 保持 int + 并列 ``step_text``；``video`` / ``file`` 双字段名；
  ``camera_view`` 必填二选一（缺省按 ``face_on`` 落值不硬拒）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config
from .frame_service import FrameError, phase_metrics, render_frame
from .pipeline import run_analysis
from .schemas import AnalysisError, CameraView, TaskStatus
from .task_store import task_store

logging.basicConfig(level=config.LOG_LEVEL, format=config.LOG_FORMAT)
logger = logging.getLogger("app.main")

API_PREFIX = "/api/v1"

#: 业务码 -> HTTP 状态码（内部语义码驱动 HTTP 状态；对外码只影响响应包 code）
_CODE_TO_HTTP: Dict[int, int] = {0: 200, 4001: 400, 4004: 404, 4009: 409, 5000: 500}


# ---------------------------------------------------------------------------
# 统一响应包（架构文档 §10.5 + v2 §6.3 错误码映射）
# ---------------------------------------------------------------------------


def ok(data: Any, status_code: int = 200) -> JSONResponse:
    """成功响应（message 对齐 PDD = ``"success"``）。"""
    return JSONResponse(
        status_code=status_code,
        content={"code": 0, "data": data, "message": "success"},
    )


def err(code: int, message: str, pdd_code: Optional[int] = None) -> JSONResponse:
    """失败响应。对外码 = ``pdd_code``（PDD 风格）或 ``code``（legacy 风格）。

    HTTP 状态码始终由**内部语义码**决定（4001->400 / 4004->404 / 4009->409 /
    5000->500），与对外码无关——保证新旧两套码的 HTTP 语义一致。
    """
    out_code = pdd_code if config.API_CODE_STYLE == "pdd" else code
    return JSONResponse(
        status_code=_CODE_TO_HTTP.get(code, 500),
        content={"code": out_code, "data": None, "message": message},
    )


class ApiError(Exception):
    """接口层业务异常。

    Args:
        code: 内部语义码（0/4001/4004/4009/5000），决定 HTTP 状态与日志。
        message: 用户可见中文文案。
        pdd_code: 对外 PDD 码；``None`` 时回落为 ``code``。
    """

    def __init__(self, code: int, message: str, pdd_code: Optional[int] = None) -> None:
        self.code = code
        self.pdd_code = pdd_code or code
        self.message = message
        super().__init__(message)


def _parse_camera_view(raw: Optional[str]) -> CameraView:
    """解析 ``camera_view`` 表单值。

    契约（2026-09-04 调整为「无值默认 AUTO」）：

    - ``"auto"`` / 缺省 / ``None`` → :attr:`CameraView.AUTO`
      （由 :func:`app.view_detector.resolve` 自动判定：9/9 命中实测）。
    - ``"face_on"`` / ``"down_the_line"`` → 显式对应机位；
      后端做一致性校验，不一致会返回 :data:`config.WARN_VIEW_MISMATCH`。
    - 其它非法值 → 回退 ``AUTO``（不硬拒）。

    改默认值的原因：之前的 ``face_on`` 默认让 DTL 视频被错判为 face_on，
    ClubProbe 的 DTL 门控永远不触发；改 AUTO 后真实 DTL 视频能自动识别，
    ClubProbe / view-dependent 指标都能正确生效。
    """
    value = str(raw).strip().lower() if raw is not None else ""
    if not value or value == "auto":
        return CameraView.AUTO
    for candidate in CameraView:
        if candidate.value == value:
            return candidate
    logger.warning("非法 camera_view=%r，回退 AUTO", raw)
    return CameraView.AUTO


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------

app = FastAPI(title="Golf Swing Analyzer", version="1.0.0", docs_url="/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

config.DATA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(config.DATA_DIR)), name="static")


# ---------------------------------------------------------------------------
# 异常处理
# ---------------------------------------------------------------------------


@app.exception_handler(ApiError)
async def _api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    """接口层业务异常。"""
    return err(exc.code, exc.message, exc.pdd_code)


@app.exception_handler(AnalysisError)
async def _analysis_error_handler(_: Request, exc: AnalysisError) -> JSONResponse:
    """分析业务异常 -> 4001 + PDD 细分码（若抛出时显式携带）+ 中文文案。"""
    return err(4001, config.error_message(exc.code.value), exc.pdd_code)


@app.exception_handler(StarletteHTTPException)
async def _http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    """把框架级 HTTP 异常也包进统一响应包（对外码按 PDD 表映射）。"""
    mapping = {400: 4001, 404: 4004, 405: 4001, 409: 4009, 413: 4001, 422: 4001}
    pdd_mapping = {4001: config.PDD_CODE_BAD_FORMAT, 4004: config.PDD_CODE_TASK_NOT_FOUND,
                   4009: config.PDD_CODE_TASK_PENDING}
    code = mapping.get(exc.status_code, 5000)
    pdd_code = pdd_mapping.get(code, config.PDD_CODE_INTERNAL if code == 5000 else None)
    message = str(exc.detail) if exc.detail else "请求失败"
    if exc.status_code == 404:
        message = "资源不存在"
    return err(code, message, pdd_code)


@app.exception_handler(Exception)
async def _fallback_handler(_: Request, exc: Exception) -> JSONResponse:
    """兜底：记录 traceback，但绝不返回给前端。"""
    logger.exception("unhandled error: %s", exc)
    return err(5000, "服务器内部错误", config.PDD_CODE_INTERNAL)


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


@app.get(f"{API_PREFIX}/health")
async def health() -> JSONResponse:
    """健康检查。"""
    return ok({"status": "ok", "mediapipe": config.MEDIAPIPE_VERSION})


def _validate_filename(filename: Optional[str], content_type: Optional[str]) -> None:
    """扩展名 / content-type 校验（PDD 放开 .mov）。

    Raises:
        ApiError: 4001 + 对外 10002（格式不支持）。
    """
    name = (filename or "").strip().lower()
    suffix = Path(name).suffix
    if suffix and suffix not in config.ALLOWED_VIDEO_EXTS:
        raise ApiError(
            4001, "只支持 mp4 / mov 格式的视频", config.PDD_CODE_BAD_FORMAT
        )
    ctype = (content_type or "").strip().lower()
    if ctype and ctype not in config.ALLOWED_CONTENT_TYPES:
        raise ApiError(
            4001, "只支持 mp4 / mov 格式的视频", config.PDD_CODE_BAD_FORMAT
        )


def _pick_upload(
    video: Optional[UploadFile], file: Optional[UploadFile]
) -> Optional[UploadFile]:
    """``video``（PDD）为主，``file``（旧）兼容；取非 None 者。"""
    if video is not None:
        return video
    return file


@app.post(f"{API_PREFIX}/task/create")
@app.post(f"{API_PREFIX}/tasks")
async def create_task(
    background_tasks: BackgroundTasks,
    video: UploadFile = File(None),
    file: UploadFile = File(None),
    camera_view: str = Form("auto"),  # 2026-09-04：默认从 face_on 改为 auto
) -> JSONResponse:
    """上传视频并创建分析任务（PDD 主路径 + 旧路径双注册）。

    - 文件字段名：``video``（PDD 主）/ ``file``（旧兼容）；
    - ``camera_view``：``face_on`` / ``down_the_line``（``auto`` 内部可接受），
      缺省/非法值按 ``face_on`` 落值，不硬拒。
    """
    upload = _pick_upload(video, file)
    if upload is None:
        raise ApiError(
            4001, "缺少视频文件（字段名 video 或 file）", config.PDD_CODE_BAD_FORMAT
        )
    _validate_filename(upload.filename, upload.content_type)

    parsed_view = _parse_camera_view(camera_view)

    state = task_store.create(camera_view=parsed_view)
    target = Path(state.out_dir or str(config.DATA_DIR / state.task_id))
    ext = Path(upload.filename or ".mp4").suffix or ".mp4"
    video_path = target / config.upload_filename(ext)

    written = 0
    try:
        with open(video_path, "wb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > config.MAX_UPLOAD_BYTES:
                    raise ApiError(
                        4001, "视频大小超过 40MB", config.PDD_CODE_FILE_TOO_LARGE
                    )
                handle.write(chunk)
    except ApiError:
        task_store.remove(state.task_id)
        raise
    except OSError as exc:
        task_store.remove(state.task_id)
        logger.exception("write upload failed: %s", exc)
        raise ApiError(5000, "服务器内部错误", config.PDD_CODE_INTERNAL) from exc
    finally:
        await upload.close()

    if written == 0:
        task_store.remove(state.task_id)
        raise ApiError(
            4001, "上传的视频为空文件", config.PDD_CODE_BAD_FORMAT
        )

    task_store.update(state.task_id, video_path=str(video_path))
    background_tasks.add_task(run_analysis, state.task_id)
    logger.info(
        "upload ok: %s (%d bytes) view=%s", state.task_id, written, parsed_view.value
    )

    return ok(
        {"task_id": state.task_id, "status": TaskStatus.PENDING.value},
        status_code=201,
    )


@app.get(f"{API_PREFIX}/task/status/{{task_id}}")
@app.get(f"{API_PREFIX}/tasks/{{task_id}}")
async def get_task(task_id: str) -> JSONResponse:
    """查询任务状态（前端 1.5s 轮询）。"""
    task_store.sweep()
    state = task_store.get(task_id)
    if state is None:
        raise ApiError(
            4004, "任务不存在或已过期", config.PDD_CODE_TASK_NOT_FOUND
        )
    return ok(state.to_view().model_dump(mode="json"))


@app.get(f"{API_PREFIX}/task/result/{{task_id}}")
@app.get(f"{API_PREFIX}/tasks/{{task_id}}/result")
async def get_result(task_id: str) -> JSONResponse:
    """获取完整分析结果。"""
    state = task_store.get(task_id)
    if state is None:
        raise ApiError(
            4004, "任务不存在或已过期", config.PDD_CODE_TASK_NOT_FOUND
        )
    if state.status is not TaskStatus.SUCCESS or state.result is None:
        raise ApiError(
            4009, "任务尚未完成", config.PDD_CODE_TASK_PENDING
        )
    return ok(state.result.model_dump(mode="json"))


@app.get(f"{API_PREFIX}/task/{{task_id}}/frame/{{frame_index}}")
@app.get(f"{API_PREFIX}/tasks/{{task_id}}/frame/{{frame_index}}")
async def get_frame(task_id: str, frame_index: int) -> Response:
    """动态渲染指定帧的骨架叠加图 PNG（结果页缩略图 ◀▶ 手动微调）。

    - 双路径：PDD 主路径 ``/api/v1/task/{id}/frame/{idx}`` + 旧别名
      ``/api/v1/tasks/{id}/frame/{idx}``；
    - 成功返回 ``image/png`` 二进制，实际渲染帧号经响应头 ``X-Frame-Index``
      回传（降采样视频中间帧会快照到最近采样帧）；
    - 失败返回统一错误包：任务不存在 20001、任务未完成 20002、
      帧号越界/超出调整范围 20003。

    说明：本接口只负责**换图**；指标卡实时重算由前端额外调用
    ``GET /api/v1/task/{id}/phase_metrics/{phase}/{idx}`` 完成。
    """
    try:
        png, actual = render_frame(task_id, frame_index)
    except FrameError as exc:
        return err(exc.code, exc.message, exc.pdd_code)
    return Response(
        content=png,
        media_type="image/png",
        headers={"X-Frame-Index": str(actual)},
    )


@app.get(f"{API_PREFIX}/task/{{task_id}}/phase_metrics/{{phase}}/{{frame_index}}")
@app.get(f"{API_PREFIX}/tasks/{{task_id}}/phase_metrics/{{phase}}/{{frame_index}}")
async def get_phase_metrics(task_id: str, phase: str, frame_index: int) -> JSONResponse:
    """手动微调时实时重算目标阶段指标（纯增量，不改核心算法）。

    - 双路径：PDD 主路径 ``/api/v1/task/{id}/phase_metrics/{phase}/{idx}``
      + 旧别名 ``/api/v1/tasks/{id}/phase_metrics/{phase}/{idx}``；
    - ``phase`` 接受 PhaseKey 值（``downswing``）或枚举名（``DOWNSWING``）；
    - 成功返回 ``{phase, frame_index, metrics}``（``frame_index`` 为采样对齐后的
      实际帧号，与骨架图同帧）；
    - 失败统一错误包：任务不存在 20001、任务未完成 20002、帧号越界 20003、
      阶段非法 20004。
    """
    try:
        phase_key, actual, metrics = phase_metrics(task_id, phase, frame_index)
    except FrameError as exc:
        return err(exc.code, exc.message, exc.pdd_code)
    return ok(
        {
            "phase": phase_key.value,
            "frame_index": actual,
            "metrics": [m.model_dump(mode="json") for m in metrics],
        }
    )
