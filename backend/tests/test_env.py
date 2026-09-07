"""``app.env``（.env 加载器）单元测试。

覆盖三类风险：
1. **解析正确性** —— 引号、``!`` / ``#``、``export`` 前缀、行内 ``=`` 等边界
2. **优先级** —— 真环境变量 > .env（这条错了会导致生产配置被本地文件覆盖）
3. **健壮性** —— 文件缺失 / 内容异常时静默降级，绝不抛异常

测试全部用临时文件，不触碰项目根真实的 ``.env``。
"""

from __future__ import annotations

import os

import pytest

from app import env

#: 模块导入时保存原始实现。``_isolated`` fixture 会把 ``env.find_env_file``
#: 替换成桩（返回 None），定位相关的用例必须走这里拿真函数，
#: 否则测的是桩而不是被测代码（曾因此出现 2 个假失败 + 1 个假通过）。
_REAL_FIND = env.find_env_file


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """每个用例前重置模块状态，并把 .env 查找指向「不存在」。

    避免测试读到项目根真实 .env，也避免用例间互相污染 os.environ。
    """
    env._loaded = False
    env._skipped.clear()
    monkeypatch.setattr(env, "find_env_file", lambda: None)
    yield
    env._loaded = False
    env._skipped.clear()


def _write_env(tmp_path, text: str):
    """在临时目录写 .env 并让 find_env_file 指向它。"""
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def _use(monkeypatch, path):
    monkeypatch.setattr(env, "find_env_file", lambda: path)


# ---------------------------------------------------------------------------
# 一、解析正确性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line, expected",
    [
        # 基本
        ("KEY=value", ("KEY", "value")),
        ("  KEY  =  value  ", ("KEY", "value")),  # 前后空白剥离
        ("export KEY=value", ("KEY", "value")),  # export 前缀
        ("export   KEY=value", ("KEY", "value")),  # 多个空格
        # 引号：剥落成对的，内部字符原样保留
        ('KEY="v a l"', ("KEY", "v a l")),
        ("KEY='v a l'", ("KEY", "v a l")),
        ('KEY="P@ss!w0rd#2026"', ("KEY", "P@ss!w0rd#2026")),  # 密码里的 !（示意值，非真实凭据）
        ('KEY="a#b"', ("KEY", "a#b")),  # 引号内的 # 不是注释
        ('KEY=""', ("KEY", "")),  # 空引号
        # 值内含 = 只分第一个
        ("KEY=a=b=c", ("KEY", "a=b=c")),
        ("URL=http://x.com/?a=1&b=2", ("URL", "http://x.com/?a=1&b=2")),
        # 未包裹时 # 也是值的一部分（不做行尾注释剥离）
        ("KEY=a#b", ("KEY", "a#b")),
        # 非配对引号不剥离
        ('KEY="unclosed', ("KEY", '"unclosed')),
        # 应跳过的行
        ("", None),
        ("   ", None),
        ("# comment", None),
        ("   # indented comment", None),
        ("no-equal-sign", None),
        ("=value", None),  # 缺键名
    ],
)
def test_parse_line(line, expected):
    assert env._parse_line(line) == expected


def test_load_applies_to_environ(tmp_path, monkeypatch):
    """load() 把键值写入 os.environ。"""
    path = _write_env(tmp_path, "GOLF_TEST_A=1\nGOLF_TEST_B=two\n")
    monkeypatch.delenv("GOLF_TEST_A", raising=False)
    monkeypatch.delenv("GOLF_TEST_B", raising=False)
    _use(monkeypatch, path)

    applied = env.load()

    assert applied == {"GOLF_TEST_A": "1", "GOLF_TEST_B": "two"}
    assert os.environ["GOLF_TEST_A"] == "1"
    assert os.environ["GOLF_TEST_B"] == "two"


def test_load_quoted_password_keeps_special_chars(tmp_path, monkeypatch):
    """带引号的密码：! 和 # 必须原样保留（真实 DB 密码含 !）。"""
    path = _write_env(tmp_path, 'GOLF_PWD_TEST="P@ss!w#rd"\n')
    monkeypatch.delenv("GOLF_PWD_TEST", raising=False)
    _use(monkeypatch, path)

    env.load()

    assert os.environ["GOLF_PWD_TEST"] == "P@ss!w#rd"


# ---------------------------------------------------------------------------
# 二、优先级：真环境变量 > .env
# ---------------------------------------------------------------------------


def test_real_environ_wins(tmp_path, monkeypatch):
    """已存在的环境变量不被 .env 覆盖（生产用 systemd Environment= 优先）。"""
    path = _write_env(tmp_path, "GOLF_PRIO_TEST=from_dotenv\n")
    monkeypatch.setenv("GOLF_PRIO_TEST", "from_real_env")
    _use(monkeypatch, path)

    applied = env.load()

    assert applied == {}  # 没有键被写入
    assert os.environ["GOLF_PRIO_TEST"] == "from_real_env"
    assert env.is_overridden("GOLF_PRIO_TEST") is True


def test_override_true_forces_dotenv(tmp_path, monkeypatch):
    """override=True 时用 .env 覆盖环境变量。"""
    path = _write_env(tmp_path, "GOLF_PRIO_TEST=from_dotenv\n")
    monkeypatch.setenv("GOLF_PRIO_TEST", "from_real_env")
    _use(monkeypatch, path)

    applied = env.load(override=True)

    assert applied == {"GOLF_PRIO_TEST": "from_dotenv"}
    assert os.environ["GOLF_PRIO_TEST"] == "from_dotenv"
    assert env.is_overridden("GOLF_PRIO_TEST") is False


