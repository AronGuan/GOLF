"""微信登录与登录态管理（方案 M1）。

设计要点（方案：``docs/plans/2026-09-05-wechat-login-and-audit.md``）

安全约定
--------
1. **``session_key`` 只存库，绝不下发客户端** —— 它可解密微信敏感数据。
2. **token 落库存 SHA-256 哈希** —— 库被拖走也无法冒用。
3. **不用 JWT** —— 无法主动失效，而 MVP 需要「禁用用户立即生效」；
   服务端 token + ``revoked`` 标记更简单可控。
4. **不用 ``session_key`` 当登录态** —— 很多教程的错误做法，见上第 1 条。

时间处理
--------
过期判断一律用 MySQL ``NOW(3)`` 而非 Python ``datetime.now()``：应用服务器与
数据库服务器的时区/时钟可能不一致，混用会导致 token 提前或延后失效。
因此写入用 ``DATE_ADD(NOW(3), INTERVAL n DAY)``，查询用 ``expires_at > NOW(3)``。

降级语义
--------
所有失败（未配置 AppSecret / 微信超时 / code 无效 / 数据库不可用）都返回
``None`` 或空值并记日志，**不抛异常**。登录是增强项，绝不能拖垮分析主链路
（方案验收标准 8）。调用方只需判断返回值真假。
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Mapping, Optional, Tuple

from . import config, db

logger = logging.getLogger(__name__)

#: 微信 ``jscode2session`` 常见错误码 -> 中文（用于日志，不下发客户端）
_WX_ERR_TEXT: Dict[int, str] = {
    -1: "微信系统繁忙",
    40029: "code 无效",
    40226: "code 被封禁（高风险用户）",
    41008: "缺少 code",
    45011: "API 调用太频繁（超过频率限制）",
    40013: "AppID 无效",
    40125: "AppSecret 无效",
}

#: 默认昵称的编号宽度（``球手 0007``）
_NICK_WIDTH: int = config.DEFAULT_NICKNAME_ID_WIDTH


# ---------------------------------------------------------------------------
# 一、微信接口
# ---------------------------------------------------------------------------


def _build_code2session_url(code: str) -> str:
    """拼 ``jscode2session`` 请求 URL。"""
    query = urllib.parse.urlencode(
        {
            "appid": config.WX_APPID,
            "secret": config.WX_SECRET,
            "js_code": code,
            "grant_type": "authorization_code",
        }
    )
    return f"{config.WX_CODE2SESSION_URL}?{query}"


def wx_login(code: str) -> Optional[Tuple[str, str]]:
    """用 ``wx.login`` 的临时 code 换 openid 与 session_key。

    Args:
        code: 小程序端 ``wx.login()`` 拿到的临时凭证（5 分钟有效，一次性）。

    Returns:
        成功返回 ``(openid, session_key)``；任何失败返回 ``None``。

    注意:
        code **只能用一次**，重复使用微信会报 40163。前端必须做登录单例
        Promise，避免 ``onLaunch`` 与页面 ``onLoad`` 并发触发。
    """
    if not config.WX_LOGIN_ENABLED:
        logger.warning("wx_login: 未配置 AppSecret，登录功能已禁用")
        return None

    code = (code or "").strip()
    if not code:
        logger.warning("wx_login: code 为空")
        return None

    url = _build_code2session_url(code)
    # ⚠️ 敏感：URL 含 AppSecret，任何日志都不要打印它
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=config.WX_HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        logger.warning("wx_login: HTTP %s", exc.code)
        return None
    except Exception as exc:  # 超时 / DNS / 连接重置
        logger.warning("wx_login: 请求微信失败: %s", exc)
        return None

    try:
        payload: Dict[str, Any] = json.loads(raw)
    except Exception as exc:
        logger.warning("wx_login: 响应不是合法 JSON: %s", exc)
        return None

    errcode = int(payload.get("errcode", 0) or 0)
    if errcode != 0:
        # ⚠️ 敏感：errmsg 不含 AppSecret，可安全记日志
        logger.warning(
            "wx_login: errcode=%s (%s)",
            errcode,
            _WX_ERR_TEXT.get(errcode, payload.get("errmsg", "")),
        )
        return None

    openid = str(payload.get("openid") or "").strip()
    session_key = str(payload.get("session_key") or "").strip()
    if not openid:
        logger.warning("wx_login: 响应缺少 openid")
        return None
    return openid, session_key


# ---------------------------------------------------------------------------
# 二、用户表读写
# ---------------------------------------------------------------------------


def _upsert_user(openid: str, ip: Optional[str]) -> Optional[int]:
    """新用户插入、老用户累加登录次数。返回 ``users.id``，失败返回 ``None``。

    ``login_count`` 用 SQL 自增而非「读出来 +1 再写回」，避免并发下的
    丢失更新（read-modify-write 竞态）。
    """
    db.execute(
        "INSERT INTO users (openid, login_count, last_login_at, last_login_ip) "
        "VALUES (%s, 1, NOW(3), %s) "
        "ON DUPLICATE KEY UPDATE "
        "  login_count = login_count + 1, "
        "  last_login_at = NOW(3), "
        "  last_login_ip = VALUES(last_login_ip)",
        (openid, ip),
    )
    row = db.fetchone("SELECT id FROM users WHERE openid=%s", (openid,))
    if not row:
        logger.warning("upsert_user: 写入后仍查不到用户，openid=%.6s...", openid)
        return None
    return int(row["id"])


def find_user(openid: str) -> Optional[Dict[str, Any]]:
    """按 openid 查用户，未找到或 DB 不可用返回 ``None``。"""
    return db.fetchone(
        "SELECT id, openid, nickname, avatar_url, nickname_updated_at, "
        "       login_count, created_at "
        "FROM users WHERE openid=%s",
        (openid,),
    )


# ---------------------------------------------------------------------------
# 三、登录态（token）
# ---------------------------------------------------------------------------


def _hash_token(token: str) -> str:
    """token 的 SHA-256 十六进制摘要（64 字符）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token(
    openid: str,
    session_key: str = "",
    *,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    """签发登录态 token 并落库。

    Args:
        openid: 用户标识。
        session_key: 微信会话密钥，只入库不下发。
        ip: 客户端 IP（可能经反代，取到的未必是真实 IP，仅作审计线索）。
        user_agent: 客户端 UA。

    Returns:
        成功返回 ``(明文 token, expires_at ISO 字符串)``；失败返回 ``None``。
        明文 token **只在此刻返回一次**，之后无处可查（库里只有哈希）。
    """
    token = secrets.token_urlsafe(config.TOKEN_BYTES)
    token_hash = _hash_token(token)

    ok = db.execute(
        "INSERT INTO user_tokens "
        "  (token_hash, openid, session_key, expires_at, created_ip, user_agent) "
        "VALUES (%s, %s, %s, DATE_ADD(NOW(3), INTERVAL %s DAY), %s, %s)",
        (
            token_hash,
            openid,
            session_key or None,
            config.TOKEN_TTL_DAYS,
            (ip or None),
            (user_agent or "")[:512],
        ),
    )
    if not ok:
        logger.warning("issue_token: 写 user_tokens 失败，openid=%.6s...", openid)
        return None

    # 回读 expires_at —— 它由 MySQL NOW(3) 算出，不能由 Python 猜
    row = db.fetchone(
        "SELECT expires_at FROM user_tokens WHERE token_hash=%s", (token_hash,)
    )
    if not row or row.get("expires_at") is None:
        return (token, "")  # token 已签发成功，只是读不到过期时间，不判失败
    return (token, row["expires_at"].isoformat())


def verify_token(token: str) -> Optional[str]:
    """校验 token，返回 openid。无效 / 过期 / 已撤销返回 ``None``。

    过期判断用 MySQL ``NOW(3)``（见模块 docstring「时间处理」）。
    """
    token = (token or "").strip()
    if not token:
        return None
    row = db.fetchone(
        "SELECT openid FROM user_tokens "
        "WHERE token_hash=%s AND revoked=0 AND expires_at > NOW(3)",
        (_hash_token(token),),
    )
    # 用 .get 而非硬取键：DB 返回异常行时静默降级，不能让接口 500
    # （与 app.db 层「失败返回空值」的哲学一致）
    openid = (row or {}).get("openid")
    return str(openid) if openid else None


def revoke_token(token: str) -> bool:
    """撤销 token（登出 / 禁用用户）。成功返回 True。"""
    token = (token or "").strip()
    if not token:
        return False
    return bool(
        db.execute(
            "UPDATE user_tokens SET revoked=1 WHERE token_hash=%s", (_hash_token(token),)
        )
    )


def openid_from_headers(headers: Mapping[str, str]) -> Optional[str]:
    """从请求头解析 ``Authorization: Bearer <token>`` 得到 openid。

    接受 ``headers`` 映射（FastAPI ``Request.headers`` 或普通 dict），
    不直接依赖 Request 对象，便于单测。
    """
    try:
        raw = headers.get("authorization") or headers.get("Authorization") or ""
    except Exception:
        return None
    raw = raw.strip()
    if not raw:
        return None
    # 按前 6 字符判断（而非 "bearer " 7 字符）：这样 "Bearer" 这种
    # **没有实质 token** 的头也会被判为空，不会拿 "Bearer" 当 token 去查库。
    if raw[:6].lower() == "bearer":
        raw = raw[6:].strip()
    return verify_token(raw) if raw else None


# ---------------------------------------------------------------------------
# 四、默认值兜底（方案 §3.6）
# ---------------------------------------------------------------------------


def default_nickname(user_id: Optional[int]) -> str:
    """默认昵称：``球手`` + 4 位编号 → ``球手 0007``。

    编号用 ``users.id``（自增），保证唯一。
    """
    if user_id is None:
        return config.DEFAULT_NICKNAME_PREFIX
    return f"{config.DEFAULT_NICKNAME_PREFIX} {user_id:0{_NICK_WIDTH}d}"


def avatar_hue(openid: str) -> int:
    """默认头像色块的色相（0~359），由 ``sha256(openid)[:4] % 360`` 得出。

    确定性：同一用户每次算出的颜色一致，不需要存库。
    """
    digest = hashlib.sha256((openid or "").encode("utf-8")).hexdigest()
    return int(digest[:4], 16) % 360


def mask_openid(openid: str) -> str:
    """脱敏 openid：保留首尾各 4 位 → ``oAbC...wXyZ``。"""
    if not openid:
        return ""
    if len(openid) <= 8:
        return "*" * len(openid)
    return f"{openid[:4]}...{openid[-4:]}"


def public_user(openid: str) -> Optional[Dict[str, Any]]:
    """组装对外用户信息（脱敏 + 默认值兜底）。

    Returns:
        含 ``openid_masked`` / ``nickname`` / ``nickname_custom`` /
        ``avatar_url`` / ``avatar_hue`` / ``login_count`` / ``created_at``；
        用户不存在或 DB 不可用返回 ``None``。
    """
    row = find_user(openid)
    if not row:
        return None

    nickname = str(row.get("nickname") or "")
    custom = row.get("nickname_updated_at") is not None
    created_at = row.get("created_at")

    return {
        "openid_masked": mask_openid(str(row.get("openid") or "")),
        # 库中为空 -> 用默认值兜底，但**不回写**（空值本身是有效信息）
        "nickname": nickname if nickname else default_nickname(row.get("id")),
        "nickname_custom": bool(custom),
        "avatar_url": str(row.get("avatar_url") or ""),
        "avatar_hue": avatar_hue(str(row.get("openid") or "")),
        "login_count": int(row.get("login_count") or 0),
        "created_at": created_at.isoformat() if created_at is not None else "",
    }


# ---------------------------------------------------------------------------
# 五、登录编排（供 main.py 调用）
# ---------------------------------------------------------------------------


def login(
    code: str,
    *,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """完整登录流程：code → openid → 建/更新用户 → 签发 token。

    Returns:
        成功返回 ``{"token", "expires_at", "is_new_user", "user"}``；
        任何失败返回 ``None``（调用方降级为匿名，绝不阻断）。
    """
    if not config.WX_LOGIN_ENABLED:
        logger.warning("login: 登录未启用（缺少 AppSecret）")
        return None

    pair = wx_login(code)
    if pair is None:
        return None
    openid, session_key = pair

    is_new_user = find_user(openid) is None

    user_id = _upsert_user(openid, ip)
    if user_id is None:
        return None

    issued = issue_token(openid, session_key, ip=ip, user_agent=user_agent)
    if issued is None:
        return None
    token, expires_at = issued

    user = public_user(openid) or {
        "openid_masked": mask_openid(openid),
        "nickname": default_nickname(user_id),
        "nickname_custom": False,
        "avatar_url": "",
        "avatar_hue": avatar_hue(openid),
        "login_count": 1,
        "created_at": "",
    }

    logger.info(
        "login ok: openid=%.6s... uid=%s is_new=%s", openid, user_id, is_new_user
    )
    return {
        "token": token,
        "expires_at": expires_at,
        "is_new_user": is_new_user,
        "user": user,
    }
