"""app.db 单元测试 —— 核心验收「MySQL 不可用不影响分析主链路」。

⚠️ 全部用例**不连真实数据库**：用假连接池替换 ``db._pool``，只验证

* 降级语义 —— 未配置 / 建池失败 / 查询报错，一律返回空值且**不抛异常**
* 正常路径 —— 返回类型正确（dict / list / rowcount / lastrowid）
* 退避机制 —— 建池失败后一段时间内不重试，避免每个请求都卡在连接超时上

方案验收标准 8：MySQL 宕机时分析功能仍可用（登录 / 记录静默失败）。
"""

from __future__ import annotations

import time

import pytest

from app import config, db


# ---------------------------------------------------------------------------
# 假对象
# ---------------------------------------------------------------------------


class _FakeCursor:
    """可控游标：按构造参数决定返回什么或抛什么。"""

    def __init__(self, rows=None, rowcount=0, lastrowid=0, raise_exc=None):
        self._rows = list(rows or [])
        self._rowcount = rowcount
        self._lastrowid = lastrowid
        self._raise = raise_exc
        self._buf: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, args=None):
        if self._raise:
            raise self._raise
        self._buf = list(self._rows)
        return self._rowcount

    def executemany(self, sql, seq):
        if self._raise:
            raise self._raise
        # 真实驱动会在 executemany 后把 rowcount 更新为影响行数
        self._rowcount = len(seq)
        return self._rowcount

    def fetchone(self):
        return self._buf[0] if self._buf else None

    def fetchall(self):
        return list(self._buf)

    @property
    def rowcount(self):
        return self._rowcount

    @property
    def lastrowid(self):
        return self._lastrowid


class _FakeConn:
    def __init__(self, **kw):
        self._kw = kw
        self.closed = False

    def cursor(self):
        return _FakeCursor(**self._kw)

    def close(self):
        self.closed = True


class _FakePool:
    def __init__(self, **kw):
        self._kw = kw
        self.conns: list = []

    def connection(self):
        conn = _FakeConn(**self._kw)
        self.conns.append(conn)
        return conn


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """每个用例都在「DB 未配置」的干净状态下开始，用完重置连接池。"""
    monkeypatch.setattr(config, "DB_ENABLED", False)
    db.reset_pool()
    yield
    db.reset_pool()


def _install_pool(monkeypatch, **kw) -> _FakePool:
    """把假连接池装进 db，并打开 DB_ENABLED。"""
    monkeypatch.setattr(config, "DB_ENABLED", True)
    pool = _FakePool(**kw)
    monkeypatch.setattr(db, "_pool", pool)
    return pool


# ---------------------------------------------------------------------------
# 1. 未配置数据库：静默降级，零异常
# ---------------------------------------------------------------------------


def test_disabled_returns_empty_without_raising():
    assert config.DB_ENABLED is False
    assert db.is_available() is False
    assert db.ping() is False

    # 所有查询类操作返回空值，绝不抛异常
    assert db.fetchone("SELECT 1") is None
    assert db.fetchall("SELECT 1") == []
    assert db.execute("DELETE FROM users WHERE 1=0") == 0
    assert db.executemany("INSERT INTO users (openid) VALUES (%s)", [("a",)]) == 0
    assert db.insert("INSERT INTO users (openid) VALUES (%s)", ("a",)) is None


def test_disabled_executemany_empty_seq_short_circuits():
    """空参数序列直接短路，不建连接。"""
    assert db.executemany("INSERT INTO users (openid) VALUES (%s)", []) == 0


# ---------------------------------------------------------------------------
# 2. 建池失败：静默降级 + 退避
# ---------------------------------------------------------------------------


def test_pool_create_failure_is_silent(monkeypatch):
    monkeypatch.setattr(config, "DB_ENABLED", True)
    calls: list = []

    def boom(**kwargs):
        calls.append(1)
        raise RuntimeError("connection refused")

    monkeypatch.setattr("dbutils.pooled_db.PooledDB", boom)

    assert db.is_available() is False
    assert db.ping() is False
    assert db.fetchall("SELECT 1") == []


def test_pool_create_failure_backs_off(monkeypatch):
    """建池失败后退避期内不再重试 —— 否则 DB 宕机会把每个请求都拖慢。"""
    monkeypatch.setattr(config, "DB_ENABLED", True)
    calls: list = []

    def boom(**kwargs):
        calls.append(1)
        raise RuntimeError("connection refused")

    monkeypatch.setattr("dbutils.pooled_db.PooledDB", boom)

    db.ping()
    assert len(calls) == 1

    # 退避期内：不再尝试建池
    db.ping()
    db.fetchall("SELECT 1")
    db.execute("DELETE FROM users")
    assert len(calls) == 1, "退避期内不应重复建池"

    # 退避到期后：允许重新尝试一次
    monkeypatch.setattr(db, "_next_create_ts", time.monotonic() - 1)
    db.ping()
    assert len(calls) == 2