def test_is_overridden_false_when_applied(tmp_path, monkeypatch):
    """未被覆盖的键，is_overridden 必须为 False。

    曾出过 bug：用 ``key in os.environ`` 判断，而 load() 成功后 .env 的键
    全都进了 os.environ，导致「全部都被覆盖」的误判。
    """
    path = _write_env(tmp_path, "GOLF_OVR_TEST=from_dotenv\n")
    monkeypatch.delenv("GOLF_OVR_TEST", raising=False)
    _use(monkeypatch, path)

    env.load()

    assert "GOLF_OVR_TEST" in os.environ  # 确实被写入了
    assert env.is_overridden("GOLF_OVR_TEST") is False  # 但不是被覆盖


# ---------------------------------------------------------------------------
# 三、健壮性
# ---------------------------------------------------------------------------


def test_load_missing_file_returns_empty(monkeypatch):
    """.env 不存在时静默返回空，不抛异常。"""
    monkeypatch.setattr(env, "find_env_file", lambda: None)
    assert env.load() == {}


def test_load_unreadable_file_returns_empty(tmp_path, monkeypatch):
    """文件存在但读不了（权限/编码）时静默降级。"""
    path = tmp_path / ".env"
    path.write_bytes(b"\xff\xfe\x00bad-encoding")
    _use(monkeypatch, path)

    assert env.load() == {}  # UnicodeDecodeError 被吞掉


def test_load_is_idempotent(tmp_path, monkeypatch):
    """重复 load() 不重复解析（除非 override）。"""
    path = _write_env(tmp_path, "GOLF_IDEM_TEST=1\n")
    monkeypatch.delenv("GOLF_IDEM_TEST", raising=False)
    _use(monkeypatch, path)

    assert env.load() == {"GOLF_IDEM_TEST": "1"}
    assert env.load() == {}  # 第二次跳过


def test_load_bad_line_does_not_break_others(tmp_path, monkeypatch):
    """单行异常不影响其余行（当前实现下解析不会抛，但保证行为稳定）。"""
    path = _write_env(tmp_path, "BAD_LINE\nGOOD_TEST=ok\n")
    monkeypatch.delenv("GOOD_TEST", raising=False)
    _use(monkeypatch, path)

    applied = env.load()

    assert applied == {"GOOD_TEST": "ok"}


# ---------------------------------------------------------------------------
# 四、脱敏与敏感项识别
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, keep, expected",
    [
        ("", 3, ""),
        ("ab", 3, "**"),  # 短于 keep → 全星号
        ("abc", 3, "***"),  # 等于 keep → 全星号
        ("abcdef", 3, "abc***"),
        # 32 位十六进制（形如微信 AppSecret，示意值，非真实凭据）
        ("d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6", 3, "d1e" + "*" * 29),
    ],
)
def test_mask(value, keep, expected):
    assert env.mask(value, keep=keep) == expected


@pytest.mark.parametrize(
    "key, expected",
    [
        ("GOLF_WX_SECRET", True),
        ("GOLF_DB_PASSWORD", True),
        ("MY_TOKEN", True),
        ("API_KEY", True),
        ("GOLF_DB_HOST", False),
        ("GOLF_WX_APPID", False),
        ("GOLF_DB_PORT", False),
    ],
)
def test_is_sensitive(key, expected):
    assert env.is_sensitive(key) is expected


def test_loaded_keys_masks_sensitive(tmp_path, monkeypatch):
    """loaded_keys() 对敏感键自动脱敏，非敏感键原样返回。"""
    path = _write_env(tmp_path, "GOLF_SECRET_X=abcdefgh\nGOLF_HOST_X=1.2.3.4\n")
    _use(monkeypatch, path)

    keys = dict(env.loaded_keys())

    assert keys["GOLF_SECRET_X"] == "abc*****"
    assert keys["GOLF_HOST_X"] == "1.2.3.4"


def test_loaded_keys_missing_file(monkeypatch):
    monkeypatch.setattr(env, "find_env_file", lambda: None)
    assert env.loaded_keys() == []


# ---------------------------------------------------------------------------
# 五、文件定位
# ---------------------------------------------------------------------------


def test_find_env_file_prefers_project_root(tmp_path, monkeypatch):
    """优先项目根，与 cwd 无关（uvicorn 可能从任意目录启动）。"""
    root_env = env.PROJECT_ROOT / ".env"
    monkeypatch.chdir(tmp_path)  # cwd 下没有 .env

    found = _REAL_FIND()

    if root_env.is_file():
        assert found == root_env
    else:
        assert found is None


def test_find_env_file_falls_back_to_cwd(tmp_path, monkeypatch):
    """项目根没有时，退回 cwd。"""
    monkeypatch.setattr(env, "PROJECT_ROOT", tmp_path / "nonexistent-root")
    (tmp_path / ".env").write_text("X=1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert _REAL_FIND() == tmp_path / ".env"


def test_find_env_file_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(env, "PROJECT_ROOT", tmp_path / "nonexistent-root")
    monkeypatch.chdir(tmp_path)  # 空目录，任何层级都没有 .env

    assert _REAL_FIND() is None
