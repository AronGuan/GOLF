"""MySQL 连接池与轻量查询封装。

对应方案：``docs/plans/2026-09-05-wechat-login-and-audit.md``
表结构：``deploy/mysql/001_init.sql``（任务）+ ``002_auth.sql``（登录 / 操作记录）

设计铁律（方案验收标准 8）：

    **数据库不可用绝不影响分析主链路。**

    本模块所有公开函数都**不抛异常** —— 连接失败、查询报错一律记日志后返回
    空结果 / 0 / False。调用方不需要写 ``try/except``。

    三重保障：

    1. ``config.DB_ENABLED`` 为 False（未配密码）时直接短路，零开销
    2. 连接池创建失败后退避 :data:`_CREATE_BACKOFF_SEC` 秒 —— 若无退避，
       DB 宕机后每个请求都会尝试建连并卡满 ``DB_CONNECT_TIMEOUT`` 秒，
       反而把主链路拖垮（这是本模块最重要的一条）
    3. 每次取连接都 ping（云数据库会主动断开长空闲连接）
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

from . import config

logger = logging.getLogger(__name__)

__all__ = [
    "is_available",
    "ping",
    "execute",
    "executemany",
    "insert",
    "fetchone",
    "fetchall",
    "reset_pool",
]

# ---------------------------------------------------------------------------
# 连接池（懒加载）
# ---------------------------------------------------------------------------

#: 连接池创建失败后的退避时长（秒）。见模块文档「三重保障」第 2 条。
_CREATE_BACKOFF_SEC: float = 30.0

_pool: Any = None
_pool_lock = threading.Lock()

#: 退避截止时刻（monotonic）。在此之前不再尝试建池。
_next_create_ts: float = 0.0


def _get_pool() -> Any:
    """取连接池；不可用或未到重试时刻返回 ``None``（已记日志）。

    Returns:
        已就绪的 ``PooledDB`` 实例，或 ``None``。
    """
    global _pool, _next_create_ts

    if _pool is not None:
        return _pool
    if not config.DB_ENABLED:
        return None
    if time.monotonic() < _next_create_ts:
        return None

    with _pool_lock:
        if _pool is not None:
            return _pool
        if time.monotonic() < _next_create_ts:
            return None

        try:
            import pymysql
            from dbutils.pooled_db import PooledDB
        except ImportError as exc:
            # 依赖缺失是永久性的，退避同样能避免刷屏
            logger.error("db driver missing: %s (need: pip install pymysql dbutils)", exc)
            _next_create_ts = time.monotonic() + _CREATE_BACKOFF_SEC
            return None

        try:
            _pool = PooledDB(
                creator=pymysql,
                maxconnections=config.DB_MAX_CONNECTIONS,
                host=config.DB_HOST,
                port=config.DB_PORT,
                user=config.DB_USER,
                password=config.DB_PASSWORD,
                database=config.DB_NAME,
                charset=config.DB_CHARSET,
                cursorclass=pymysql.cursors.DictCursor,
                connect_timeout=config.DB_CONNECT_TIMEOUT,
                read_timeout=config.DB_READ_TIMEOUT,
                write_timeout=config.DB_WRITE_TIMEOUT,
                autocommit=True,
                # ping=1 —— 每次取出连接时 ping，等价 SQLAlchemy 的 pool_pre_ping。
                # 云数据库会主动断开长空闲连接，不 ping 会拿到已死连接。
                ping=1,
            )
            logger.info(
                "db pool ready: %s@%s:%s/%s (max=%d)",
                config.DB_USER, config.DB_HOST, config.DB_PORT,
                config.DB_NAME, config.DB_MAX_CONNECTIONS,
            )
        except Exception as exc:
            logger.warning(
                "db pool init failed, retry after %.0fs: %s",
                _CREATE_BACKOFF_SEC, exc,
            )
            _next_create_ts = time.monotonic() + _CREATE_BACKOFF_SEC
            _pool = None

    return _pool


# ---------------------------------------------------------------------------
# 查询执行
# ---------------------------------------------------------------------------

def _run(sql: str, args: Any, *, many: bool, fetch: str) -> Any:
    """内部执行器：取连接 → 执行 → 归还连接。任何异常都吞掉。

    Args:
        sql: 带 ``%s`` 占位符的 SQL。
        args: 参数序列；``many=True`` 时为参数序列的序列。
        many: 是否用 ``executemany``。
        fetch: ``"none"`` 写操作（返回 rowcount）|
               ``"one"`` 单行（返回 dict）|
               ``"all"`` 多行（返回 dict 列表）。

    Returns:
        按 ``fetch`` 返回对应类型；失败时返回该类型的空值
        （``0`` / ``None`` / ``[]``）。
    """
    pool = _get_pool()
    if pool is None:
        return _EMPTY[fetch]

    conn = None
    try:
        conn = pool.connection()
        with conn.cursor() as cur:
            if many:
                cur.executemany(sql, args)
            else:
                cur.execute(sql, args)

            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return cur.rowcount
    except Exception as exc:
        logger.warning("db query failed: %s | sql=%s", exc, _safe_sql(sql))
        return _EMPTY[fetch]
    finally:
        if conn is not None:
            try:
                conn.close()  # 归还连接池，不是真关闭
            except Exception:
                pass


#: 各 fetch 模式失败时的空值。失败必须返回**同类型**的空值，
#: 这样调用方用 ``if not rows`` / ``if uid is None`` 判断即可，不需要 try。
_EMPTY: Dict[str, Any] = {"none": 0, "one": None, "all": []}


def _safe_sql(sql: str) -> str:
    """日志用的 SQL 摘要（截断 + 压平换行），避免长 SQL 刷屏。"""
    flat = " ".join(sql.split())
    return flat if len(flat) <= 200 else flat[:200] + "..."


def is_available() -> bool:
    """数据库是否可用（已配置且连接池已就绪）。

    只做本地判断，**不发起真实 I/O** —— 适合在热路径上做前置检查。
    要真实探测连通性用 :func:`ping`。

    Returns:
        可用返回 True。
    """
    return _get_pool() is not None


def ping() -> bool:
    """真实连通性探测（健康检查 / 启动自检 / 部署验证用）。

    Returns:
        ``SELECT 1`` 成功返回 True；DB 未配置或不可达返回 False。
    """
    row = fetchone("SELECT 1 AS ok")
    return row is not None


def execute(sql: str, args: Optional[Sequence] = None) -> int:
    """执行单条写语句，返回受影响行数。

    Args:
        sql: 带 ``%s`` 占位符的 SQL。
        args: 参数序列。

    Returns:
        受影响行数；失败返回 0。
    """
    result = _run(sql, args, many=False, fetch="none")
    return result if isinstance(result, int) else 0


def executemany(sql: str, seq: Sequence[Sequence]) -> int:
    """批量执行写语句（如一次写入约 190 行阶段指标）。

    Args:
        sql: 带 ``%s`` 占位符的 SQL。
        seq: 参数序列的序列。

    Returns:
        受影响行数；失败返回 0。
    """
    if not seq:
        return 0
    result = _run(sql, seq, many=True, fetch="none")
    return result if isinstance(result, int) else 0


def insert(sql: str, args: Optional[Sequence] = None) -> Optional[int]:
    """执行 INSERT 并返回自增主键。

    Args:
        sql: 带 ``%s`` 占位符的 INSERT 语句。
        args: 参数序列。

    Returns:
        新行的自增 id（如 ``users.id``，默认昵称要用）；失败返回 ``None``。
    """
    pool = _get_pool()
    if pool is None:
        return None

    conn = None
    try:
        conn = pool.connection()
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return int(cur.lastrowid)
    except Exception as exc:
        logger.warning("db insert failed: %s | sql=%s", exc, _safe_sql(sql))
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def fetchone(sql: str, args: Optional[Sequence] = None) -> Optional[Dict[str, Any]]:
    """查询单行。

    Args:
        sql: 带 ``%s`` 占位符的 SQL。
        args: 参数序列。

    Returns:
        字段名到值的 dict；无结果或失败返回 ``None``。
    """
    row = _run(sql, args, many=False, fetch="one")
    return row if isinstance(row, dict) else None


def fetchall(sql: str, args: Optional[Sequence] = None) -> List[Dict[str, Any]]:
    """查询多行。

    Args:
        sql: 带 ``%s`` 占位符的 SQL。
        args: 参数序列。

    Returns:
        dict 列表；无结果或失败返回 ``[]``。
    """
    rows = _run(sql, args, many=False, fetch="all")
    return rows if isinstance(rows, list) else []


def reset_pool() -> None:
    """关闭并清空连接池（**仅测试使用**）。

    下次调用会重新建池。生产环境不需要调用 —— 池内连接由 ``ping=1`` 自动保活。
    """
    global _pool, _next_create_ts
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.close()
            except Exception:
                pass
        _pool = None
        _next_create_ts = 0.0
