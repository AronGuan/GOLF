"""任务表持久化层（``tasks`` + ``task_phases``）。

背景
----
MVP 阶段任务状态只存进程内 :class:`app.task_store.TaskStore`（dict），
重启或 :attr:`app.config.RESULT_TTL_HOURS` 过期后**任务记录全部丢失**，
前端 ``GET /api/v1/tasks/{id}/result`` 直接返 20001。

``deploy/mysql/001_init.sql`` 已经把 ``tasks`` / ``task_phases`` /
``task_metrics`` / ``task_risks`` 4 张业务表建好，但**接入代码从未写**。
本模块负责前两张（M3.1），后两张留给 M3.2。

设计约束
--------
1. **写入失败绝不抛异常**（与 :mod:`app.audit` 同语义）：
   任务表是「事后分析」用途，分析主流程已经被 :class:`TaskStore` 持久化
   （in-memory），DB 落库失败仅记 warn，**不影响用户拿到结果**。
2. **DB 是真源**（source of truth）：``TaskStore.sweep`` 清内存时**不动 DB**，
   重启由 ``main.lifespan`` 调 :func:`rehydrate` 从 DB 回灌。
3. **task_metrics / task_risks 不在本模块范围**，M3.2 再补；
   但 ``tasks.result_json`` 仍存完整 ``AnalysisResult`` 快照，
   后续要做指标分析可回溯（代价 = 反序列化，~~180 行/任务）。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Sequence

from . import config, db
from .schemas import (
    AnalysisResult,
    CameraView,
    PhaseKey,
    PhaseResult,
    TaskState,
    TaskStatus,
)

logger = logging.getLogger("app.persistence")


# ---------------------------------------------------------------------------
# tasks 表
# ---------------------------------------------------------------------------

#: ``INSERT ... ON DUPLICATE KEY UPDATE`` —— task_id 是 UNIQUE KEY，
#: 用一次原子写完成「有则更新、无则插入」，避免先 SELECT 再决定。
_TASK_UPSERT_SQL = (
    "INSERT INTO tasks ("
    "  task_id, status, progress, step, step_text, message,"
    "  error_code, error_message,"
    "  camera_view, fps, duration, width, height, frame_count, total_frames,"
    "  sample_step, low_fps, orientation,"
    "  tempo_ratio, swing_duration, max_head_drift_pct,"
    "  warnings, disclaimer, result_json,"
    "  video_path, out_dir, openid, finished_at"
    ") VALUES ("
    "  %s, %s, %s, %s, %s, %s,"
    "  %s, %s,"
    "  %s, %s, %s, %s, %s, %s, %s,"
    "  %s, %s, %s,"
    "  %s, %s, %s,"
    "  %s, %s, %s,"
    "  %s, %s, %s, %s"
    ") ON DUPLICATE KEY UPDATE "
    "  status=VALUES(status), progress=VALUES(progress), step=VALUES(step),"
    "  step_text=VALUES(step_text), message=VALUES(message),"
    "  error_code=VALUES(error_code), error_message=VALUES(error_message),"
    "  camera_view=VALUES(camera_view), fps=VALUES(fps), duration=VALUES(duration),"
    "  width=VALUES(width), height=VALUES(height),"
    "  frame_count=VALUES(frame_count), total_frames=VALUES(total_frames),"
    "  sample_step=VALUES(sample_step), low_fps=VALUES(low_fps),"
    "  orientation=VALUES(orientation),"
    "  tempo_ratio=VALUES(tempo_ratio), swing_duration=VALUES(swing_duration),"
    "  max_head_drift_pct=VALUES(max_head_drift_pct),"
    "  warnings=VALUES(warnings), disclaimer=VALUES(disclaimer),"
    "  result_json=VALUES(result_json),"
    "  video_path=VALUES(video_path), out_dir=VALUES(out_dir),"
    "  openid=VALUES(openid),"
    "  finished_at=COALESCE(VALUES(finished_at), finished_at)"
)


def _row_for_task(state: TaskState) -> Sequence:
    """把 TaskState 拍平成 tasks 表的一行参数。

    - ``state.result`` 可能是 None（分析中）；video_meta / global_metrics
      等只取 ``result`` 已填充时的值，否则 NULL。
    - ``warnings`` / ``result_json`` 序列化为 JSON 字符串，依赖
      ``db.execute`` 的字符串透传（MySQL 收到 JSON 字符串自动转 JSON 类型）。
    - ``finished_at`` 仅终态时填——历史任务的 ``finished_at`` 保留首次值。
    """
    result: Optional[AnalysisResult] = state.result
    video_meta = result.video_meta if result else None
    global_metrics = result.global_metrics if result else None

    # warnings: PhaseResult.risks 不计入（那是 task_risks 的事，2026-09 M3.2 补）
    warnings_json = None
    if result and result.warnings:
        warnings_json = json.dumps(result.warnings, ensure_ascii=False)

    result_json_str = None
    if result is not None:
        try:
            result_json_str = result.model_dump_json()
        except Exception as exc:  # noqa: BLE001 —— pydantic 兜底
            logger.warning("result_json 序列化失败: %s", exc)

    is_terminal = state.status in (TaskStatus.SUCCESS, TaskStatus.FAILED)
    finished_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(state.updated_at)) if is_terminal else None

    return (
        state.task_id,
        state.status.value if isinstance(state.status, TaskStatus) else str(state.status),
        state.progress,
        state.step,
        state.step_text or "",
        state.message or "",
        state.error_code.value if state.error_code else None,
        state.error_message,
        # camera_view：优先取 result.video_meta（已确定），否则取 state 默认
        (video_meta.camera_view.value if video_meta else state.camera_view.value),
        video_meta.fps if video_meta else 0.0,
        video_meta.duration if video_meta else 0.0,
        video_meta.width if video_meta else 0,
        video_meta.height if video_meta else 0,
        video_meta.frame_count if video_meta else 0,
        video_meta.total_frames if video_meta else 0,
        video_meta.sample_step if video_meta else 1,
        1 if (video_meta and video_meta.low_fps) else 0,
        video_meta.orientation if video_meta else 0,
        global_metrics.tempo_ratio if global_metrics else None,
        global_metrics.swing_duration if global_metrics else None,
        global_metrics.max_head_drift_pct if global_metrics else None,
        warnings_json,
        result.disclaimer if result else "",
        result_json_str,
        state.video_path,
        state.out_dir,
        state.openid,
        finished_at,
    )


def upsert_task(state: TaskState) -> bool:
    """写入或更新 tasks 表一行（``INSERT ... ON DUPLICATE KEY UPDATE``）。

    失败仅 warn（不抛），返回 False。调用方**不应据此阻断主流程**。
    """
    try:
        affected = db.execute(_TASK_UPSERT_SQL, _row_for_task(state))
    except Exception as exc:  # noqa: BLE001 —— 旁路，吞所有
        logger.warning("tasks upsert 失败（已忽略）: %s err=%s", state.task_id, exc)
        return False
    if not affected:
        logger.debug("tasks upsert 影响行数为 0: %s", state.task_id)
    return True


def delete_task(task_id: str) -> bool:
    """从 tasks 表删除一行（用户主动撤销等场景）。"""
    try:
        affected = db.execute("DELETE FROM tasks WHERE task_id = %s", (task_id,))
    except Exception as exc:  # noqa: BLE001
        logger.warning("tasks delete 失败（已忽略）: %s err=%s", task_id, exc)
        return False
    return bool(affected)


# ---------------------------------------------------------------------------
# task_phases 表
# ---------------------------------------------------------------------------

_PHASE_INSERT_SQL = (
    "INSERT INTO task_phases ("
    "  task_id, phase_index, phase_key, name_cn, name_en,"
    "  frame_index, timestamp_sec, estimated, image_url,"
    "  source, confidence"
    ") VALUES ("
    "  %s, %s, %s, %s, %s,"
    "  %s, %s, %s, %s,"
    "  %s, %s"
    ")"
)


def _row_for_phase(task_id: str, phase: PhaseResult) -> Sequence:
    """PhaseResult -> task_phases 行参数。"""
    return (
        task_id,
        phase.index,
        phase.key.value if isinstance(phase.key, PhaseKey) else str(phase.key),
        phase.name_cn or "",
        phase.name_en or "",
        phase.frame_index,
        phase.timestamp,
        1 if phase.estimated else 0,
        phase.image_url or "",
        # source / confidence：当前 pipeline 还未回填（schema 允许 NULL），
        # 留 NULL 是诚实的——M3.2 接 SwingNet 时再写。
        None,
        None,
    )


def insert_phases(task_id: str, phases: List[PhaseResult]) -> int:
    """批量写入 8 阶段行。失败 warn；返回实际写入条数。

    Note:
        当前实现是「先删后插」——同一 task_id 重复调用是幂等的。
        缺点：``frame_index`` 已经被手动微调过的「调整后结果」会被覆盖。
        但 MVP 没有 UI 持久化调整，所以重复写只在 pipeline 重跑时出现，
        此时本来就该用最新结果。
    """
    if not phases:
        return 0
    rows = [_row_for_phase(task_id, p) for p in phases]
    try:
        # 先清旧行（幂等）
        db.execute("DELETE FROM task_phases WHERE task_id = %s", (task_id,))
        # 再批量插新行
        affected = db.executemany(_PHASE_INSERT_SQL, rows)
    except Exception as exc:  # noqa: BLE001
        logger.warning("task_phases 写入失败（已忽略）: %s err=%s", task_id, exc)
        return 0
    return int(affected or 0)


# ---------------------------------------------------------------------------
# rehydrate —— 启动时从 DB 回灌内存
# ---------------------------------------------------------------------------

_LOAD_TERMINAL_SQL = (
    "SELECT task_id, status, progress, step, step_text, message,"
    "       error_code, error_message,"
    "       camera_view, video_path, out_dir, openid,"
    "       result_json,"
    "       UNIX_TIMESTAMP(updated_at) AS updated_at_ts,"
    "       UNIX_TIMESTAMP(created_at) AS created_at_ts"
    "  FROM tasks"
    " WHERE status IN ('success', 'failed')"
    "   AND updated_at >= DATE_SUB(NOW(), INTERVAL %s HOUR)"
    " ORDER BY updated_at DESC"
    " LIMIT %s"
)


def _row_to_state(row: Dict[str, Any]) -> TaskState:
    """把 SELECT 的一行还原成内存 TaskState。"""
    # 状态字符串 -> enum
    try:
        status = TaskStatus(row["status"])
    except ValueError:
        status = TaskStatus.FAILED

    # camera_view 字符串 -> enum（兜底 FACE_ON）
    try:
        camera_view = CameraView(row["camera_view"])
    except (KeyError, ValueError):
        camera_view = CameraView.FACE_ON

    # result_json -> AnalysisResult（可选）
    result: Optional[AnalysisResult] = None
    raw_result = row.get("result_json")
    if raw_result:
        try:
            # MySQL JSON 列查出来可能是 dict（取决于 driver），也可能是 str
            if isinstance(raw_result, str):
                result = AnalysisResult.model_validate_json(raw_result)
            else:
                result = AnalysisResult.model_validate(raw_result)
        except Exception as exc:  # noqa: BLE001 —— pydantic 容错
            logger.warning("rehydrate result_json 解析失败: %s err=%s", row.get("task_id"), exc)
            result = None

    return TaskState(
        task_id=row["task_id"],
        status=status,
        progress=int(row.get("progress") or 0),
        step=int(row.get("step") or 1),
        step_text=row.get("step_text") or "",
        message=row.get("message") or "",
        error_code=row.get("error_code"),
        error_message=row.get("error_message"),
        result=result,
        video_path=row.get("video_path"),
        out_dir=row.get("out_dir"),
        camera_view=camera_view,
        openid=row.get("openid"),
        created_at=float(row.get("created_at_ts") or 0.0),
        updated_at=float(row.get("updated_at_ts") or 0.0),
    )


def load_terminal_tasks(
    within_hours: Optional[int] = None,
    limit: int = 1000,
) -> List[TaskState]:
    """从 tasks 表读出最近 N 小时内的终态任务。"""
    hours = within_hours if within_hours is not None else int(config.RESULT_TTL_HOURS)
    try:
        rows = db.fetchall(_LOAD_TERMINAL_SQL, (hours, limit))
    except Exception as exc:  # noqa: BLE001
        logger.warning("rehydrate 读 tasks 失败（已忽略）: err=%s", exc)
        return []
    return [_row_to_state(r) for r in rows]
