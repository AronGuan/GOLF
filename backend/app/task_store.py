"""进程内任务表。

不引入 Celery / Redis / 数据库：``dict`` + ``threading.Lock`` 足以支撑 MVP
（并发预期 < 3）。过期清理由 :meth:`TaskStore.sweep` 在每次状态查询时顺带触发。
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from . import config
from .schemas import (
    AnalysisResult,
    CameraView,
    ErrorCode,
    TaskState,
    TaskStatus,
)

logger = logging.getLogger(__name__)

#: 终态集合
_TERMINAL_STATES = (TaskStatus.SUCCESS, TaskStatus.FAILED)


class TaskStore:
    """线程安全的任务仓库。"""

    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self._tasks: Dict[str, TaskState] = {}
        self._lock = threading.Lock()
        self._data_dir: Path = Path(data_dir) if data_dir else config.DATA_DIR
        self._data_dir.mkdir(parents=True, exist_ok=True)

    # -- 基础读写 ---------------------------------------------------------

    @property
    def data_dir(self) -> Path:
        """任务数据根目录。"""
        return self._data_dir

    def create(
        self,
        video_path: Optional[str] = None,
        out_dir: Optional[str] = None,
        camera_view: CameraView = CameraView.FACE_ON,
    ) -> TaskState:
        """创建任务记录并准备任务目录。

        Args:
            video_path: 视频落盘路径；上传流程中可先留空，落盘后再 :meth:`update`。
            out_dir: 任务目录；留空则按 ``{DATA_DIR}/{task_id}`` 自动创建。
            camera_view: 用户选择的拍摄机位（v2 新增，默认 face-on 兼容旧版）。

        Returns:
            新建的 :class:`TaskState`。
        """
        task_id = uuid.uuid4().hex[:12]
        target_dir = Path(out_dir) if out_dir else self._data_dir / task_id
        target_dir.mkdir(parents=True, exist_ok=True)

        now = time.time()
        state = TaskState(
            task_id=task_id,
            status=TaskStatus.PENDING,
            progress=0,
            step=1,
            message="排队中",
            video_path=video_path,
            out_dir=str(target_dir),
            camera_view=camera_view,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._tasks[task_id] = state
        logger.info("task created: %s -> %s", task_id, target_dir)
        return state

    def get(self, task_id: str) -> Optional[TaskState]:
        """按 id 取任务，不存在返回 ``None``。"""
        with self._lock:
            return self._tasks.get(task_id)

    def update(self, task_id: str, **kw: object) -> None:
        """按字段名批量更新任务。未知字段忽略。"""
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                return
            for key, value in kw.items():
                if hasattr(state, key):
                    setattr(state, key, value)
            state.updated_at = time.time()

    # -- 状态流转 ---------------------------------------------------------

    def set_progress(
        self, task_id: str, step: int, progress: int, message: str, step_text: str = ""
    ) -> None:
        """上报进度。进度**单调不回退**：新值小于旧值时忽略数值但仍更新文案。

        Args:
            step_text: PDD 的字符串 step（v2 新增）；空串时回落
                ``config.STEP_TEXTS.get(step, "")``。
        """
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None or state.status in _TERMINAL_STATES:
                return
            state.status = TaskStatus.PROCESSING
            state.step = max(state.step, int(step))
            new_progress = max(0, min(100, int(progress)))
            if new_progress > state.progress:
                state.progress = new_progress
            state.message = message
            state.step_text = step_text or config.STEP_TEXTS.get(int(step), "")
            state.updated_at = time.time()

    def fail(self, task_id: str, code: ErrorCode, message: str = "") -> None:
        """置为失败态，写入错误码与中文文案。"""
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None or state.status in _TERMINAL_STATES:
                return
            state.status = TaskStatus.FAILED
            state.error_code = code
            state.error_message = message or config.error_message(code.value)
            state.message = "分析失败"
            state.updated_at = time.time()
        logger.warning("task failed: %s code=%s", task_id, code.value)

    def succeed(self, task_id: str, result: AnalysisResult) -> None:
        """置为成功态，挂载结果。"""
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                return
            state.status = TaskStatus.SUCCESS
            state.result = result
            state.progress = 100
            state.step = 4
            state.message = "分析完成"
            state.error_code = None
            state.error_message = None
            state.updated_at = time.time()
        logger.info("task succeeded: %s", task_id)

    # -- 清理 -------------------------------------------------------------

    def sweep(self) -> None:
        """超时判定 + 过期目录清理。低成本，可在每次状态查询时调用。"""
        now = time.time()
        timed_out: List[str] = []
        expired: List[TaskState] = []

        with self._lock:
            for state in list(self._tasks.values()):
                if state.status in (TaskStatus.PENDING, TaskStatus.PROCESSING):
                    if now - state.created_at > config.TASK_TIMEOUT_SEC:
                        timed_out.append(state.task_id)
                elif now - state.updated_at > config.RESULT_TTL_HOURS * 3600.0:
                    expired.append(state)

        for task_id in timed_out:
            self.fail(task_id, ErrorCode.TIMEOUT)

        for state in expired:
            self._purge(state)

    def _purge(self, state: TaskState) -> None:
        """删除任务目录并从任务表中移除。"""
        with self._lock:
            self._tasks.pop(state.task_id, None)
        if not state.out_dir:
            return
        try:
            shutil.rmtree(state.out_dir, ignore_errors=True)
            logger.info("task purged: %s", state.task_id)
        except OSError:  # pragma: no cover - 清理失败不影响主流程
            logger.exception("purge failed: %s", state.task_id)

    def remove(self, task_id: str) -> None:
        """立即删除任务及其目录（上传校验失败时回滚用）。"""
        state = self.get(task_id)
        if state is not None:
            self._purge(state)

    def count(self) -> int:
        """当前任务数（仅用于调试 / 健康检查）。"""
        with self._lock:
            return len(self._tasks)


#: 全局单例
task_store = TaskStore()
