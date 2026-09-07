"""``app.user``（M2.5 头像昵称）单元测试。

重点覆盖四类：
1. **格式识别** —— 按 magic bytes，不信扩展名
2. **路径固定** —— 同 openid 始终落同一文件，覆盖写不堆积
3. **频率限制** —— 用 operation_logs 计数，今日上限
4. **失败路径** —— 任何违规/超限都返回 None，绝不写入
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app import audit, user


# ---------------------------------------------------------------------------
# 一、格式识别（纯函数）
# ---------------------------------------------------------------------------


# 最小合法 PNG（8 字节 magic + IHDR 头 25 字节）
PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n"
    + b"\x00\x00\x00\rIHDR"
    + b"\x00\x00\x00\x01\x00\x00\x00\x01"
    + b"\x08\x02\x00\x00\x00\x90wS\xde"
)

# 最小合法 JPEG
JPEG_1x1 = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


@pytest.mark.parametrize(
    "data, expected",
    [
        (PNG_1x1, ".png"),
        (JPEG_1x1, ".jpg"),
        (b"", None),
        (b"\x00\x00\x00", None),
        (b"\x89PNG", None),  # magic 截断
        (b"GIF89a...", None),  # 非 PNG/JPEG
        (b"\xff\xd8\xff", ".jpg"),  # 最小 magic
    ],
)
def test_detect_avatar_format(data, expected):
    assert user.detect_avatar_format(data) == expected


def test_avatar_path_for_is_deterministic():
    """同 openid 始终返回同一文件名 → 覆盖写不堆积。"""
    a = user.avatar_path_for("oABC", ".png")
    b = user.avatar_path_for("oABC", ".png")
    assert a == b
    assert a.endswith(".png")
    # 不依赖扩展名：同 hash + 不同 ext 也同名（除扩展名）
    a2 = user.avatar_path_for("oABC", ".jpg")
    assert a2.endswith(".jpg") and not a2.endswith(".png")


def test_avatar_path_for_differs_per_openid():
    a = user.avatar_path_for("oABC", ".png")
    b = user.avatar_path_for("oDEF", ".png")
    assert a != b


def test_avatar_path_for_no_path_traversal():
    """文件名不含特殊字符（防止 openid 被注入路径）"""
    # 即便 openid 含 ../ 也只用作 SHA 输入，输出仍是纯 hex + ext
    name = user.avatar_path_for("../../../etc/passwd", ".png")
    assert "/" not in name.replace(".png", "")  # 去掉 ext 后没有 /


# ---------------------------------------------------------------------------
# 二、_today_action_count 内部
# ---------------------------------------------------------------------------


class FakeDB:
    """假 DB：记录所有 SQL，按 SQL 关键字返回不同结果。"""

    def __init__(self, count_result: int = 0) -> None:
        self.calls = []
        self.count_result = count_result
        self.execute_result = 1
        self.user_rows: list = []

    def execute(self, sql, args=None):
        self.calls.append((sql, args))
        return self.execute_result

    def fetchone(self, sql, args=None):
        self.calls.append((sql, args))
        if "COUNT(*) AS n FROM operation_logs" in sql:
            return {"n": self.count_result}
        if "FROM users WHERE openid=%s" in sql:
            return (
                self.user_rows.pop(0) if self.user_rows else {"nickname_updated_at": None}
            )
        return None


@pytest.fixture
def fake_db(monkeypatch):
    """替换 DB 层为假实现。

    ⚠️ 必须同时 patch ``user.db`` 和 ``audit.db``：
    操作记录已收敛到 ``app.audit``（2026-09-07），若只替换 user.db，
    审计写入会走**真的** db 模块（测试环境静默降级），fake_db.calls 里
    就看不到 INSERT INTO operation_logs，断言会以假阴性失败。
    """
    db = FakeDB()
    monkeypatch.setattr(user, "db", db)
    monkeypatch.setattr(audit, "db", db)
    return db


def test_today_count_returns_int(fake_db):
    fake_db.count_result = 3
    assert user._today_action_count("oABC", "update_avatar") == 3


def test_today_count_returns_zero_on_empty(fake_db):
    fake_db.count_result = 0
    assert user._today_action_count("oABC", "update_avatar") == 0


def test_today_count_handles_missing_n_field(fake_db):
    """DB 行格式异常时不崩，返回 0（与 db 模块「失败返回空值」哲学一致）。"""

    def fake_fetchone(sql, args=None):
        return {"wrong_key": 0}

    fake_db.fetchone = fake_fetchone
    assert user._today_action_count("oABC", "update_avatar") == 0


def test_today_count_handles_non_int(fake_db):
    def fake_fetchone(sql, args=None):
        return {"n": "abc"}

    fake_db.fetchone = fake_fetchone
    assert user._today_action_count("oABC", "update_avatar") == 0


# ---------------------------------------------------------------------------
# 三、update_avatar
# ---------------------------------------------------------------------------


def test_update_avatar_success(fake_db, monkeypatch, tmp_path):
    monkeypatch.setattr(user.config, "DATA_DIR", tmp_path)
    fake_db.count_result = 0

    result = user.update_avatar("oABC123", PNG_1x1)

    assert result is not None
    # URL 以 ".png?v=" 结尾（v= 时间戳用于绕开 <image> 缓存）
    assert result["avatar_url"].endswith(".png?v=" + result["avatar_url"].rsplit("?v=", 1)[1])
    assert "?v=" in result["avatar_url"]
    assert "oABC123" not in result["avatar_url"]  # openid 不出现在 URL
    # 文件真的写到了磁盘
    files = list((tmp_path / "avatars").iterdir())
    assert len(files) == 1
    assert files[0].read_bytes() == PNG_1x1
    # UPDATE + 写日志
    assert any("UPDATE users SET avatar_url" in s for s, _ in fake_db.calls)
    # action 已参数化（2026-09-07 收敛到 app.audit，列顺序：
    # openid, action, action_name, task_id, detail, result, ...），
    # 因此 'update_avatar' 出现在 args[1] 而非 SQL 字面量
    assert any(
        "INSERT INTO operation_logs" in s and a and a[1] == "update_avatar"
        for s, a in fake_db.calls
    )


def test_update_avatar_jpeg(fake_db, monkeypatch, tmp_path):
    monkeypatch.setattr(user.config, "DATA_DIR", tmp_path)
    fake_db.count_result = 0

    result = user.update_avatar("oABC", JPEG_1x1)

    assert result is not None
    assert ".jpg?v=" in result["avatar_url"]


def test_update_avatar_overwrite_same_file(fake_db, monkeypatch, tmp_path):
    """同一 openid 第二次上传：覆盖写同一文件，不堆积。"""
    monkeypatch.setattr(user.config, "DATA_DIR", tmp_path)
    fake_db.count_result = 0

    user.update_avatar("oABC", PNG_1x1)
    user.update_avatar("oABC", JPEG_1x1)  # 改成 jpg

    files = list((tmp_path / "avatars").iterdir())
    assert len(files) == 1  # 关键：仍只有 1 个文件


def test_update_avatar_rejects_oversize(fake_db, monkeypatch, tmp_path):
    monkeypatch.setattr(user.config, "DATA_DIR", tmp_path)
    fake_db.count_result = 0
    big = PNG_1x1 + b"\x00" * (user.AVATAR_MAX_BYTES + 1)

    result = user.update_avatar("oABC", big)

    assert result is None
    assert not (tmp_path / "avatars").exists() or not list((tmp_path / "avatars").iterdir())


def test_update_avatar_rejects_invalid_format(fake_db, monkeypatch, tmp_path):
    monkeypatch.setattr(user.config, "DATA_DIR", tmp_path)
    fake_db.count_result = 0

    result = user.update_avatar("oABC", b"not an image at all")

    assert result is None


def test_update_avatar_daily_limit(fake_db, monkeypatch, tmp_path):
    monkeypatch.setattr(user.config, "DATA_DIR", tmp_path)
    fake_db.count_result = user.AVATAR_DAILY_LIMIT  # 今日已达上限

    result = user.update_avatar("oABC", PNG_1x1)

    assert result is None
    # 频率检查失败时不写文件
    assert not (tmp_path / "avatars").exists() or not list((tmp_path / "avatars").iterdir())


def test_update_avatar_empty_openid(fake_db, monkeypatch, tmp_path):
    monkeypatch.setattr(user.config, "DATA_DIR", tmp_path)
    fake_db.count_result = 0

    assert user.update_avatar("", PNG_1x1) is None


def test_update_avatar_empty_data(fake_db, monkeypatch, tmp_path):
    monkeypatch.setattr(user.config, "DATA_DIR", tmp_path)
    fake_db.count_result = 0

    assert user.update_avatar("oABC", b"") is None


def test_update_avatar_db_failure_returns_none(fake_db, monkeypatch, tmp_path):
    monkeypatch.setattr(user.config, "DATA_DIR", tmp_path)
    fake_db.count_result = 0
    fake_db.execute_result = 0  # UPDATE 失败

    result = user.update_avatar("oABC", PNG_1x1)

    assert result is None


def test_update_avatar_write_failure_returns_none(fake_db, monkeypatch, tmp_path):
    monkeypatch.setattr(user.config, "DATA_DIR", tmp_path)
    fake_db.count_result = 0

    class BoomPath:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        @property
        def parent(self):
            class P:
                def mkdir(self, **kw):
                    return None

            return P()

        def write_bytes(self, _):
            raise OSError("disk full")

    # 拦截真实的 Path.write_bytes：monkeypatch 不太适合 Path 对象，改用副作用
    monkeypatch.setattr(user, "_today_action_count", lambda *a: 0)
    # 直接覆盖 write_bytes 在类级别
    from pathlib import Path

    orig = Path.write_bytes

    def boom(self, data):
        if "avatars" in str(self):
            raise OSError("disk full")
        return orig(self, data)

    monkeypatch.setattr(Path, "write_bytes", boom)
    assert user.update_avatar("oABC", PNG_1x1) is None


def test_update_avatar_url_contains_version_timestamp(fake_db, monkeypatch, tmp_path):
    """URL 必须含 ?v= 时间戳，绕开 <image> 缓存。"""
    monkeypatch.setattr(user.config, "DATA_DIR", tmp_path)
    fake_db.count_result = 0

    result = user.update_avatar("oABC", PNG_1x1)

    assert "?v=" in result["avatar_url"]
    version = result["avatar_url"].split("?v=")[1]
    assert len(version) == 14  # YYYYMMDDHHMMSS
    assert version.isdigit()


def test_update_avatar_url_is_absolute(fake_db, monkeypatch, tmp_path):
    monkeypatch.setattr(user.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(user.config, "PUBLIC_BASE_URL", "http://example.com:8000")
    fake_db.count_result = 0

    result = user.update_avatar("oABC", PNG_1x1)

    assert result["avatar_url"].startswith("http://example.com:8000/static/avatars/")


# ---------------------------------------------------------------------------
# 四、update_nickname
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "nickname, expected_ok",
    [
        ("球手", True),
        ("老虎伍兹", True),  # 5 chars
        ("a" * 16, True),  # 上限
        ("", False),  # 空
        ("a" * 17, False),  # 超限
        ("   ", False),  # 全空白被 strip 后为空
        ("  老虎伍兹  ", True),  # strip 后 4 chars
    ],
)
def test_update_nickname_length_boundaries(fake_db, monkeypatch, nickname, expected_ok):
    fake_db.count_result = 0

    result = user.update_nickname("oABC", nickname)

    if expected_ok:
        assert result is not None
        assert result["nickname_custom"] is True
        assert "  " not in result["nickname"]  # 已 strip
    else:
        assert result is None


def test_update_nickname_daily_limit(fake_db):
    fake_db.count_result = user.NICKNAME_DAILY_LIMIT

    result = user.update_nickname("oABC", "新昵称")

    assert result is None


def test_update_nickname_empty_openid(fake_db):
    fake_db.count_result = 0
    assert user.update_nickname("", "x") is None


def test_update_nickname_writes_log(fake_db):
    fake_db.count_result = 0
    user.update_nickname("oABC", "老虎")
    insert_sql, insert_args = next((s, a) for s, a in fake_db.calls if "INSERT INTO operation_logs" in s)
    # action 已参数化（列顺序见 audit.log_operation）：
    #   (openid, action, action_name, task_id, detail, result, ...)
    assert insert_args[1] == "update_nickname"
    assert insert_args[2] == "修改昵称"
    detail = json.loads(insert_args[4])
    assert detail["length"] == 2


def test_update_nickname_db_failure_returns_none(fake_db):
    fake_db.count_result = 0
    fake_db.execute_result = 0
    assert user.update_nickname("oABC", "老虎") is None


def test_update_nickname_returns_updated_at_iso(fake_db):
    fake_db.count_result = 0
    fake_db.user_rows = [{"nickname_updated_at": None}]
    # 让 fetchone 返回真实 datetime
    from datetime import datetime

    fake_db.user_rows = [{"nickname_updated_at": datetime(2026, 9, 5, 11, 0, 0)}]

    result = user.update_nickname("oABC", "老虎")

    assert result["updated_at"].startswith("2026-09-05T11:00:00")


# ---------------------------------------------------------------------------
# 六、classify_* 失败分类
# ---------------------------------------------------------------------------


def test_classify_avatar_empty_openid(fake_db):
    assert user.classify_avatar_failure("", PNG_1x1) == user.ERR_OPENID


def test_classify_avatar_empty_data(fake_db):
    assert user.classify_avatar_failure("oABC", b"") == user.ERR_EMPTY


def test_classify_avatar_too_big(fake_db):
    big = PNG_1x1 + b"\x00" * user.AVATAR_MAX_BYTES
    assert user.classify_avatar_failure("oABC", big) == user.ERR_SIZE


def test_classify_avatar_bad_format(fake_db):
    assert user.classify_avatar_failure("oABC", b"not image") == user.ERR_FORMAT


def test_classify_avatar_daily_limit(fake_db):
    fake_db.count_result = user.AVATAR_DAILY_LIMIT
    assert user.classify_avatar_failure("oABC", PNG_1x1) == user.ERR_LIMIT


def test_classify_avatar_passes_when_ok(fake_db):
    fake_db.count_result = 0
    assert user.classify_avatar_failure("oABC", PNG_1x1) is None


def test_classify_nickname_empty_openid(fake_db):
    assert user.classify_nickname_failure("", "x") == user.ERR_OPENID


@pytest.mark.parametrize("nick", ["", "   ", "a" * 17])
def test_classify_nickname_bad_length(fake_db, nick):
    assert user.classify_nickname_failure("oABC", nick) == user.ERR_FORMAT


def test_classify_nickname_daily_limit(fake_db):
    fake_db.count_result = user.NICKNAME_DAILY_LIMIT
    assert user.classify_nickname_failure("oABC", "老虎") == user.ERR_LIMIT


def test_classify_nickname_passes_when_ok(fake_db):
    fake_db.count_result = 0
    assert user.classify_nickname_failure("oABC", "老虎") is None


def test_classify_checks_in_cheap_order(fake_db):
    """openid 空 / data 空 / size / format 都在 DB 查询之前完成（性能考量）。"""
    # openid 空：连 data 都不读，直接返回
    fake_db.count_result = 99  # 即使超限也先 openid 优先
    assert user.classify_avatar_failure("", PNG_1x1) == user.ERR_OPENID
    # data 空：也不查 DB
    assert user.classify_avatar_failure("oABC", b"") == user.ERR_EMPTY
    # data 格式错：也不查 DB
    assert user.classify_avatar_failure("oABC", b"junk") == user.ERR_FORMAT