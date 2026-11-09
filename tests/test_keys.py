"""``src/keys.py`` 单元测试：Key 四层优先级、打码、钥匙串降级。

所有用例都用替身 keyring，绝不触碰真实系统钥匙串，也不写入真实 .env。
"""

from __future__ import annotations

import stat
import os
from pathlib import Path

import pytest

from src import keys
from src.config import AppConfig, load_config


@pytest.fixture(autouse=True)
def isolated_project_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """密钥测试只用临时文件，绝不能触碰项目真实 .env。"""
    monkeypatch.setattr(keys, "env_file_path", lambda config=None: tmp_path / ".env")


class FakeKeyring:
    """最小 keyring 替身：只在内存里存值，可模拟写入失败。"""

    def __init__(self, fail_on_set: bool = False) -> None:
        self.store = {}
        self.fail_on_set = fail_on_set

    def get_keyring(self):
        """返回一个占位后端对象，只要不是 fail 后端即可。"""
        return object()

    def get_password(self, service: str, username: str):
        """读取已存密码。"""
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, value: str) -> None:
        """写入密码；``fail_on_set`` 时抛错模拟钥匙串锁定。"""
        if self.fail_on_set:
            raise RuntimeError("keychain locked")
        self.store[(service, username)] = value

    def delete_password(self, service: str, username: str) -> None:
        """删除密码；不存在时抛错，模拟 keyring 行为。"""
        if (service, username) not in self.store:
            raise KeyError("no entry")
        del self.store[(service, username)]


@pytest.fixture()
def config() -> AppConfig:
    """使用项目真实配置（只读取，不写盘）。"""
    return load_config()


@pytest.fixture()
def empty_keyring() -> FakeKeyring:
    """空的钥匙串替身。"""
    return FakeKeyring()


def _seed_keyring(fake: FakeKeyring, config: AppConfig, value: str) -> None:
    """把 Key 预置进替身钥匙串。"""
    fake.store[(config.runtime.keyring_service, keys.KEYRING_USERNAME)] = value


def _write_env(path: Path, value: str) -> Path:
    """写一个只含 AMAP_KEY 的 .env。"""
    path.write_text(f"AMAP_KEY={value}\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 四层优先级
# --------------------------------------------------------------------------- #
def test_cli_key_has_highest_priority(
    tmp_path: Path, config: AppConfig, empty_keyring: FakeKeyring
) -> None:
    """--key 优先于环境变量、钥匙串与 .env。"""
    _seed_keyring(empty_keyring, config, "keyring_key")
    env_file = _write_env(tmp_path / ".env", "dotenv_key")

    result = keys.resolve_key(
        cli_key="cli_key",
        config=config,
        env={"AMAP_KEY": "env_key"},
        env_path=env_file,
        keyring_module=empty_keyring,
    )

    assert result.key == "cli_key"
    assert result.source == "cli"
    assert result.configured is True


def test_env_key_beats_keyring_and_dotenv(
    tmp_path: Path, config: AppConfig, empty_keyring: FakeKeyring
) -> None:
    """环境变量优先于钥匙串与 .env。"""
    _seed_keyring(empty_keyring, config, "keyring_key")
    env_file = _write_env(tmp_path / ".env", "dotenv_key")

    result = keys.resolve_key(
        config=config,
        env={"AMAP_KEY": "env_key"},
        env_path=env_file,
        keyring_module=empty_keyring,
    )

    assert result.key == "env_key"
    assert result.source == "env"


def test_keyring_beats_dotenv(
    tmp_path: Path, config: AppConfig, empty_keyring: FakeKeyring
) -> None:
    """无环境变量时，钥匙串优先于 .env。"""
    _seed_keyring(empty_keyring, config, "keyring_key")
    env_file = _write_env(tmp_path / ".env", "dotenv_key")

    result = keys.resolve_key(
        config=config,
        env={},
        env_path=env_file,
        keyring_module=empty_keyring,
    )

    assert result.key == "keyring_key"
    assert result.source == "keyring"


def test_dotenv_used_when_nothing_else(
    tmp_path: Path, config: AppConfig, empty_keyring: FakeKeyring
) -> None:
    """四层中只有 .env 有值时命中 .env。"""
    env_file = _write_env(tmp_path / ".env", "dotenv_key")

    result = keys.resolve_key(
        config=config,
        env={},
        env_path=env_file,
        keyring_module=empty_keyring,
    )

    assert result.key == "dotenv_key"
    assert result.source == "dotenv"
    assert str(env_file) in result.detail


def test_none_when_all_sources_empty(
    tmp_path: Path, config: AppConfig, empty_keyring: FakeKeyring
) -> None:
    """四层全空时返回 none 且不抛异常。"""
    result = keys.resolve_key(
        config=config,
        env={},
        env_path=tmp_path / "missing.env",
        keyring_module=empty_keyring,
    )

    assert result.key is None
    assert result.source == "none"
    assert result.configured is False
    assert result.masked == keys.MASK_PREFIX


def test_blank_env_value_is_ignored(
    tmp_path: Path, config: AppConfig, empty_keyring: FakeKeyring
) -> None:
    """空串环境变量视为未设置，继续向下回退到 .env。"""
    env_file = _write_env(tmp_path / ".env", "dotenv_key")

    result = keys.resolve_key(
        config=config,
        env={"AMAP_KEY": "   "},
        env_path=env_file,
        keyring_module=empty_keyring,
    )

    assert result.key == "dotenv_key"
    assert result.source == "dotenv"


# --------------------------------------------------------------------------- #
# 打码与清理
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("DUMMY_AMAP_KEY_NOT_REAL_0000cdef", "****cdef"),
        ("abcdef", "****cdef"),
        ("abcd", "****"),
        ("", "****"),
        (None, "****"),
    ],
)
def test_mask_key(raw, expected: str) -> None:
    """打码只保留后 4 位，短 Key 不泄露任何字符。"""
    assert keys.mask_key(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  abc  ", "abc"),
        ('"abc"', "abc"),
        ("'abc'", "abc"),
        ("", None),
        ("   ", None),
        (None, None),
        # 复制粘贴/命令行转义残留：反斜杠 + 单引号、不配对的引号
        ("\\" + ("DUMMY_" + "a" * 26) + "'", ("DUMMY_" + "a" * 26)),
        ("'" + ("DUMMY_" + "b" * 26), ("DUMMY_" + "b" * 26)),
        ('"' + ("DUMMY_" + "c" * 26), ("DUMMY_" + "c" * 26)),
        ("`" + ("DUMMY_" + "d" * 26) + "`", ("DUMMY_" + "d" * 26)),
        ("  \\" + ("DUMMY_" + "e" * 26) + "'  ", ("DUMMY_" + "e" * 26)),
    ],
)
def test_normalize_key(raw, expected) -> None:
    """清理两侧空白、误带引号与反斜杠（Key 本身只含字母数字）。"""
    assert keys.normalize_key(raw) == expected


