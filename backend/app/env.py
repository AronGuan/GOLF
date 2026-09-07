"""项目根 ``.env`` 加载器（零第三方依赖）。

设计要点
--------
1. **零依赖** —— 只用 stdlib。项目对依赖敏感（embeddable Python 无 lock file，
   外部清理工具会删包），不为一个「读 KEY=VALUE」引入 python-dotenv。

2. **真环境变量优先** —— 已存在于 ``os.environ`` 的键**不被 .env 覆盖**。
   这样生产环境用 systemd ``Environment=`` 或 shell export 设置的值优先级更高，
   `.env` 只是本地开发的兜底。

3. **不剥离行尾注释** —— 密码里出现 ``#`` 很常见，任何形式的行尾注释剥离
   都有误伤风险。只把**整行以 ``#`` 开头**视为注释。

4. **失败静默** —— 文件缺失、权限不足、编码错误一律只记日志不抛异常。
   `.env` 是开发便利设施，缺失时退化为「纯环境变量」，不能让服务起不来。

用法
----
在 ``config.py`` 顶部 ``from . import env`` 即可（import 时自动加载一次）。
通常无需显式调用 :func:`load`。

自检::

    python -m app.env        # 打印已加载的键（值脱敏）
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: 项目根目录（``backend/app/env.py`` → app → backend → 项目根）
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent

#: ``.env`` 文件名
ENV_FILE_NAME: str = ".env"

# 引号字符集：用于剥落成对的包裹引号
_QUOTES = ("'", '"')

# 已加载标记，避免重复解析
_loaded: bool = False

#: :func:`load` 时因「环境变量已存在」而被跳过的键。
#: 注意：不能用 ``key in os.environ`` 判断——load() 成功后 .env 的键
#: 全都会进入 os.environ，那样会得出「全部都被覆盖」的错误结论。
_skipped: set = set()


def find_env_file() -> Optional[Path]:
    """定位 ``.env`` 文件。按优先级依次尝试：

    1. 项目根（``backend/app/env.py`` 往上三级）—— 与 cwd 无关，最稳
    2. 当前工作目录 —— 从 ``backend/`` 启动时为 ``backend/.env``
    3. 当前工作目录的父目录 —— 从 ``backend/app/`` 等子目录启动时兜底

    Returns:
        找到则返回绝对路径，否则 ``None``。
    """
    cwd = Path.cwd()
    candidates = (
        PROJECT_ROOT / ENV_FILE_NAME,
        cwd / ENV_FILE_NAME,
        cwd.parent / ENV_FILE_NAME,
    )
    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _parse_line(line: str) -> Optional[Tuple[str, str]]:
    """解析单行。返回 ``(key, value)``，非键值对 / 注释返回 ``None``。

    支持 ``KEY=VALUE``、``export KEY=VALUE``、成对引号包裹的值。
    **不做**行尾注释剥离与转义序列解释，值按字面量原样保留。
    """
    text = line.strip()
    if not text or text.startswith("#"):
        return None

    if text.startswith("export "):
        text = text[len("export ") :].lstrip()

    if "=" not in text:
        return None

    key, _, raw = text.partition("=")
    key = key.strip()
    if not key:
        return None

    value = raw.strip()
    # 成对引号包裹 → 去引号，内部字符（空格 / # / !）全部保留
    if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTES:
        value = value[1:-1]
    return key, value


def load(override: bool = False) -> Dict[str, str]:
    """把 ``.env`` 中的键值写入 ``os.environ``。

    Args:
        override: True 时用 .env 覆盖已存在的环境变量（默认 False）。

    Returns:
        本次**实际写入**的键值字典（已存在而被跳过的键不含在内）。
    """
    global _loaded
    if _loaded and not override:
        return {}
    _loaded = True

    path = find_env_file()
    if path is None:
        logger.debug("env: no .env found (looked in %s, cwd, cwd/..)", PROJECT_ROOT)
        return {}

    applied: Dict[str, str] = {}
    _skipped.clear()
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("env: cannot read %s: %s", path, exc)
        return {}

    for lineno, line in enumerate(raw.splitlines(), start=1):
        try:
            parsed = _parse_line(line)
        except Exception as exc:  # 单行解析异常不影响其余行
            logger.warning("env: %s:%d parse failed: %s", path.name, lineno, exc)
            continue
        if parsed is None:
            continue
        key, value = parsed
        if not override and key in os.environ:
            _skipped.add(key)  # 真环境变量优先，.env 的值不生效
            continue
        os.environ[key] = value
        applied[key] = value

    if applied:
        logger.info(
            "env: loaded %d key(s) from %s: %s",
            len(applied),
            path,
            ", ".join(sorted(applied)),
        )
    return applied


def mask(value: str, keep: int = 3) -> str:
    """脱敏：保留前 ``keep`` 位，其余用 ``*`` 替代。空值原样返回。"""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)


#: 含这些子串的键，自检输出时自动脱敏（大小写不敏感）
SENSITIVE_HINTS: Tuple[str, ...] = (
    "secret",
    "password",
    "passwd",
    "token",
    "key",
    "pwd",
)


def is_sensitive(key: str) -> bool:
    """判断键是否属于敏感项（用于日志/自检脱敏）。"""
    lowered = key.lower()
    return any(hint in lowered for hint in SENSITIVE_HINTS)


def is_overridden(key: str) -> bool:
    """该键是否被**真实环境变量**抢先占用（.env 中的值未生效）。

    仅在上一次 :func:`load` 之后有效；用于自检输出提示。
    """
    return key in _skipped


def loaded_keys() -> List[Tuple[str, str]]:
    """返回 ``.env`` 中出现的全部键及其（可能脱敏的）值，供自检使用。"""
    path = find_env_file()
    if path is None:
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return []
    out: List[Tuple[str, str]] = []
    for line in raw.splitlines():
        parsed = _parse_line(line)
        if parsed is None:
            continue
        key, value = parsed
        out.append((key, mask(value) if is_sensitive(key) else value))
    return out


# ---------------------------------------------------------------------------
# import 即生效：config.py 顶部 ``from . import env`` 后，
# 后续所有 os.getenv() 都能读到 .env 的值。
# ---------------------------------------------------------------------------
load()


if __name__ == "__main__":
    # 自检入口：python -m app.env
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    path = find_env_file()
    print(f"项目根    : {PROJECT_ROOT}")
    print(f".env 路径 : {path if path else '（未找到）'}")
    print(f"cwd       : {Path.cwd()}")
    print("-" * 60)
    keys = loaded_keys()
    if not keys:
        print("（.env 为空或不存在）")
    else:
        width = max(len(k) for k, _ in keys)
        for key, shown in keys:
            note = "  [被环境变量覆盖，未生效]" if is_overridden(key) else ""
            print(f"{key:<{width}} = {shown}{note}")
