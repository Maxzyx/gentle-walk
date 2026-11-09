"""高德 Key 的便捷设置与读取。

优先级（由高到低）：``--key`` 命令行参数 > 环境变量 ``AMAP_KEY`` >
系统钥匙串（keyring）> 项目根目录 ``.env`` 文件。

设计要点（AGENTS.md「Key 只从环境变量读取，不硬编码，不提交真实 Key」）：
- 本模块是 Key 的**唯一**入口，其它模块不得自行 ``os.environ.get("AMAP_KEY")``；
- 任何对外输出都只给 ``****后4位``，不打印完整 Key；
- 钥匙串不可用（无桌面后端/无 keyring）时降级为提示写入 ``.env``，不抛异常；
- 读取 ``.env`` 用 python-dotenv（可用时），否则用内置极简解析，避免额外硬依赖。
"""

from __future__ import annotations

import getpass as _getpass
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from .config import PROJECT_ROOT, AppConfig, load_config

#: 环境变量名（AGENTS.md 指定的唯一变量名）
KEY_ENV_NAME = "AMAP_KEY"
#: 钥匙串里的固定账户名
KEYRING_USERNAME = "amap-key"
#: 默认 .env 文件名
ENV_FILE_NAME = ".env"
#: 打码前缀
MASK_PREFIX = "****"
#: 高德 Web 服务 Key 的常见长度，仅用于友好提示，不强制
TYPICAL_KEY_LENGTH = 32

SOURCE_LABELS = {
    "cli": "命令行参数 --key",
    "env": "环境变量 AMAP_KEY",
    "keyring": "系统钥匙串",
    "dotenv": ".env 文件",
    "none": "未配置",
}


# --------------------------------------------------------------------------- #
# 打码与校验
# --------------------------------------------------------------------------- #
def mask_key(key: Optional[str], visible: int = 4) -> str:
    """把 Key 打码成 ``****abcd``；长度不足时只返回 ``****``。"""
    if not key:
        return MASK_PREFIX
    text = str(key).strip()
    if len(text) <= visible:
        return MASK_PREFIX
    return f"{MASK_PREFIX}{text[-visible:]}"


#: 复制粘贴/命令行转义常带在 Key 两侧的字符（Key 本身只含字母数字，可安全剥离）
_WRAPPING_CHARS = frozenset(['\\', '"', "'", '`'])


def normalize_key(raw: Optional[str]) -> Optional[str]:
    """清理 Key 两侧空白、引号、反斜杠等复制粘贴残留；空值返回 None。

    ``.env`` 里常见 ``AMAP_KEY='xxx'``、``AMAP_KEY=\\'xxx'`` 这类写法，
    引号/反斜杠不是 Key 的一部分，这里统一剥掉，避免把整段当成 Key 去请求。
    """
    if raw is None:
        return None
    text = str(raw).strip()
    # 逐层剥离两侧的引号/反斜杠（含不配对的残留），直到干净
    changed = True
    while changed and text:
        changed = False
        while text and text[0] in _WRAPPING_CHARS:
            text = text[1:].strip()
            changed = True
        while text and text[-1] in _WRAPPING_CHARS:
            text = text[:-1].strip()
            changed = True
    return text or None


def key_hint(key: Optional[str]) -> Optional[str]:
    """对 Key 长度做温和提示，不阻断使用。"""
    if not key:
        return None
    if len(key) != TYPICAL_KEY_LENGTH:
        return (
            f"注意：Key 长度为 {len(key)}，高德 Web 服务 Key 常见为 "
            f"{TYPICAL_KEY_LENGTH} 位；如后续调用失败，请到控制台核对。"
        )
    return None


# --------------------------------------------------------------------------- #
# 钥匙串
# --------------------------------------------------------------------------- #
def _import_keyring() -> Any:
    """导入 keyring；未安装或无可用后端时返回 None（调用方据此降级）。"""
    try:
        import keyring  # noqa: PLC0415 - 可选依赖，按需导入
    except Exception:  # noqa: BLE001
        return None
    try:
        backend = keyring.get_keyring()
    except Exception:  # noqa: BLE001
        return None
    backend_name = type(backend).__name__
    # keyring 无后端时使用 fail.Keyring，写入会抛 NoKeyringError
    if backend_name.lower().startswith("fail"):
        return None
    return keyring