def test_key_hint_warns_on_unusual_length() -> None:
    """长度异常时给出温和提示，正常长度不提示。"""
    assert keys.key_hint(("DUMMY_" + "a" * 26)) is None
    hint = keys.key_hint("short")
    assert hint is not None and "32" in hint


def test_describe_key_status_masks_value(
    tmp_path: Path, config: AppConfig, empty_keyring: FakeKeyring
) -> None:
    """状态查询只返回来源与打码值。"""
    _write_env(tmp_path / ".env", "DUMMY_AMAP_KEY_NOT_REAL_0000cdef")
    source, masked = keys.describe_key_status(config=config, env={})
    assert source in {"none", "dotenv", "keyring", "env"}
    assert "DUMMY_AMAP_KEY_NOT_REAL_0000cdef" not in masked


# --------------------------------------------------------------------------- #
# 钥匙串不可用时的降级
# --------------------------------------------------------------------------- #
def test_store_key_without_keyring_guides_to_dotenv(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无可用钥匙串时返回失败与 .env 指引，而不是抛异常。"""
    # 强制模拟「没有 keyring」的环境，避免触碰真实系统钥匙串
    monkeypatch.setattr(keys, "_import_keyring", lambda: None)

    ok, message = keys.store_key("abc123", config)

    assert ok is False
    assert ".env" in message
    assert "AMAP_KEY" in message
    assert keys.keyring_backend_name() is None


def test_store_key_rejects_empty(config: AppConfig) -> None:
    """空 Key 直接拒绝并给出下一步。"""
    ok, message = keys.store_key("   ", config, keyring_module=FakeKeyring())
    assert ok is False
    assert "下一步" in message


def test_store_and_clear_key_via_fake_keyring(config: AppConfig) -> None:
    """替身钥匙串上可完成写入与删除。"""
    fake = FakeKeyring()
    ok, message = keys.store_key("DUMMY_AMAP_KEY_NOT_REAL_0000cdef", config, fake)
    assert ok is True
    assert "****cdef" in message
    assert "DUMMY_AMAP_KEY_NOT_REAL_0000cdef" not in message

    assert keys.get_stored_key(config, fake) == "DUMMY_AMAP_KEY_NOT_REAL_0000cdef"

    ok, message = keys.clear_stored_key(config, fake)
    assert ok is True
    assert keys.get_stored_key(config, fake) is None


def test_store_key_failure_falls_back_to_dotenv(
    tmp_path: Path, config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """钥匙串写入失败时，交互式设置改写入 .env 并收紧权限。"""
    env_path = tmp_path / ".env"
    monkeypatch.setattr(keys, "env_file_path", lambda cfg=None: env_path)

    ok, message = keys.prompt_and_store_key(
        config,
        keyring_module=FakeKeyring(fail_on_set=True),
        secret_input=lambda prompt="": "DUMMY_AMAP_KEY_NOT_REAL_0000cdef",
    )

    assert ok is True
    assert env_path.is_file()
    assert "AMAP_KEY=DUMMY_AMAP_KEY_NOT_REAL_0000cdef" in env_path.read_text(
        encoding="utf-8"
    )
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert "DUMMY_AMAP_KEY_NOT_REAL_0000cdef" not in message


# --------------------------------------------------------------------------- #
# .env 写入
# --------------------------------------------------------------------------- #
def test_write_dotenv_key_creates_file_with_600(tmp_path: Path) -> None:
    """新建 .env 时写入 Key 且权限为 600。"""
    target = tmp_path / ".env"
    ok, message = keys.write_dotenv_key("DUMMY_AMAP_KEY_NOT_REAL_0000cdef", target)

    assert ok is True
    assert target.read_text(encoding="utf-8").strip().endswith(
        "DUMMY_AMAP_KEY_NOT_REAL_0000cdef"
    )
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert "****cdef" in message


def test_write_dotenv_key_replaces_existing_line_and_keeps_others(
    tmp_path: Path,
) -> None:
    """已有 .env 时只替换 AMAP_KEY 行，其它变量保留。"""
    target = tmp_path / ".env"
    target.write_text(
        "OFFLINE_DEMO=true\nAMAP_KEY=old_key_value\nSENIOR_SPEED_MPS=0.8\n",
        encoding="utf-8",
    )

    ok, _ = keys.write_dotenv_key("DUMMY_AMAP_KEY_NOT_REAL_0000cdef", target)
    text = target.read_text(encoding="utf-8")

    assert ok is True
    assert "OFFLINE_DEMO=true" in text
    assert "SENIOR_SPEED_MPS=0.8" in text
    assert "old_key_value" not in text
    assert text.count("AMAP_KEY=") == 1


def test_read_dotenv_key_missing_file(tmp_path: Path) -> None:
    """缺失 .env 时返回 None，不抛异常。"""
    assert keys.read_dotenv_key(tmp_path / "nope.env") is None


def test_key_setup_guide_mentions_three_ways() -> None:
    """设置指引应包含三种方式与检查命令。"""
    guide = keys.key_setup_guide()
    assert "--set-key" in guide
    assert ".env" in guide
    assert "AMAP_KEY" in guide
    assert "--key-status" in guide


def test_clear_removes_dotenv_without_keyring(tmp_path, monkeypatch) -> None:
    target = tmp_path / ".env"
    target.write_text("OFFLINE_DEMO=true\nAMAP_KEY=test-only\nSENIOR_SPEED_MPS=0.8\n")
    monkeypatch.setattr(keys, "_import_keyring", lambda: None)
    monkeypatch.delenv("AMAP_KEY", raising=False)
    ok, message = keys.clear_stored_key()
    assert ok and ".env" in message
    assert keys.read_dotenv_key(target) is None
    assert "OFFLINE_DEMO=true" in target.read_text()
    assert "SENIOR_SPEED_MPS=0.8" in target.read_text()


def test_clear_reports_environment_key_without_disclosure(monkeypatch) -> None:
    monkeypatch.setenv("AMAP_KEY", "test-environment-only")
    monkeypatch.setattr(keys, "_import_keyring", lambda: None)
    ok, message = keys.clear_stored_key()
    assert ok
    assert "环境变量 AMAP_KEY 仍" in message
    assert "test-environment-only" not in message


@pytest.mark.skipif(os.name != "posix", reason="POSIX 文件权限")
@pytest.mark.parametrize("mask", [0o000, 0o022])
def test_dotenv_is_private_before_secret_write(tmp_path, monkeypatch, mask) -> None:
    target = tmp_path / ".env"
    original_fdopen = keys.os.fdopen
    observed = []
    class CheckedWriter:
        def __init__(self, handle): self.handle = handle
        def __enter__(self): return self
        def __exit__(self, *args): return self.handle.__exit__(*args)
        def __getattr__(self, name): return getattr(self.handle, name)
        def write(self, text):
            observed.append(stat.S_IMODE(os.fstat(self.handle.fileno()).st_mode))
            assert observed[-1] == 0o600
            return self.handle.write(text)
    monkeypatch.setattr(keys.os, "fdopen", lambda *args, **kwargs: CheckedWriter(original_fdopen(*args, **kwargs)))
    old_mask = os.umask(mask)
    try:
        ok, _ = keys.write_dotenv_key("test-only", target)
    finally:
        os.umask(old_mask)
    assert ok and observed == [0o600]
    assert target.read_text() == "AMAP_KEY=test-only\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX 文件权限")
def test_dotenv_permission_failure_keeps_existing_file(tmp_path, monkeypatch) -> None:
    target = tmp_path / ".env"
    original = "OFFLINE_DEMO=true\nAMAP_KEY=old-test-only\n"
    target.write_text(original)
    def denied(*args): raise OSError("denied")
    monkeypatch.setattr(keys.os, "fchmod", denied)
    ok, _ = keys.write_dotenv_key("new-test-only", target)
    assert not ok
    assert target.read_text() == original
    assert list(tmp_path.iterdir()) == [target]
