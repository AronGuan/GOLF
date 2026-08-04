"""FastAPI 应用入口（架构文档 §4）。

启动::

    cd E:/project/golf/backend
    E:/project/golf/.tools/python312/python.exe -m uvicorn app.main:app \
        --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, FastAPI, File, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config
from .pipeline import run_analysis
from .schemas import AnalysisError, TaskStatus
from .task_store import task_store

logging.basicConfig(level=config.LOG_LEVEL, format=config.LOG_FORMAT)
logger = logging.getLogger("app.main")

API_PREFIX = "/api/v1"

#: 业务码 -> HTTP 状态码
_CODE_TO_HTTP: Dict[int, int] = {0: 200, 4001: 400, 4004: 404, 4009: 409, 5000: 500}


# ---------------------------------------------------------------------------
# 统一响应包（架构文档 §10.5）
# ---------------------------------------------------------------------------


def ok(data: Any, status_code: int = 200) -> JSONResponse:
    """成功响应。"""
    return JSONResponse(
        status_code=status_code, content={"code": 0, "data": data, "message": "ok"}
    )


def err(code: int, message: str) -> JSONResponse:
    """失败响应。"""
    return JSONResponse(
        status_code=_CODE_TO_HTTP.get(code, 500),
        content={"code": code, "data": None, "message": message},
    )


class ApiError(Exception):
    """接口层业务异常。"""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


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
    return err(exc.code, exc.message)


@app.exception_handler(AnalysisError)
async def _analysis_error_handler(_: Request, exc: AnalysisError) -> JSONResponse:
    """分析业务异常 -> 4001 + 中文文案。"""
    return err(4001, config.error_message(exc.code.value))


@app.exception_handler(StarletteHTTPException)
async def _http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    """把框架级 HTTP 异常也包进统一响应包。"""
    mapping = {400: 4001, 404: 4004, 405: 4001, 409: 4009, 413: 4001, 422: 4001}
    code = mapping.get(exc.status_code, 5000)
    message = str(exc.detail) if exc.detail else "请求失败"
    if exc.status_code == 404:
        message = "资源不存在"
    return err(code, message)


@app.exception_handler(Exception)
async def _fallback_handler(_: Request, exc: Exception) -> JSONResponse:
    """兜底：记录 traceback，但绝不返回给前端。"""
    logger.exception("unhandled error: %s", exc)
    return err(5000, "服务器内部错误")


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


@app.get(f"{API_PREFIX}/health")
async def health() -> JSONResponse:
    """健康检查。"""
    return ok({"status": "ok", "mediapipe": config.MEDIAPIPE_VERSION})


def _validate_filename(filename: Optional[str], content_type: Optional[str]) -> None:
    """扩展名 / content-type 校验。

    Raises:
        ApiError: 4001。
    """
    name = (filename or "").strip().lower()
    suffix = Path(name).suffix
    if suffix and suffix not in config.ALLOWED_VIDEO_EXTS:
        raise ApiError(4001, "只支持 mp4 格式的视频")
    ctype = (content_type or "").strip().lower()
    if ctype and ctype not in config.ALLOWED_CONTENT_TYPES:
        raise ApiError(4001, "只支持 mp4 格式的视频")


@app.post(f"{API_PREFIX}/tasks")
async def create_task(
    background_tasks: BackgroundTasks, file: UploadFile = File(...)
) -> JSONResponse:
    """上传视频并创建分析任务。"""
    _validate_filename(file.filename, file.content_type)

    state = task_store.create()
    target = Path(state.out_dir or str(config.DATA_DIR / state.task_id))
    video_path = target / config.UPLOAD_FILENAME

    written = 0
    try:
        with open(video_path, "wb") as handle:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > config.MAX_UPLOAD_BYTES:
                    raise ApiError(4001, "视频大小超过 20MB")
                handle.write(chunk)
    except ApiError:
        task_store.remove(state.task_id)
        raise
    except OSError as exc:
        task_store.remove(state.task_id)
        logger.exception("write upload failed: %s", exc)
        raise ApiError(5000, "服务器内部错误") from exc
    finally:
        await file.close()

    if written == 0:
        task_store.remove(state.task_id)
        raise ApiError(4001, "上传的视频为空文件")

    task_store.update(state.task_id, video_path=str(video_path))
    background_tasks.add_task(run_analysis, state.task_id)
    logger.info("upload ok: %s (%d bytes)", state.task_id, written)

    return ok(
        {"task_id": state.task_id, "status": TaskStatus.PENDING.value},
        status_code=201,
    )


@app.get(f"{API_PREFIX}/tasks/{{task_id}}")
async def get_task(task_id: str) -> JSONResponse:
    """查询任务状态（前端 1.5s 轮询）。"""
    task_store.sweep()
    state = task_store.get(task_id)
    if state is None:
        raise ApiError(4004, "任务不存在")
    return ok(state.to_view().model_dump(mode="json"))


@app.get(f"{API_PREFIX}/tasks/{{task_id}}/result")
async def get_result(task_id: str) -> JSONResponse:
    """获取完整分析结果。"""
    state = task_store.get(task_id)
    if state is None:
        raise ApiError(4004, "任务不存在")
    if state.status is not TaskStatus.SUCCESS or state.result is None:
        raise ApiError(4009, "任务尚未完成")
    return ok(state.result.model_dump(mode="json"))