def test_missing_driver_is_silent(monkeypatch):
    """pymysql / dbutils 未安装时同样静默降级（部署漏装不会 500）。"""
    import builtins

    monkeypatch.setattr(config, "DB_ENABLED", True)
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name in ("pymysql", "dbutils.pooled_db"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert db.is_available() is False
    assert db.fetchone("SELECT 1") is None


# ---------------------------------------------------------------------------
# 3. 查询报错：静默降级（这是「DB 宕机不影响主链路」的最后一道）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "func,args,expected",
    [
        (db.fetchone, ("SELECT 1",), None),
        (db.fetchall, ("SELECT 1",), []),
        (db.execute, ("DELETE FROM users",), 0),
        (db.insert, ("INSERT INTO users (openid) VALUES (%s)",), None),
    ],
)
def test_query_error_never_raises(monkeypatch, func, args, expected):
    _install_pool(monkeypatch, raise_exc=RuntimeError("MySQL server has gone away"))

    result = func(*args)
    assert result == expected or result is expected


def test_query_error_returns_same_type_empty(monkeypatch):
    """失败必须返回**同类型**空值 —— 调用方用 falsy / is None 判断即可，
    不需要 try/except，也不会拿到 0 冒充空列表这种类型错乱。"""
    _install_pool(monkeypatch, raise_exc=RuntimeError("deadlock found"))
    assert db.fetchall("SELECT 1") == []
    assert db.fetchone("SELECT 1") is None
    assert db.execute("DELETE FROM users") == 0
    assert db.insert("INSERT INTO users (openid) VALUES (%s)", ("a",)) is None


def test_connection_is_returned_to_pool_on_error(monkeypatch):
    """异常时连接必须归还，否则池会被耗尽。"""
    pool = _install_pool(monkeypatch, raise_exc=RuntimeError("boom"))
    db.fetchall("SELECT 1")
    assert pool.conns, "应取出过连接"
    assert all(c.closed for c in pool.conns), "异常分支也必须 close() 归还连接"


# ---------------------------------------------------------------------------
# 4. 正常路径：返回类型正确
# ---------------------------------------------------------------------------


def test_fetchone_returns_dict(monkeypatch):
    _install_pool(monkeypatch, rows=[{"id": 7, "nickname": ""}])
    row = db.fetchone("SELECT id, nickname FROM users WHERE openid=%s", ("o1",))
    assert row == {"id": 7, "nickname": ""}


def test_fetchall_returns_list_of_dict(monkeypatch):
    _install_pool(monkeypatch, rows=[{"id": 1}, {"id": 2}])
    rows = db.fetchall("SELECT id FROM users")
    assert rows == [{"id": 1}, {"id": 2}]


def test_fetchone_no_row_returns_none(monkeypatch):
    _install_pool(monkeypatch, rows=[])
    assert db.fetchone("SELECT id FROM users WHERE 1=0") is None


def test_execute_returns_rowcount(monkeypatch):
    _install_pool(monkeypatch, rowcount=3)
    assert db.execute("DELETE FROM operation_logs WHERE created_at < %s", ("2026-01-01",)) == 3


def test_insert_returns_lastrowid(monkeypatch):
    """默认昵称「球手 NNNN」依赖自增 id，必须能拿到。"""
    _install_pool(monkeypatch, lastrowid=42)
    assert db.insert("INSERT INTO users (openid) VALUES (%s)", ("o1",)) == 42


def test_executemany_returns_rowcount(monkeypatch):
    _install_pool(monkeypatch)
    seq = [("t1", "tempo_ratio", 2.8), ("t1", "swing_duration", 1.15)]
    assert db.executemany("INSERT INTO task_metrics (task_id, metric_key, value) VALUES (%s,%s,%s)", seq) == 2


def test_connection_is_returned_to_pool_on_success(monkeypatch):
    pool = _install_pool(monkeypatch, rows=[{"ok": 1}])
    db.fetchone("SELECT 1 AS ok")
    assert all(c.closed for c in pool.conns), "正常分支也要 close() 归还连接"


# ---------------------------------------------------------------------------
# 5. 可用性判断语义
# ---------------------------------------------------------------------------


def test_is_available_does_no_io(monkeypatch):
    """is_available() 只做本地判断，不应触发查询。"""
    pool = _install_pool(monkeypatch, rows=[{"ok": 1}])
    assert db.is_available() is True
    assert pool.conns == [], "is_available() 不应发起真实 I/O"


def test_ping_performs_io(monkeypatch):
    pool = _install_pool(monkeypatch, rows=[{"ok": 1}])
    assert db.ping() is True
    assert pool.conns, "ping() 应发起真实查询"
