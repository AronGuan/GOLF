"""用户操作记录（``operation_logs``）统一写入口。

背景
----
``operation_logs`` 的定位是**行为审计 + 运营看板 + 频率计数**三合一，
与 4 张 task 业务表（tasks / task_metrics / task_phases / task_risks）正交：

- 业务表存「分析结果是什么」
- 本表存「用户做了什么」

2026-09-07 补齐：此前只有 ``update_avatar`` / ``update_nickname`` 两个 action
落地（user.py 直写 SQL），``login`` / ``upload`` / ``view_result`` 三个 action
schema 已预留但代码未接。现统一收敛到本模块。

设计约束
--------
1. **写入失败绝不抛异常**：审计是旁路，DB 挂了不能拖垮主流程
   （与 db 模块的「静默降级」语义一致）。
2. **匿名操作也记**：``openid=None`` 时照常写库（schema 允许 NULL），
   这样「未登录用户的上传量」也能进看板。
3. **action 集中定义**：新增 action 只改本文件，调用方不会写错字符串。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from . import db

logger = logging.getLogger("app.audit")

# ---------------------------------------------------------------------------
# action 常量（与 002_auth.sql 中 operation_logs.action 注释对齐）
# ---------------------------------------------------------------------------

LOGIN = "login"
UPLOAD = "upload"
VIEW_RESULT = "view_result"
UPDATE_AVATAR = "update_avatar"
UPDATE_NICKNAME = "update_nickname"

#: action -> 中文名（运营看板直接展示）
ACTION_NAMES: Dict[str, str] = {
    LOGIN: "微信登录",
    UPLOAD: "上传分析",
    VIEW_RESULT: "查看结果",
    UPDATE_AVATAR: "修改头像",
    UPDATE_NICKNAME: "修改昵称",
}

#: detail JSON 序列化失败时的兜底值
_DETAIL_FALLBACK = None


def _detail_json(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    """把上下文字典序列化为 JSON 字符串；失败返回 None（detail 可空）。"""
    if not payload:
        return None
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        logger.warning("audit detail 序列化失败，丢弃 detail: %s", exc)
        return _DETAIL_FALLBACK


def log_operation(
    action: str,
    *,
    openid: Optional[str] = None,
    task_id: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
    result: str = "success",
    fail_reason: Optional[str] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> bool:
    """写一条操作记录。

    Args:
        action: :data:`LOGIN` / :data:`UPLOAD` / :data:`VIEW_RESULT`
            / :data:`UPDATE_AVATAR` / :data:`UPDATE_NICKNAME` 之一。
        openid: 用户标识；``None`` 表示匿名操作（照常记录）。
        task_id: 关联任务 id，无则 ``None``。
        detail: 结构化上下文（会序列化为 JSON 存入 detail 列）。
        result: ``success`` / ``fail``。
        fail_reason: 失败原因（result='fail' 时有意义）。
        ip: 客户端 IP。
        user_agent: UA（超长会被截断到 512，与 schema 一致）。
        duration_ms: 耗时毫秒（分析类操作）。

    Returns:
        写入成功（受影响行数 > 0）返回 True；DB 不可用 / 写入失败返回 False。
        **调用方不应据此阻断主流程。**
    """
    action_name = ACTION_NAMES.get(action, action)
    ua = (user_agent or "")[:512] or None

    try:
        affected = db.execute(
            "INSERT INTO operation_logs "
            "(openid, action, action_name, task_id, detail, result, "
            " fail_reason, ip, user_agent, duration_ms) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                openid,
                action,
                action_name,
                task_id,
                _detail_json(detail),
                result,
                (fail_reason or "")[:255] or None,
                ip,
                ua,
                duration_ms,
            ),
        )
    except Exception as exc:  # noqa: BLE001 —— 审计旁路，任何异常都不得外抛
        logger.warning("audit log 写入失败（已忽略）: action=%s err=%s", action, exc)
        return False

    return bool(affected)
