"""``app.auth``（微信登录）单元测试。

重点覆盖三类：
1. **安全属性** —— session_key 不外泄、token 落库只存哈希、openid 脱敏
2. **降级语义** —— 任何失败返回 None/空值，绝不抛异常（验收标准 8）
3. **默认值兜底** —— 库中空串时接口补齐，且**不回写**库

测试全部用假 DB 与假 HTTP，不触碰真实数据库与微信接口。
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from datetime import datetime

import pytest

from app import auth, config


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class FakeResp:
    """假 HTTP 响应（支持 with 语法）。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


class FakeDB:
    """假数据库：记录所有 SQL，按队列返回查询结果。"""

    def __init__(self) -> None:
        self.calls: list = []  # [(sql, args)]
        self.user_rows: list = []  # find_user 的返回队列
        self.token_expiry = datetime(2026, 10, 5, 12, 0, 0)
        self.token_openid: str | None = None  # 由 INSERT user_tokens 回填
        self.execute_result = 1

    def execute(self, sql, args=None):
        self.calls.append((sql, args))
        # 保真：记住写进去的 openid，verify 时才查得出来
        if "INSERT INTO user_tokens" in sql and args:
            self.token_openid = args[1]
        return self.execute_result

    def fetchone(self, sql, args=None):
        self.calls.append((sql, args))
        if "FROM users" in sql:
            if self.user_rows:
                return self.user_rows.pop(0)
            return {
                "id": 7,
                "openid": "oTEST12345678",
                "nickname": "",
                "avatar_url": "",
                "nickname_updated_at": None,
                "login_count": 3,
                "created_at": datetime(2026, 9, 5, 10, 0, 0),
            }
        if "FROM user_tokens" in sql:
            # 保真：真实 SQL 查的就是 openid（假对象缺字段会掩盖真实行为）
            return {"openid": self.token_openid, "expires_at": self.token_expiry}
        return None

    def fetchall(self, sql, args=None):
        self.calls.append((sql, args))
        return []

    def sql_contains(self, needle: str) -> bool:
        return any(needle in sql for sql, _ in self.calls)


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(auth, "db", db)
    return db


@pytest.fixture
def wx_enabled(monkeypatch):
    monkeypatch.setattr(config, "WX_LOGIN_ENABLED", True)
    monkeypatch.setattr(config, "WX_APPID", "wxaTEST")
    monkeypatch.setattr(config, "WX_SECRET", "secretTEST")


def _mock_wx(monkeypatch, payload: dict | bytes | Exception):
    """把微信接口替换成指定响应（dict 自动转 JSON 字节）或异常。"""

    def _urlopen(req, timeout=None):
        if isinstance(payload, Exception):
            raise payload
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return FakeResp(raw)

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)


# ---------------------------------------------------------------------------
# 一、wx_login
# ---------------------------------------------------------------------------


def test_wx_login_success(monkeypatch, wx_enabled):
    _mock_wx(monkeypatch, {"openid": "oABC123", "session_key": "sk_xxx"})
    assert auth.wx_login("code123") == ("oABC123", "sk_xxx")


def test_wx_login_errcode_returns_none(monkeypatch, wx_enabled):
    _mock_wx(monkeypatch, {"errcode": 40163, "errmsg": "code been used"})
    assert auth.wx_login("code123") is None


def test_wx_login_http_error_returns_none(monkeypatch, wx_enabled):
    _mock_wx(monkeypatch, urllib.error.HTTPError("u", 502, "Bad Gateway", {}, None))
    assert auth.wx_login("code123") is None


def test_wx_login_timeout_returns_none(monkeypatch, wx_enabled):
    _mock_wx(monkeypatch, TimeoutError("timed out"))
    assert auth.wx_login("code123") is None


def test_wx_login_invalid_json_returns_none(monkeypatch, wx_enabled):
    _mock_wx(monkeypatch, b"<html>502 Bad Gateway</html>")
    assert auth.wx_login("code123") is None


def test_wx_login_missing_openid_returns_none(monkeypatch, wx_enabled):
    _mock_wx(monkeypatch, {"session_key": "sk"})
    assert auth.wx_login("code123") is None


@pytest.mark.parametrize("code", ["", "   ", None])
def test_wx_login_empty_code(monkeypatch, wx_enabled, code):
    _mock_wx(monkeypatch, {"openid": "oABC"})
    assert auth.wx_login(code) is None


def test_wx_login_disabled_short_circuit(monkeypatch):
    """未配置 AppSecret 时不发请求（避免无意义的外部调用）。"""
    monkeypatch.setattr(config, "WX_LOGIN_ENABLED", False)
    called = []
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: called.append(1) or FakeResp(b"{}")
    )
    assert auth.wx_login("code123") is None
    assert called == []