def keyring_backend_name(keyring_module: Any = None) -> Optional[str]:
    """返回钥匙串后端名称；不可用返回 None。"""
    module = keyring_module if keyring_module is not None else _import_keyring()
    if module is None:
        return None
    try:
        backend = module.get_keyring()
    except Exception:  # noqa: BLE001
        return None
    return type(backend).__name__


def _service_name(config: Optional[AppConfig]) -> str:
    """取钥匙串服务名（可在配置中改名）。"""
    if config is None:
        return "laochengqu-amap"
    return config.runtime.keyring_service


def get_stored_key(
    config: Optional[AppConfig] = None, keyring_module: Any = None
) -> Optional[str]:
    """从系统钥匙串读取 Key；不可用或未存返回 None。"""
    module = keyring_module if keyring_module is not None else _import_keyring()
    if module is None:
        return None
    try:
        value = module.get_password(_service_name(config), KEYRING_USERNAME)
    except Exception:  # noqa: BLE001 - 钥匙串异常一律视为不可用
        return None
    return normalize_key(value)


def store_key(
    key: str, config: Optional[AppConfig] = None, keyring_module: Any = None
) -> Tuple[bool, str]:
    """把 Key 写入系统钥匙串。

    Returns:
        (是否成功, 面向用户的中文提示；失败时包含下一步怎么做)
    """
    normalized = normalize_key(key)
    if not normalized:
        return False, "Key 为空，未写入。下一步：重新执行 --set-key 并粘贴完整 Key。"

    module = keyring_module if keyring_module is not None else _import_keyring()
    if module is None:
        return False, (
            "当前系统没有可用的安全钥匙串（keyring 缺失或无桌面后端）。"
            "下一步：把 Key 写入项目根目录的 .env 文件，内容为 "
            f"{KEY_ENV_NAME}=你的Key，然后执行 chmod 600 .env。"
        )
    try:
        module.set_password(_service_name(config), KEYRING_USERNAME, normalized)
    except Exception as exc:  # noqa: BLE001
        return False, (
            f"写入钥匙串失败：{exc}。下一步：改用 .env 方式，内容为 "
            f"{KEY_ENV_NAME}=你的Key。"
        )
    return True, f"已写入系统钥匙串（服务 {_service_name(config)}），值 {mask_key(normalized)}"


def clear_stored_key(
    config: Optional[AppConfig] = None, keyring_module: Any = None
) -> Tuple[bool, str]:
    """清除钥匙串和 .env 保存的 Key，并说明仍有效的进程环境变量。"""
    messages = []
    failures = []
    module = keyring_module if keyring_module is not None else _import_keyring()
    if module is not None:
        try:
            if module.get_password(_service_name(config), KEYRING_USERNAME) is not None:
                module.delete_password(_service_name(config), KEYRING_USERNAME)
                messages.append("已从系统钥匙串删除 AMAP_KEY。")
        except Exception:
            failures.append("钥匙串清除失败。下一步：解锁钥匙串后重试。")
    target = env_file_path(config)
    try:
        if target.is_symlink():
            raise OSError(".env 是符号链接")
        if target.is_file():
            kept = []
            removed = False
            for line in target.read_text(encoding="utf-8").splitlines():
                text = line.strip()
                name = text.partition("=")[0].strip()
                if name.startswith("export "):
                    name = name[7:].strip()
                if text and not text.startswith("#") and "=" in text and name == KEY_ENV_NAME:
                    removed = True
                else:
                    kept.append(line)
            if removed:
                _atomic_write_dotenv(target, "\n".join(kept).rstrip() + ("\n" if kept else ""))
                messages.append("已从 .env 删除 AMAP_KEY，其他变量已保留。")
    except (OSError, UnicodeDecodeError) as exc:
        failures.append(f".env 清除失败：{exc}。下一步：检查文件权限及 UTF-8 编码。")
    if not messages and not failures:
        messages.append("未发现可清除的已保存 AMAP_KEY。")
    if normalize_key(os.environ.get(KEY_ENV_NAME)):
        messages.append("环境变量 AMAP_KEY 仍在当前会话中生效。下一步：在启动终端取消该变量后用 --key-status 确认。")
    return not failures, "\n".join(messages + failures)


# --------------------------------------------------------------------------- #
# .env 文件
# --------------------------------------------------------------------------- #
def env_file_path(config: Optional[AppConfig] = None) -> Path:
    """返回 .env 路径（固定项目根目录，避免受工作目录影响）。"""
    return PROJECT_ROOT / ENV_FILE_NAME


