"""用户资料修改（方案 M2.5：§3.5）。

功能
----
- 头像上传：``update_avatar(openid, bytes)`` → 落盘 + 更新 DB + 写日志
- 昵称修改：``update_nickname(openid, nickname)`` → 更新 DB + 写日志

约束（来自方案 §3.5）
---------------------
- 头像必须 PNG / JPEG（**按文件 magic bytes 判断**，不信扩展名或 content-type）
- 头像 ≤ 1MB（与微信 imgSecCheck 上限一致）
- 昵称长度 1~16 字符
- 头像每日 ≤ 10 次，昵称每日 ≤ 5 次（防刷接口）
- 所有失败（违规/超大/超限/未登录）一律返回 ``None`` —— 失败是合规底线
  （与登录失败「放行」的处理**完全相反**）

安全
----
- 不解析用户提供的文件名（避免 ``../../etc/passwd``）
- 落盘路径用 ``sha256(openid)[:16] + ext`` 决定，固定名**覆盖写**——
  同一用户反复换头像不产生垃圾文件，无需清理任务
- 头像 URL 含 ``?v=<更新时间戳>``，绕过 ``<image>`` 缓存
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from . import config, db

logger = logging.getLogger(__name__)

#: 头像大小上限：1MB（与微信 ``img_sec_check`` 一致）
AVATAR_MAX_BYTES: int = 1 * 1024 * 1024

#: 头像 / 昵称修改频率上限（每日）
AVATAR_DAILY_LIMIT: int = 10
NICKNAME_DAILY_LIMIT: int = 5

#: 昵称长度限制
NICKNAME_MIN_LEN: int = 1
NICKNAME_MAX_LEN: int = 16

#: 文件 magic bytes —— **不信扩展名**，按头判断
_PNG_MAGIC: bytes = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC: bytes = b"\xff\xd8\xff"


# ---------------------------------------------------------------------------
# 一、纯函数（便于测试）
# ---------------------------------------------------------------------------


def detect_avatar_format(data: bytes) -> Optional[str]:
    """识别图片格式，返回 ``.png`` / ``.jpg`` 或 ``None``（不支持）。"""
    if not data:
        return None
    if len(data) >= 8 and data[:8] == _PNG_MAGIC:
        return ".png"
    if len(data) >= 3 and data[:3] == _JPEG_MAGIC:
        return ".jpg"
    return None


def avatar_path_for(openid: str, ext: str) -> str:
    """``openid`` → 固定头像文件名（含扩展名）。用于落盘与 URL。

    同一 openid 永远对应同一文件名，覆盖写不堆积。
    """
    digest = hashlib.sha256(openid.encode("utf-8")).hexdigest()
    return f"{digest[:16]}{ext}"


# ---------------------------------------------------------------------------
# 一.5、失败分类（路由层在调 update_* 前先分类，给前端明确提示）
#
# 为什么单独写：update_avatar 失败只返回 None，但路由要区分
# "文件过大 / 格式错 / 频率超限 / 未登录" 等原因，不能都笼统说"头像保存失败"。
# ---------------------------------------------------------------------------


# 失败码常量（路由层映射到 HTTP / 业务码）
ERR_OPENID: str = "openid"
ERR_EMPTY: str = "empty"
ERR_SIZE: str = "size"
ERR_FORMAT: str = "format"
ERR_LIMIT: str = "limit"


def classify_avatar_failure(openid: str, data: bytes) -> Optional[str]:
    """预判头像上传会失败的**原因**，无失败返回 ``None``（不保证成功）。

    用于路由层在 :func:`update_avatar` 前先映射错误码给前端。
    顺序：openid → empty → size → format → limit（按"最便宜的检查优先"）。
    """
    if not openid:
        return ERR_OPENID
    if not data:
        return ERR_EMPTY
    if len(data) > AVATAR_MAX_BYTES:
        return ERR_SIZE
    if detect_avatar_format(data) is None:
        return ERR_FORMAT
    if _today_action_count(openid, "update_avatar") >= AVATAR_DAILY_LIMIT:
        return ERR_LIMIT
    return None


def classify_nickname_failure(openid: str, nickname: str) -> Optional[str]:
    """预判昵称修改会失败的原因。"""
    if not openid:
        return ERR_OPENID
    nickname = (nickname or "").strip()
    if not (NICKNAME_MIN_LEN <= len(nickname) <= NICKNAME_MAX_LEN):
        return ERR_FORMAT  # 长度也归"格式"类
    if _today_action_count(openid, "update_nickname") >= NICKNAME_DAILY_LIMIT:
        return ERR_LIMIT
    return None


# ---------------------------------------------------------------------------
# 二、修改流程
# ---------------------------------------------------------------------------


def update_avatar(openid: str, data: bytes) -> Optional[Dict[str, Any]]:
    """头像落盘 + 更新 DB + 写日志。

    Args:
        openid: 当前登录用户。
        data: 文件原始字节。

    Returns:
        成功返回 ``{avatar_url, updated_at}``；失败返回 ``None``。
        调用方应将 ``None`` 映射为具体的错误码（频率/格式/大小）。
    """
    if not openid:
        return None
    if not data:
        return None
    if len(data) > AVATAR_MAX_BYTES:
        logger.warning("update_avatar: size %s > %s", len(data), AVATAR_MAX_BYTES)
        return None
    ext = detect_avatar_format(data)
    if ext is None:
        logger.warning("update_avatar: not PNG/JPEG (magic=%s...)", data[:8].hex())
        return None

    # 频率检查
    if _today_action_count(openid, "update_avatar") >= AVATAR_DAILY_LIMIT:
        logger.warning("update_avatar: daily limit reached, openid=%.6s...", openid)
        return None

    # 落盘（固定名覆盖写）
    avatar_dir = config.DATA_DIR / "avatars"
    avatar_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(openid.encode("utf-8")).hexdigest()[:16]

    # 清理同 hash 的旧文件（不同扩展名也算旧文件，否则换格式会残留两份）
    for old in avatar_dir.glob(f"{digest}.*"):
        try:
            old.unlink()
        except OSError as exc:
            logger.warning("update_avatar: cannot remove old %s: %s", old, exc)

    filename = f"{digest}{ext}"
    avatar_path = avatar_dir / filename
    try:
        avatar_path.write_bytes(data)
    except OSError as exc:
        logger.exception("update_avatar: write failed: %s", exc)
        return None

    updated_at = datetime.now()
    # URL 含 ?v= 时间戳，强制绕开 <image> 缓存
    version = updated_at.strftime("%Y%m%d%H%M%S")
    avatar_url = f"{config.PUBLIC_BASE_URL}/static/avatars/{filename}?v={version}"

    ok = db.execute(
        "UPDATE users SET avatar_url=%s, avatar_updated_at=%s WHERE openid=%s",
        (avatar_url, updated_at, openid),
    )
    if not ok:
        return None

    # 操作日志（一举两得：审计 + 频率计数）
    db.execute(
        "INSERT INTO operation_logs (openid, action, action_name, detail) "
        "VALUES (%s, 'update_avatar', '修改头像', %s)",
        (openid, _detail_json({"size": len(data), "ext": ext})),
    )

    logger.info(
        "update_avatar ok: openid=%.6s... size=%s ext=%s", openid, len(data), ext
    )
    return {"avatar_url": avatar_url, "updated_at": updated_at.isoformat()}


def update_nickname(openid: str, nickname: str) -> Optional[Dict[str, Any]]:
    """更新昵称。

    Returns:
        成功返回 ``{nickname, nickname_custom: true, updated_at}``；
        失败（未登录/超限/长度不符/DB 失败）返回 ``None``。
    """
    if not openid:
        return None
    nickname = (nickname or "").strip()
    if not (NICKNAME_MIN_LEN <= len(nickname) <= NICKNAME_MAX_LEN):
        logger.warning("update_nickname: bad length %s", len(nickname))
        return None

    if _today_action_count(openid, "update_nickname") >= NICKNAME_DAILY_LIMIT:
        logger.warning("update_nickname: daily limit reached, openid=%.6s...", openid)
        return None

    ok = db.execute(
        "UPDATE users SET nickname=%s, nickname_updated_at=NOW(3) WHERE openid=%s",
        (nickname, openid),
    )
    if not ok:
        return None

    db.execute(
        "INSERT INTO operation_logs (openid, action, action_name, detail) "
        "VALUES (%s, 'update_nickname', '修改昵称', %s)",
        (openid, _detail_json({"length": len(nickname)})),
    )

    row = db.fetchone(
        "SELECT nickname_updated_at FROM users WHERE openid=%s", (openid,)
    )
    updated_at = row.get("nickname_updated_at") if row else None

    logger.info("update_nickname ok: openid=%.6s... len=%s", openid, len(nickname))
    return {
        "nickname": nickname,
        "nickname_custom": True,
        "updated_at": updated_at.isoformat() if updated_at else "",
    }


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _today_action_count(openid: str, action: str) -> int:
    """今日此动作的次数（用 operation_logs 而非另开计数器）。"""
    row = db.fetchone(
        "SELECT COUNT(*) AS n FROM operation_logs "
        "WHERE openid=%s AND action=%s AND created_at >= CURDATE()",
        (openid, action),
    )
    if not row:
        return 0
    try:
        return int(row["n"])
    except (KeyError, TypeError, ValueError):
        return 0


def _detail_json(payload: Dict[str, Any]) -> str:
    """手搓 JSON 字符串，避免引入 json 模块依赖。"""
    parts = []
    for k, v in payload.items():
        if isinstance(v, str):
            v_str = '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
        elif isinstance(v, bool):
            v_str = "true" if v else "false"
        elif isinstance(v, (int, float)):
            v_str = str(v)
        else:
            v_str = '"' + str(v) + '"'
        parts.append('"' + k + '":' + v_str)
    return "{" + ",".join(parts) + "}"