def test_wx_login_url_never_logged(monkeypatch, wx_enabled):
    """URL 含 AppSecret，任何日志都不能出现它。"""
    captured = []

    def _urlopen(req, timeout=None):
        captured.append(req.full_url)
        return FakeResp(json.dumps({"openid": "oABC", "session_key": "sk"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    auth.wx_login("code123")
    assert "secretTEST" in captured[0]  # 确实带上了
    assert "secretTEST" not in str(config.LOG_FORMAT)  # 但不在日志格式里


# ---------------------------------------------------------------------------
# 二、token 签发与校验
# ---------------------------------------------------------------------------


def test_issue_token_returns_plaintext_and_expiry(fake_db):
    token, expires_at = auth.issue_token("oABC", "sk_xxx", ip="1.2.3.4")
    assert token and len(token) > 30
    assert expires_at == fake_db.token_expiry.isoformat()


def test_token_stored_as_sha256_not_plaintext(fake_db):
    """🔴 安全属性：落库的必须是哈希，绝不能是明文 token。"""
    token, _ = auth.issue_token("oABC", "sk_xxx")
    insert_sql, insert_args = next(
        (s, a) for s, a in fake_db.calls if "INSERT INTO user_tokens" in s
    )
    stored = insert_args[0]
    assert stored != token  # 明文没有直接入参
    assert stored == hashlib.sha256(token.encode()).hexdigest()
    assert len(stored) == 64


def test_issue_token_uses_sql_for_expiry(fake_db):
    """过期时间由 MySQL NOW(3) 算，不用 Python 时间（避免时区/时钟不一致）。"""
    fake_db.execute("x")
    auth.issue_token("oABC", "sk")
    sql = next(s for s, _ in fake_db.calls if "INSERT INTO user_tokens" in s)
    assert "DATE_ADD(NOW(3)" in sql


def test_issue_token_db_failure_returns_none(fake_db):
    fake_db.execute_result = 0
    assert auth.issue_token("oABC", "sk") is None


def test_issue_token_expiry_read_failure_still_returns_token(fake_db):
    """token 签发成功但读不到过期时间 -> 仍返回 token，不判失败。"""
    fake_db.token_expiry = None
    token, expires_at = auth.issue_token("oABC", "sk")
    assert token and expires_at == ""


def test_verify_token_ok(fake_db):
    token, _ = auth.issue_token("oABC", "sk")
    assert auth.verify_token(token) == "oABC"


def test_verify_token_rejects_unknown(fake_db):
    fake_db.user_rows = []
    fake_db.fetchone = lambda sql, args=None: None
    assert auth.verify_token("bogus") is None


@pytest.mark.parametrize("token", ["", "   ", None])
def test_verify_token_empty(fake_db, token):
    assert auth.verify_token(token) is None


def test_verify_token_checks_revoked_and_expiry_in_sql(fake_db):
    """过期判断必须走 SQL（NOW(3)），不能由 Python 比较。"""
    auth.verify_token("tok")
    sql = next(s for s, _ in fake_db.calls if "FROM user_tokens" in s)
    assert "revoked=0" in sql
    assert "expires_at > NOW(3)" in sql


def test_revoke_token(fake_db):
    auth.revoke_token("tok")
    sql, args = fake_db.calls[-1]
    assert "UPDATE user_tokens SET revoked=1" in sql
    assert args[0] == hashlib.sha256(b"tok").hexdigest()


@pytest.mark.parametrize("token", ["", None])
def test_revoke_token_empty(fake_db, token):
    assert auth.revoke_token(token) is False


# ---------------------------------------------------------------------------
# 三、请求头解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {"authorization": "Bearer tok123"},
        {"Authorization": "Bearer tok123"},
        {"authorization": "tok123"},  # 无 Bearer 前缀也接受
        {"authorization": "  Bearer   tok123  "},
    ],
)
def test_openid_from_headers_variants(fake_db, monkeypatch, headers):
    monkeypatch.setattr(auth, "verify_token", lambda t: "oABC" if t == "tok123" else None)
    assert auth.openid_from_headers(headers) == "oABC"


@pytest.mark.parametrize(
    "headers",
    [{}, {"authorization": ""}, {"authorization": "Bearer "}, {"other": "x"}],
)
def test_openid_from_headers_missing(fake_db, headers):
    assert auth.openid_from_headers(headers) is None


def test_openid_from_headers_bad_mapping(fake_db):
    """headers 取值抛异常时不崩（防御性）。"""

    class Boom:
        def get(self, _k):
            raise RuntimeError("boom")

    assert auth.openid_from_headers(Boom()) is None


# ---------------------------------------------------------------------------
# 四、默认值兜底（方案 §3.6）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "uid, expected",
    [(None, "球手"), (0, "球手 0000"), (7, "球手 0007"), (12345, "球手 12345")],
)
def test_default_nickname(uid, expected):
    assert auth.default_nickname(uid) == expected


def test_avatar_hue_is_deterministic():
    """同一 openid 每次算出同一颜色（不入库也要稳定）。"""
    assert auth.avatar_hue("oABC") == auth.avatar_hue("oABC")


def test_avatar_hue_differs_and_in_range():
    hues = {auth.avatar_hue(f"openid-{i}") for i in range(50)}
    assert all(0 <= h <= 359 for h in hues)
    assert len(hues) > 20  # 分布足够散


def test_avatar_hue_handles_empty():
    assert 0 <= auth.avatar_hue("") <= 359


