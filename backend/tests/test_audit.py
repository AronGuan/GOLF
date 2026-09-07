"""``app.audit`` 单元测试 —— 操作记录（operation_logs）统一写入口。

覆盖重点：
1. action 常量与中文名映射（5 个 action 齐全）
2. **审计是旁路：DB 挂了/写入失败绝不能外抛**（最关键的性质）
3. 匿名操作（openid=None）照常记录
4. detail JSON 序列化 + 序列化失败时的兜底
5. 超长 UA / fail_reason 截断到 schema 列宽
"""

from __future__ import annotations

import json

import pytest

from app import audit


class FakeDB:
    """假数据库：记录 SQL，可选让 execute 抛异常。"""

    def __init__(self, *, raise_on_execute: bool = False, affected: int = 1) -> None:
        self.calls: list = []
        self.raise_on_execute = raise_on_execute
        self.affected = affected

    def execute(self, sql, args=None):
        if self.raise_on_execute:
            raise RuntimeError("db down")
        self.calls.append((sql, args))
        return self.affected


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(audit, "db", db)
    return db


# ---------------------------------------------------------------------------
# 一、action 常量
# ---------------------------------------------------------------------------


def test_all_five_actions_defined():
    """5 个 action 齐全，且与 002_auth.sql 的 schema 注释一致。"""
    assert audit.LOGIN == "login"
    assert audit.UPLOAD == "upload"
    assert audit.VIEW_RESULT == "view_result"
    assert audit.UPDATE_AVATAR == "update_avatar"
    assert audit.UPDATE_NICKNAME == "update_nickname"


def test_every_action_has_chinese_name():
    """每个 action 都要有中文名（运营看板直接展示，不能空白）。"""
    for action in (
        audit.LOGIN,
        audit.UPLOAD,
        audit.VIEW_RESULT,
        audit.UPDATE_AVATAR,
        audit.UPDATE_NICKNAME,
    ):
        assert action in audit.ACTION_NAMES, f"{action} 缺中文名"
        assert audit.ACTION_NAMES[action], f"{action} 中文名为空"


# ---------------------------------------------------------------------------
# 二、写入行为
# ---------------------------------------------------------------------------


def test_log_writes_expected_columns(fake_db):
    """写入一条记录，各列取值正确。"""
    assert audit.log_operation(
        audit.UPLOAD,
        openid="oABC",
        task_id="task123",
        detail={"bytes": 1024},
        ip="1.2.3.4",
        user_agent="UA",
        duration_ms=250,
    ) is True

    sql, args = fake_db.calls[0]
    assert "INSERT INTO operation_logs" in sql
    # 列顺序：(openid, action, action_name, task_id, detail, result,
    #          fail_reason, ip, user_agent, duration_ms)
    assert args[0] == "oABC"
    assert args[1] == "upload"
    assert args[2] == "上传分析"
    assert args[3] == "task123"
    assert json.loads(args[4]) == {"bytes": 1024}
    assert args[5] == "success"
    assert args[7] == "1.2.3.4"
    assert args[8] == "UA"
    assert args[9] == 250


def test_log_anonymous_operation_still_writes(fake_db):
    """匿名操作（openid=None）也要记录——「未登录用户上传量」要看板用。"""
    assert audit.log_operation(audit.UPLOAD, task_id="t1") is True
    _, args = fake_db.calls[0]
    assert args[0] is None  # openid 允许 NULL（schema 已声明）


def test_log_returns_false_when_zero_affected(fake_db):
    """写入 0 行（如 DB 静默降级）返回 False，但不抛异常。"""
    fake_db.affected = 0
    assert audit.log_operation(audit.LOGIN) is False


# ---------------------------------------------------------------------------
# 三、审计是旁路（最关键的性质）
# ---------------------------------------------------------------------------


def test_log_never_raises_when_db_broken(monkeypatch):
    """DB 抛异常时**绝不外抛**——审计失败不能拖垮主流程。"""
    monkeypatch.setattr(audit, "db", FakeDB(raise_on_execute=True))

    # 不抛异常，只返回 False
    assert audit.log_operation(audit.VIEW_RESULT, openid="oX") is False


def test_log_survives_unserializable_detail(fake_db):
    """detail 含不可 JSON 序列化的对象时，丢弃 detail 但记录照写。"""
    assert audit.log_operation(
        audit.UPDATE_AVATAR, openid="oX", detail={"bad": object()}
    ) is True  # 仍写入成功

    _, args = fake_db.calls[0]
    assert args[4] is None  # detail 被丢弃而不是让整条记录失败


# ---------------------------------------------------------------------------
# 四、长度截断（与 schema 列宽一致，防 Data too long 报错）
# ---------------------------------------------------------------------------


def test_user_agent_truncated_to_512(fake_db):
    """UA 超长截断到 512（schema: user_agent VARCHAR(512)）。"""
    audit.log_operation(audit.LOGIN, user_agent="U" * 5000)
    _, args = fake_db.calls[0]
    assert len(args[8]) == 512


def test_empty_user_agent_becomes_null(fake_db):
    """空 UA 存 NULL 而非空串（便于看板区分「没传」和「传了空」）。"""
    audit.log_operation(audit.LOGIN, user_agent="")
    _, args = fake_db.calls[0]
    assert args[8] is None


def test_fail_reason_truncated_to_255(fake_db):
    """fail_reason 截断到 255（schema: VARCHAR(255)）。"""
    audit.log_operation(
        audit.UPLOAD, result="fail", fail_reason="E" * 1000
    )
    _, args = fake_db.calls[0]
    assert args[5] == "fail"
    assert len(args[6]) == 255


# ---------------------------------------------------------------------------
# 五、detail 序列化
# ---------------------------------------------------------------------------


def test_detail_json_preserves_chinese(fake_db):
    """中文 detail 不被转义成 \\uXXXX（便于直接查库人读）。"""
    audit.log_operation(audit.UPDATE_NICKNAME, detail={"name": "老虎"})
    _, args = fake_db.calls[0]
    assert "老虎" in args[4]  # ensure_ascii=False
    assert json.loads(args[4])["name"] == "老虎"


def test_detail_none_when_not_provided(fake_db):
    """不传 detail 时该列为 NULL。"""
    audit.log_operation(audit.LOGIN, openid="oX")
    _, args = fake_db.calls[0]
    assert args[4] is None


def test_detail_json_escapes_quotes_and_backslashes(fake_db):
    """字符串里的引号/反斜杠必须转义，否则日志 JSON 会被破坏。

    该用例原本属于 user._detail_json（手搓 JSON）；2026-09-07 收敛到
    audit 模块后改由标准 json.dumps 处理，行为仍需保证。
    """
    payload = {"note": 'has "quotes" and \\backslashes'}
    audit.log_operation(audit.UPDATE_AVATAR, detail=payload)
    _, args = fake_db.calls[0]
    assert json.loads(args[4]) == payload


def test_detail_json_handles_bool_and_numbers(fake_db):
    """bool / int / float 保持原类型，不被降级成字符串。"""
    payload = {"flag": True, "n": 42, "x": 1.5}
    audit.log_operation(audit.UPLOAD, detail=payload)
    _, args = fake_db.calls[0]
    parsed = json.loads(args[4])
    assert parsed == payload
    assert parsed["flag"] is True