def read_dotenv_key(path: Optional[Path] = None) -> Optional[str]:
    """从 .env 读取 AMAP_KEY；文件缺失或无此键返回 None。"""
    target = Path(path) if path is not None else env_file_path()
    if not target.is_file():
        return None
    values = _parse_dotenv(target)
    return normalize_key(values.get(KEY_ENV_NAME))


def _parse_dotenv(path: Path) -> Mapping[str, str]:
    """读取 .env；优先 python-dotenv，缺失时用极简解析（KEY=VALUE、忽略注释）。"""
    try:  # 优先成熟库（AGENTS.md：配置优先 python-dotenv）
        from dotenv import dotenv_values  # noqa: PLC0415

        parsed = dotenv_values(str(path))
        return {str(k): str(v) for k, v in parsed.items() if k and v is not None}
    except Exception:  # noqa: BLE001 - 未安装或解析失败时降级
        pass
    result = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return result
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        result[name.strip()] = value.strip()
    return result


def _atomic_write_dotenv(target: Path, text: str) -> None:
    """先以私有权限建立文件，再写秘密并原子替换，失败保留旧文件。"""
    if target.is_symlink():
        raise OSError(".env 是符号链接，请改用普通文件")
    if os.name != "posix":
        raise OSError("当前平台无法确认私有文件 ACL，请使用系统钥匙串或环境变量")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            os.fchmod(handle.fileno(), 0o600)
            if stat.S_IMODE(os.fstat(handle.fileno()).st_mode) != 0o600:
                raise OSError("无法建立拥有者专属权限")
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(target))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_dotenv_key(
    key: str, path: Optional[Path] = None
) -> Tuple[bool, str]:
    """把 Key 写入/更新 .env（不覆盖其它变量），并把权限收紧到 600。"""
    normalized = normalize_key(key)
    if not normalized:
        return False, "Key 为空，未写入 .env。下一步：重新执行并粘贴完整 Key。"
    target = Path(path) if path is not None else env_file_path()
    if "\n" in normalized or "\r" in normalized:
        return False, "Key 不能包含换行。下一步：重新粘贴单行完整 Key。"
    if target.is_symlink():
        return False, ".env 是符号链接，未写入。下一步：改用普通文件或系统钥匙串。"

    existing_lines = []
    if target.is_file():
        try:
            existing_lines = target.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            return False, f"读取 .env 失败：{exc}。下一步：检查文件权限。"

    replaced = False
    new_lines = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            name = stripped.partition("=")[0].strip()
            if name == KEY_ENV_NAME:
                new_lines.append(f"{KEY_ENV_NAME}={normalized}")
                replaced = True
                continue
        new_lines.append(line)
    if not replaced:
        new_lines.append(f"{KEY_ENV_NAME}={normalized}")

    try:
        _atomic_write_dotenv(target, "\n".join(new_lines).rstrip() + "\n")
    except OSError as exc:
        return False, f"写入 .env 失败：{exc}。下一步：检查目录写权限。"

    return True, (
        f"已写入 {target}（权限 600），值 {mask_key(normalized)}。"
        "注意：.env 已被 .gitignore 排除，不要提交到仓库。"
    )


def restrict_env_file_permissions(path: Optional[Path] = None) -> Optional[str]:
    """把已存在的 .env 权限收紧到 600；无法收紧时返回原因。"""
    target = Path(path) if path is not None else env_file_path()
    if not target.is_file():
        return None
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
        if mode & 0o077:
            target.chmod(0o600)
            return f"已将 {target.name} 权限收紧为 600"
    except OSError as exc:
        return f"无法调整 {target.name} 权限：{exc}"
    return None


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class KeyResolution:
    """Key 解析结果。"""

    key: Optional[str]
    source: str
    detail: str = ""

    @property
    def configured(self) -> bool:
        """是否已配置可用 Key。"""
        return bool(self.key)

    @property
    def masked(self) -> str:
        """打码后的 Key。"""
        return mask_key(self.key)

    @property
    def source_label(self) -> str:
        """来源的中文描述。"""
        return SOURCE_LABELS.get(self.source, self.source)