@pytest.mark.parametrize(
    "openid, expected",
    [
        ("", ""),
        ("short", "*****"),  # ≤8 位全掩码
        ("oABC1234wxyz", "oABC...wxyz"),
    ],
)
def test_mask_openid(openid, expected):
    assert auth.mask_openid(openid) == expected


def test_public_user_fills_defaults_but_keeps_db_empty(fake_db):
    """库中 nickname/avatar_url 为空 -> 接口补默认值，但**不回写**库。"""
    user = auth.public_user("oTEST12345678")
    assert user["nickname"] == "球手 0007"
    assert user["nickname_custom"] is False
    assert user["avatar_url"] == ""
    assert 0 <= user["avatar_hue"] <= 359
    assert user["openid_masked"] == "oTES...5678"
    # 关键：只查询，没有任何 UPDATE / INSERT
    assert not any(
        sql.strip().upper().startswith(("UPDATE", "INSERT"))
        for sql, _ in fake_db.calls
    )


def test_public_user_keeps_custom_nickname(fake_db):
    fake_db.user_rows = [
        {
            "id": 9,
            "openid": "oTEST12345678",
            "nickname": "老虎伍兹",
            "avatar_url": "/static/a.png",
            "nickname_updated_at": datetime(2026, 9, 5, 10, 0, 0),
            "login_count": 10,
            "created_at": datetime(2026, 9, 1, 10, 0, 0),
        }
    ]
    user = auth.public_user("oTEST12345678")
    assert user["nickname"] == "老虎伍兹"
    assert user["nickname_custom"] is True
    assert user["avatar_url"] == "/static/a.png"


def test_public_user_not_found(fake_db):
    fake_db.user_rows = []
    fake_db.fetchone = lambda sql, args=None: None
    assert auth.public_user("nobody") is None


# ---------------------------------------------------------------------------
# 五、登录编排
# ---------------------------------------------------------------------------


def test_login_success(monkeypatch, wx_enabled, fake_db):
    _mock_wx(monkeypatch, {"openid": "oTEST12345678", "session_key": "sk_secret"})

    result = auth.login("code123", ip="1.2.3.4", user_agent="mini")

    assert result is not None
    assert result["token"]
    assert result["expires_at"] == fake_db.token_expiry.isoformat()
    assert result["user"]["nickname"] == "球手 0007"
    assert result["is_new_user"] is False


def test_login_marks_new_user(monkeypatch, wx_enabled, fake_db):
    """首次登录：find_user 第一次返回 None -> is_new_user=True。"""
    _mock_wx(monkeypatch, {"openid": "oTEST12345678", "session_key": "sk"})
    fake_db.user_rows = [None]  # 第一次 find_user 未找到

    result = auth.login("code123")
    assert result["is_new_user"] is True


def test_login_never_leaks_session_key(monkeypatch, wx_enabled, fake_db):
    """🔴 安全属性：返回值任何位置都不能出现 session_key。"""
    _mock_wx(monkeypatch, {"openid": "oTEST12345678", "session_key": "SUPER_SECRET_SK"})

    result = auth.login("code123")

    blob = json.dumps(result, ensure_ascii=False, default=str)
    assert "SUPER_SECRET_SK" not in blob
    # 但必须确实写进了库（用于将来解密，只是不下发）
    insert_args = next(a for s, a in fake_db.calls if "INSERT INTO user_tokens" in s)
    assert "SUPER_SECRET_SK" in insert_args


def test_login_wx_failure_returns_none(monkeypatch, wx_enabled, fake_db):
    _mock_wx(monkeypatch, {"errcode": 40029, "errmsg": "invalid code"})
    assert auth.login("bad-code") is None


def test_login_disabled_returns_none(monkeypatch, fake_db):
    monkeypatch.setattr(config, "WX_LOGIN_ENABLED", False)
    assert auth.login("code123") is None


def test_login_user_write_failure_returns_none(monkeypatch, wx_enabled, fake_db):
    _mock_wx(monkeypatch, {"openid": "oTEST12345678", "session_key": "sk"})
    fake_db.user_rows = []
    fake_db.fetchone = lambda sql, args=None: None  # upsert 后查不到 id
    assert auth.login("code123") is None


def test_login_token_write_failure_returns_none(monkeypatch, wx_enabled, fake_db):
    _mock_wx(monkeypatch, {"openid": "oTEST12345678", "session_key": "sk"})

    orig_execute = fake_db.execute

    def _execute(sql, args=None):
        if "INSERT INTO user_tokens" in sql:
            return 0  # token 写入失败
        return orig_execute(sql, args)

    fake_db.execute = _execute
    assert auth.login("code123") is None


def test_login_increments_count_via_sql(monkeypatch, wx_enabled, fake_db):
    """login_count 用 SQL 自增，不做 read-modify-write（避免并发丢失更新）。"""
    _mock_wx(monkeypatch, {"openid": "oTEST12345678", "session_key": "sk"})
    auth.login("code123")
    sql = next(s for s, _ in fake_db.calls if "INSERT INTO users" in s)
    assert "login_count = login_count + 1" in sql
    assert "ON DUPLICATE KEY UPDATE" in sql
