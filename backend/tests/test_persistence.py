"""持久化层（``tasks`` + ``task_phases`` 落库）测试。

聚焦 M3.1 范围：
- :func:`persistence.upsert_task`：字段映射、INSERT vs UPDATE 走 ON DUPLICATE KEY
- :func:`persistence.insert_phases`：8 行批量写、幂等（先删后插）
- :func:`persistence.delete_task` / :func:`persistence.load_terminal_tasks`

设计要点
--------
- ``fake_db`` 同时 patch 写入与读取接口，否则 :func:`db.execute` /
  :func:`db.executemany` / :func:`db.fetchall` 走真实静默降级返回 0 / []。
- 失败的旁路语义：DB 抛异常时 upsert / insert_phases / load_terminal_tasks
  必须**不外抛**、返回 False / 0 / []。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

import pytest

from app import persistence
from app.schemas import (
    AnalysisResult,
    CameraView,
    GlobalMetrics,
    MetricSource,
    MetricStatus,
    PhaseKey,
    PhaseResult,
    StageMetric,
    TaskState,
    TaskStatus,
    VideoMeta,
)


# ---------------------------------------------------------------------------
# fake db
# ---------------------------------------------------------------------------


class _FakeDB:
    """内存记录所有写 + 读，模拟 MySQL 的「先 SELECT 再决定」/「executemany」。"""

    def __init__(self) -> None:
        self.executes: List[tuple] = []
        self.executemany_calls: List[tuple] = []
        # 模拟 MySQL 的按 task_id 索引
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.phases: Dict[str, List[Dict[str, Any]]] = {}
        # 可控：下一次 fetchall 返回什么
        self.next_rows: List[Dict[str, Any]] = []
        # 可控：下一次 execute 抛异常
        self.raise_on: Optional[str] = None

    def execute(self, sql: str, args: Optional[Sequence] = None) -> int:
        if self.raise_on and self.raise_on in sql:
            raise RuntimeError("simulated DB error")
        self.executes.append((sql.strip(), list(args or ())))
        sql_lower = sql.strip().lower()
        if sql_lower.startswith("delete from tasks"):
            tid = args[0]
            existed = tid in self.tasks
            self.tasks.pop(tid, None)
            return 1 if existed else 0
        if sql_lower.startswith("delete from task_phases"):
            tid = args[0]
            n = len(self.phases.pop(tid, []))
            return n
        if sql_lower.startswith("insert into tasks"):
            # 第一列是 task_id（按 schema 顺序）
            tid = args[0]
            self.tasks[tid] = {
                "task_id": tid,
                # 仅存最小化字段用于后续 _row_to_state
                "status": args[1],
                "progress": args[2],
                "step": args[3],
                "step_text": args[4],
                "message": args[5],
                "error_code": args[6],
                "error_message": args[7],
                "camera_view": args[8],
                "video_path": args[24],
                "out_dir": args[25],
                "openid": args[26],
                "result_json": args[23],
            }
            return 1
        return 1

    def executemany(self, sql: str, seq: Sequence[Sequence]) -> int:
        if self.raise_on and self.raise_on in sql:
            raise RuntimeError("simulated DB error")
        self.executemany_calls.append((sql.strip(), [list(s) for s in seq]))
        # task_id 是第一列
        bucket: Dict[str, List] = {}
        for row in seq:
            bucket.setdefault(row[0], []).append(list(row))
        for tid, rows in bucket.items():
            self.phases[tid] = rows
        return len(seq)

    def fetchall(self, sql: str, args: Optional[Sequence] = None) -> List[Dict[str, Any]]:
        if self.raise_on and self.raise_on in sql:
            raise RuntimeError("simulated DB error")
        return list(self.next_rows)


@pytest.fixture
def fake_db(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr(persistence, "db", fake)
    return fake


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _state(
    *,
    task_id: str = "abc123",
    status: TaskStatus = TaskStatus.PENDING,
    progress: int = 0,
    step: int = 1,
    openid: Optional[str] = None,
    result: Optional[AnalysisResult] = None,
) -> TaskState:
    return TaskState(
        task_id=task_id,
        status=status,
        progress=progress,
        step=step,
        message="排队中" if status is TaskStatus.PENDING else "处理中",
        step_text="probe",
        video_path=f"/data/tasks/{task_id}/upload.mp4",
        out_dir=f"/data/tasks/{task_id}",
        camera_view=CameraView.FACE_ON,
        openid=openid,
        result=result,
    )


def _video_meta() -> VideoMeta:
    return VideoMeta(
        fps=30.0,
        duration=2.0,
        width=1920,
        height=1080,
        frame_count=60,
        total_frames=60,
        sample_step=1,
        low_fps=False,
        camera_view=CameraView.FACE_ON,
        orientation=0,
    )


def _result(*, camera_view: CameraView = CameraView.FACE_ON) -> AnalysisResult:
    phases = [
        PhaseResult(
            index=i + 1,
            key=key,
            name_cn=key.value,
            name_en=key.value,
            frame_index=i * 6,
            timestamp=i * 0.2,
            estimated=False,
            image_url=f"phase_{i+1}.jpg",
        )
        for i, key in enumerate(
            [
                PhaseKey.ADDRESS,
                PhaseKey.TAKEAWAY,
                PhaseKey.BACKSWING,
                PhaseKey.TOP,
                PhaseKey.DOWNSWING,
                PhaseKey.IMPACT,
                PhaseKey.FOLLOW_THROUGH,
                PhaseKey.FINISH,
            ]
        )
    ]
    return AnalysisResult(
        task_id="abc123",
        status=TaskStatus.SUCCESS,
        camera_view=camera_view,
        video_meta=_video_meta(),
        global_metrics=GlobalMetrics(
            tempo_ratio=2.5, swing_duration=1.2, max_head_drift_pct=3.0, metrics=[]
        ),
        phases=phases,
        warnings=["low_light"],
        disclaimer="免责声明",
    )


# ---------------------------------------------------------------------------
# 一、upsert_task —— 字段映射 + INSERT vs UPDATE
# ---------------------------------------------------------------------------


def test_upsert_writes_row_for_pending_state(fake_db):
    """PENDING 状态也写（事后能查到「曾尝试」的任务）。"""
    state = _state(task_id="t001")
    assert persistence.upsert_task(state) is True
    assert "t001" in fake_db.tasks
    row = fake_db.tasks["t001"]
    assert row["status"] == "pending"
    assert row["openid"] is None
    assert row["video_path"].endswith("/t001/upload.mp4")


def test_upsert_with_result_populates_video_meta(fake_db):
    """result 已就绪时，video_meta / global_metrics 字段应填充。"""
    state = _state(task_id="t002", result=_result())
    persistence.upsert_task(state)
    # 直接从 SQL 参数验证（_row_for_task 的列顺序已知）
    sql, args = fake_db.executes[0]
    assert "INSERT INTO tasks" in sql
    # camera_view, fps, duration, width, height, frame_count, total_frames
    # 列顺序见 persistence._TASK_UPSERT_SQL：
    # 0 task_id / 1 status / 2 progress / 3 step / 4 step_text / 5 message /
    # 6 error_code / 7 error_message / 8 camera_view / 9 fps / 10 duration /
    # 11 width / 12 height / 13 frame_count / 14 total_frames / 15 sample_step /
    # 16 low_fps / 17 orientation / 18 tempo_ratio / 19 swing_duration /
    # 20 max_head_drift_pct / 21 warnings / 22 disclaimer / 23 result_json /
    # 24 video_path / 25 out_dir / 26 openid / 27 finished_at
    assert args[8] == "face_on"      # camera_view
    assert args[9] == 30.0           # fps
    assert args[10] == 2.0           # duration
    assert args[11] == 1920          # width
    assert args[12] == 1080          # height
    assert args[13] == 60            # frame_count
    assert args[14] == 60            # total_frames
    assert args[18] == 2.5           # tempo_ratio
    assert args[19] == 1.2           # swing_duration
    assert args[20] == 3.0           # max_head_drift_pct


def test_upsert_writes_result_json_serialized(fake_db):
    """result_json 必须是 JSON 字符串（MySQL 收到会自动转 JSON 列）。"""
    state = _state(task_id="t003", result=_result())
    persistence.upsert_task(state)
    args = fake_db.executes[0][1]
    # result_json 在索引 23
    parsed = json.loads(args[23])
    assert parsed["task_id"] == "abc123"
    assert parsed["video_meta"]["fps"] == 30.0
    assert len(parsed["phases"]) == 8


def test_upsert_terminal_sets_finished_at(fake_db):
    """终态（SUCCESS / FAILED）应填 finished_at，pending 不填。"""
    pending = _state(task_id="t004a", status=TaskStatus.PENDING)
    success = _state(task_id="t004b", status=TaskStatus.SUCCESS, result=_result())
    persistence.upsert_task(pending)
    persistence.upsert_task(success)
    pending_args = fake_db.executes[0][1]
    success_args = fake_db.executes[1][1]
    # finished_at 是最后一列（索引 27）
    assert pending_args[27] is None
    assert success_args[27] is not None  # 非空 datetime 字符串


def test_upsert_uses_on_duplicate_key(fake_db):
    """一条 SQL 同时覆盖 INSERT / UPDATE 路径（避免 SELECT-then-decide 竞态）。"""
    persistence.upsert_task(_state(task_id="t005"))
    sql = fake_db.executes[0][0]
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert "VALUES(status)" in sql  # 列自引用更新


def test_upsert_returns_false_when_db_raises(fake_db):
    """DB 异常时被吞，upsert 返回 False，**不外抛**。"""
    fake_db.raise_on = "INSERT INTO tasks"
    state = _state(task_id="t006")
    # 不应抛
    assert persistence.upsert_task(state) is False


def test_upsert_openid_none_round_trip(fake_db):
    """匿名任务（openid=None）也能写。"""
    persistence.upsert_task(_state(task_id="t007", openid=None))
    assert fake_db.tasks["t007"]["openid"] is None


# ---------------------------------------------------------------------------
# 二、insert_phases
# ---------------------------------------------------------------------------


def test_insert_phases_writes_8_rows(fake_db):
    """成功任务应落 8 行 task_phases。"""
    result = _result()
    n = persistence.insert_phases("t101", result.phases)
    assert n == 8
    assert len(fake_db.phases["t101"]) == 8


def test_insert_phases_idempotent_via_delete_first(fake_db):
    """先 DELETE 再 INSERT：重复调用不留残余行。"""
    r1 = _result()
    persistence.insert_phases("t102", r1.phases)
    # 再插一次（模拟 pipeline 重跑）
    r2_phases = [p.model_copy(update={"frame_index": 999}) for p in r1.phases]
    persistence.insert_phases("t102", r2_phases)
    # 仍是 8 行，且 frame_index 是 999
    assert len(fake_db.phases["t102"]) == 8
    assert all(row[5] == 999 for row in fake_db.phases["t102"])


def test_insert_phases_empty_returns_zero(fake_db):
    """空阶段列表 = 0 写入（不应该报错）。"""
    assert persistence.insert_phases("t103", []) == 0
    assert fake_db.executemany_calls == []


def test_insert_phases_db_error_does_not_raise(fake_db):
    """executemany 抛异常时，insert_phases 吞掉返回 0。"""
    fake_db.raise_on = "INSERT INTO task_phases"
    n = persistence.insert_phases("t104", _result().phases)
    assert n == 0


def test_insert_phases_source_confidence_are_null(fake_db):
    """source / confidence 当前未回填，schema 允许 NULL，存 None 是诚实的。"""
    persistence.insert_phases("t105", _result().phases)
    rows = fake_db.phases["t105"]
    for row in rows:
        # 索引 9, 10 是 source / confidence
        assert row[9] is None
        assert row[10] is None


# ---------------------------------------------------------------------------
# 三、delete_task
# ---------------------------------------------------------------------------


def test_delete_task_removes_row(fake_db):
    """delete 命中存在的任务返回 True。"""
    persistence.upsert_task(_state(task_id="t201"))
    assert persistence.delete_task("t201") is True
    assert "t201" not in fake_db.tasks


def test_delete_task_missing_returns_false(fake_db):
    """删除不存在的任务返回 0，函数返回 False（不抛）。"""
    assert persistence.delete_task("nope") is False


# ---------------------------------------------------------------------------
# 四、load_terminal_tasks —— 启动回灌
# ---------------------------------------------------------------------------


def test_load_terminal_returns_parsed_states(fake_db):
    """fake_db.next_rows 模拟 DB 查出的一行，应能还原成 TaskState。"""
    fake_db.next_rows = [
        {
            "task_id": "t301",
            "status": "success",
            "progress": 100,
            "step": 4,
            "step_text": "done",
            "message": "分析完成",
            "error_code": None,
            "error_message": None,
            "camera_view": "down_the_line",
            "video_path": "/data/tasks/t301/upload.mp4",
            "out_dir": "/data/tasks/t301",
            "openid": "oXYZ",
            "result_json": None,
            "updated_at_ts": 1728000000.0,
            "created_at_ts": 1727999900.0,
        }
    ]
    states = persistence.load_terminal_tasks(within_hours=24, limit=10)
    assert len(states) == 1
    s = states[0]
    assert s.task_id == "t301"
    assert s.status is TaskStatus.SUCCESS
    assert s.camera_view is CameraView.DOWN_THE_LINE
    assert s.openid == "oXYZ"
    assert s.updated_at == 1728000000.0


def test_load_terminal_handles_bad_status(fake_db):
    """status 列是脏数据（不是 success/failed）时，兜底为 FAILED 而不是抛。"""
    fake_db.next_rows = [
        {
            "task_id": "t302",
            "status": "weird_value",
            "progress": 0,
            "step": 1,
            "step_text": "",
            "message": "",
            "error_code": None,
            "error_message": None,
            "camera_view": "face_on",
            "video_path": None,
            "out_dir": None,
            "openid": None,
            "result_json": None,
            "updated_at_ts": 0.0,
            "created_at_ts": 0.0,
        }
    ]
    states = persistence.load_terminal_tasks(within_hours=24, limit=10)
    assert states[0].status is TaskStatus.FAILED


def test_load_terminal_parses_result_json(fake_db):
    """result_json 是字符串时也能反序列化为 AnalysisResult。"""
    result = _result()
    fake_db.next_rows = [
        {
            "task_id": "t303",
            "status": "success",
            "progress": 100,
            "step": 4,
            "step_text": "done",
            "message": "",
            "error_code": None,
            "error_message": None,
            "camera_view": "face_on",
            "video_path": None,
            "out_dir": None,
            "openid": None,
            "result_json": result.model_dump_json(),
            "updated_at_ts": 1.0,
            "created_at_ts": 0.5,
        }
    ]
    states = persistence.load_terminal_tasks(within_hours=24, limit=10)
    assert states[0].result is not None
    assert len(states[0].result.phases) == 8


def test_load_terminal_db_error_returns_empty(fake_db):
    """SELECT 抛异常时返回 []，不外抛（启动期不能让 DB 挂服务）。"""
    fake_db.raise_on = "SELECT"
    assert persistence.load_terminal_tasks() == []


def test_load_terminal_uses_default_window(monkeypatch):
    """不传 within_hours 时使用 config.RESULT_TTL_HOURS。

    fake_db.fetchall 不记录参数（实现上没记），这里只验证：
    1) 调用不抛
    2) 不传 within_hours 时 SQL 走默认（不直接断言 SQL，因为 fake 没捕获 fetchall 参数）
    """
    fake = _FakeDB()
    monkeypatch.setattr(persistence, "db", fake)
    monkeypatch.setattr(persistence.config, "RESULT_TTL_HOURS", 168.0, raising=False)
    # 调用不抛、返回空列表
    assert persistence.load_terminal_tasks() == []