def resolve_key(
    cli_key: Optional[str] = None,
    config: Optional[AppConfig] = None,
    env: Optional[Mapping[str, str]] = None,
    env_path: Optional[Path] = None,
    keyring_module: Any = None,
) -> KeyResolution:
    """按优先级解析出可用 Key。

    Args:
        cli_key: 命令行传入的 Key（最高优先级，仅当次有效）。
        config: 应用配置，用于取钥匙串服务名。
        env: 环境变量映射，默认 ``os.environ``（测试可注入）。
        env_path: .env 路径，默认项目根目录。
        keyring_module: 可注入的 keyring 模块（测试用），默认自动探测。
    """
    env_map: Mapping[str, str] = os.environ if env is None else env

    candidate = normalize_key(cli_key)
    if candidate:
        return KeyResolution(candidate, "cli", "来自本次命令行参数，不持久化")

    candidate = normalize_key(env_map.get(KEY_ENV_NAME))
    if candidate:
        return KeyResolution(candidate, "env", f"来自环境变量 {KEY_ENV_NAME}")

    candidate = get_stored_key(config, keyring_module)
    if candidate:
        return KeyResolution(candidate, "keyring", "来自系统钥匙串")

    dotenv_target = Path(env_path) if env_path is not None else env_file_path()
    candidate = read_dotenv_key(dotenv_target)
    if candidate:
        return KeyResolution(candidate, "dotenv", f"来自 {dotenv_target}")

    return KeyResolution(None, "none", "四层来源均未命中")


def describe_key_status(
    config: Optional[AppConfig] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Tuple[str, str]:
    """返回（来源标识, 打码后的值），供 selfcheck 显示；永不返回完整 Key。"""
    resolution = resolve_key(config=config, env=env)
    return resolution.source, resolution.masked


def key_setup_guide(config: Optional[AppConfig] = None) -> str:
    """给出三条设置 Key 的方法，供 CLI 提示使用。"""
    backend = keyring_backend_name()
    lines = [
        "设置 AMAP_KEY 的三种方式（任选其一，推荐方式一）：",
        "  方式一（推荐，写入系统钥匙串）：python -m src.cli --set-key",
    ]
    if backend:
        lines.append(f"    钥匙串后端：{backend}")
    else:
        lines.append("    钥匙串不可用，请用方式二或三；仍需在线能力时再配置")
    lines.append(
        f"  方式二：在项目根目录创建 .env，写入一行 {KEY_ENV_NAME}=你的Key"
        "（已被 .gitignore 排除）"
    )
    lines.append(f"  方式三：临时环境变量 export {KEY_ENV_NAME}=你的Key（仅当前终端有效）")
    lines.append("检查当前是否配置：python -m src.cli --key-status")
    return "\n".join(lines)


def prompt_and_store_key(
    config: Optional[AppConfig] = None,
    keyring_module: Any = None,
    secret_input: Optional[Any] = None,
    fallback_to_dotenv: bool = True,
) -> Tuple[bool, str]:
    """交互式读取 Key 并持久化：先钥匙串，失败则写 .env（可选）。

    输入用 ``getpass``（不回显）。任何情况下都不把 Key 打印出来。
    """
    reader = secret_input or _getpass.getpass
    try:
        raw = reader("请输入高德 AMAP_KEY（输入不回显，直接回车取消）：")
    except (EOFError, KeyboardInterrupt):
        return False, "已取消，未做任何修改。"

    normalized = normalize_key(raw)
    if not normalized:
        return False, "未输入内容，已取消。"

    ok, message = store_key(normalized, config, keyring_module)
    note = key_hint(normalized)
    if note:
        message = f"{message}\n{note}"
    if ok:
        restrict_env_file_permissions()
        return True, message

    if not fallback_to_dotenv:
        return False, message
    dotenv_ok, dotenv_message = write_dotenv_key(normalized)
    if dotenv_ok:
        return True, f"{message}\n已改用 .env 保存：{dotenv_message}"
    return False, f"{message}\n写入 .env 也失败：{dotenv_message}"


def load_config_safe() -> AppConfig:
    """读取配置；失败时回退到内置默认（保证 Key 工具始终可用）。"""
    try:
        return load_config()
    except Exception:  # noqa: BLE001 - Key 工具不应因配置损坏而不可用
        from .config import AreaConfig, RiskConfig, RuntimeConfig, ScoringConfig

        return AppConfig(
            area=AreaConfig(
                name="内置默认片区",
                bbox=(108.928, 34.247, 108.963, 34.276),
                center=(108.9455, 34.2611),
            ),
            runtime=RuntimeConfig(),
            scoring=ScoringConfig(),
            risk=RiskConfig(),
            quadtree=_default_quadtree(),
            source_path=PROJECT_ROOT / "config" / "default.json",
        )


def _default_quadtree():
    """构造内置四叉树默认配置（避免 load_config_safe 依赖配置文件）。"""
    from .config import QuadtreeConfig

    return QuadtreeConfig()
