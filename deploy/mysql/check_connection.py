#!/usr/bin/env python
"""golf 库连通性与表结构自检。

用途：
    1. 首次部署后确认后端能连上 MySQL
    2. 执行 DDL 后确认表结构符合预期
    3. 线上排查「数据库是不是挂了」

密码**只从环境变量读取**，不落盘、不入库、不进 git::

    export GOLF_DB_PASSWORD='...'      # 或写进项目根 .env（推荐）
    python deploy/mysql/check_connection.py            # 只读检查
    python deploy/mysql/check_connection.py --smoke     # 额外做一轮写/读/清理

凭据优先级：系统环境变量 > 项目根 ``.env``。两者都没有时直接报 FAIL 退出，
不尝试空密码连接。退出码:
    0 = 全部通过    1 = 连接失败    2 = 表结构不符预期    3 = 冒烟测试失败
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

# 期望的表 -> 列数（与 001_init.sql / 002_auth.sql 保持一致）
EXPECTED_TABLES = {
    "tasks": 31,
    "task_phases": 12,
    "task_metrics": 17,
    "task_risks": 17,
    "users": 18,
    "user_tokens": 10,
    "operation_logs": 12,
    "schema_migrations": 4,
}
EXPECTED_MIGRATIONS = {"001_init", "002_auth"}

SMOKE_OPENID = "_smoke_check_"

ok = True


def _mark(level: str, msg: str) -> None:
    global ok
    print(f"[{level}] {msg}")
    if level == "FAIL":
        ok = False


def main() -> int:
    do_smoke = "--smoke" in sys.argv

    # 导入 config 即触发 app.env 加载项目根 .env，之后统一以 config 为准。
    # ⚠️ 不要绕过 config 直接读 os.environ —— 那样 .env 里的凭据会失效。
    from app import config, db  # noqa: E402

    # 1. 凭据
    if not config.DB_ENABLED:
        _mark(
            "FAIL",
            "未配置数据库密码 —— 在项目根 .env 写 GOLF_DB_PASSWORD=...，"
            "或 export GOLF_DB_PASSWORD=...",
        )
        return 1

    print(f"目标: {config.DB_USER}@{config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME}")

    # 2. 连通性
    if not db.ping():
        _mark("FAIL", "连接失败 —— 检查网络 / 账号 / 白名单 / 密码")
        return 1
    _mark("OK", "连接成功")

    # 3. 表结构
    # ⚠️ information_schema 返回的列名是大写（TABLE_NAME），必须起别名统一成小写，
    #    否则 r["table_name"] 会 KeyError。这条只有连真库才暴露，mock 测试覆盖不到。
    rows = db.fetchall(
        "SELECT table_name AS tbl, COUNT(*) AS n FROM information_schema.columns "
        "WHERE table_schema=%s GROUP BY table_name ORDER BY table_name",
        (config.DB_NAME,),
    )
    actual = {r["tbl"]: int(r["n"]) for r in rows}
    missing = set(EXPECTED_TABLES) - set(actual)
    if missing:
        _mark("FAIL", f"缺少表: {sorted(missing)}")
        return 2

    for name, ncol in sorted(EXPECTED_TABLES.items()):
        got = actual.get(name)
        if got != ncol:
            _mark("FAIL", f"{name}: 列数 {got} != 期望 {ncol}")
        else:
            print(f"       {name:<20} {ncol} 列")

    # 4. 迁移版本
    versions = {
        r["version"]
        for r in db.fetchall("SELECT version FROM schema_migrations")
    }
    if not EXPECTED_MIGRATIONS <= versions:
        _mark("FAIL", f"迁移版本缺失: {sorted(EXPECTED_MIGRATIONS - versions)}")
        return 2
    _mark("OK", f"迁移版本: {sorted(versions)}")

    # 5. 冒烟（可选）
    if do_smoke:
        if not _smoke():
            return 3

    print("\n" + ("=" * 46))
    print("全部检查通过" if ok else "存在失败项，见上方 [FAIL]")
    return 0 if ok else 2


def _smoke() -> bool:
    """写 → 读 → 清理。验证 JSON 列、中文、自增 id、索引都正常。"""
    from app import db  # noqa: E402

    print("\n--- 冒烟测试 (写 → 读 → 清理) ---")
    try:
        db.execute("DELETE FROM operation_logs WHERE openid=%s", (SMOKE_OPENID,))
        db.execute("DELETE FROM users WHERE openid=%s", (SMOKE_OPENID,))

        uid = db.insert(
            "INSERT INTO users (openid, nickname, avatar_url) VALUES (%s, '', '')",
            (SMOKE_OPENID,),
        )
        if uid is None:
            _mark("FAIL", "INSERT users 未返回自增 id")
            return False
        print(f"       users.id = {uid}（默认昵称将显示为『球手 {uid:04d}』）")

        n = db.execute(
            "INSERT INTO operation_logs (openid, task_id, action, action_name, detail) "
            "VALUES (%s, NULL, 'login', '登录', %s)",
            (SMOKE_OPENID, '{"is_new_user": true}'),
        )
        if n != 1:
            _mark("FAIL", f"INSERT operation_logs 影响行数 {n} != 1")
            return False

        # ⚠️ JSON_EXTRACT 返回的是 JSON 类型（pymysql 转成字符串 'true'/带引号），
        #    必须 CAST 成 UNSIGNED 才能当整数用。同样只有连真库才暴露。
        row = db.fetchone(
            "SELECT u.id, u.nickname, u.avatar_url, "
            "  CAST(JSON_EXTRACT(l.detail, '$.is_new_user') AS UNSIGNED) AS is_new, "
            "  l.action_name "
            "FROM users u JOIN operation_logs l ON l.openid = u.openid "
            "WHERE u.openid = %s",
            (SMOKE_OPENID,),
        )
        if not row:
            _mark("FAIL", "JOIN 查询无结果")
            return False

        # 默认值语义：库中应为空串，由接口层兜底
        if row["nickname"] != "" or row["avatar_url"] != "":
            _mark("FAIL", f"默认值污染了库：nickname={row['nickname']!r}")
            return False
        if int(row["is_new"]) != 1:
            _mark("FAIL", f"JSON 列读取异常: {row['is_new']!r}")
            return False

        _mark("OK", f"JOIN 查询正常，中文 action_name={row['action_name']}")
        return True
    finally:
        # 无论成功失败都清理，避免留下垃圾数据
        db.execute("DELETE FROM operation_logs WHERE openid=%s", (SMOKE_OPENID,))
        db.execute("DELETE FROM users WHERE openid=%s", (SMOKE_OPENID,))
        print("       清理完成")


if __name__ == "__main__":
    sys.exit(main())
