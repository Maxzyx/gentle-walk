"""高德 Web 服务 HTTP 客户端：超时、429 退避、哈希缓存。

AGENTS.md 约束：
- Key 只从 ``src/keys.py`` 解析，不硬编码、不打印完整值；
- 429 读 ``Retry-After`` 或指数退避（1/2/4/8/16 秒），最多 5 次，仍失败则降级离线；
- 相同查询用哈希缓存，不第二次打到网络。

本模块不做业务解析，只负责「把一次 GET 请求可靠地变成 JSON 或一个明确的失败」。
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from .. import keys
from ..config import AppConfig

#: 高德 Web 服务根地址
AMAP_BASE_URL = "https://restapi.amap.com"
#: 429 重试的默认等待序列（秒），最多 5 次
DEFAULT_BACKOFF_S: Tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0)
#: 单次请求超时（秒），可被配置覆盖
DEFAULT_TIMEOUT_S = 8.0
#: 高德「限流/配额」类业务错误码：可退避重试，而不是直接失败
#: 10021 CUQPS 并发超限、10029 QPS 超限、10044 单 Key 限流、10004 日配额超限
RETRYABLE_INFOCODES = frozenset({"10021", "10029", "10044", "10004"})


class AmapNetworkError(RuntimeError):
    """网络层失败（超时、DNS、连接中断）。调用方应降级离线。"""


class AmapUnavailable(RuntimeError):
    """在线高德不可用（无 Key、重试耗尽、返回业务错误码）。"""


@dataclass
class AmapResponse:
    """一次请求的结果。"""

    ok: bool
    data: Dict[str, Any] = field(default_factory=dict)
    status: int = 0
    error: str = ""
    cached: bool = False

    @property
    def pois(self) -> list:
        """POI 列表（POI 搜索接口）。"""
        value = self.data.get("pois")
        return value if isinstance(value, list) else []

    @property
    def count(self) -> int:
        """结果总数（高德返回的 ``count`` 字段）。"""
        try:
            return int(self.data.get("count") or 0)
        except (TypeError, ValueError):
            return 0


# --------------------------------------------------------------------------- #
# 网络与缓存
# --------------------------------------------------------------------------- #
def _urlopen(url: str, timeout_s: float) -> Tuple[int, Dict[str, str], bytes]:
    """执行 GET，返回（状态码, 响应头, 响应体）；网络异常转 AmapNetworkError。"""
    request = urllib.request.Request(url, headers={"User-Agent": "gentle-walk-mvp/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            headers = {key.lower(): value for key, value in response.headers.items()}
            return int(getattr(response, "status", 200)), headers, response.read()
    except urllib.error.HTTPError as exc:
        headers = {key.lower(): value for key, value in (exc.headers or {}).items()}
        try:
            body = exc.read()
        except Exception:  # noqa: BLE001
            body = b""
        return int(exc.code), headers, body
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AmapNetworkError(
            f"网络请求失败：{exc}。下一步：检查网络，或继续使用离线演示模式。"
        ) from exc


def cache_key(path: str, params: Mapping[str, str]) -> str:
    """计算缓存键：路径 + 排序后的参数（**不含 key**），sha256 十六进制。"""
    filtered = {k: v for k, v in params.items() if k.lower() != "key"}
    payload = path + "?" + "&".join(f"{k}={filtered[k]}" for k in sorted(filtered))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AmapClient:
    """高德 Web 服务客户端（可注入网络层与时钟，便于测试）。"""

    def __init__(
        self,
        config: AppConfig,
        key: Optional[str] = None,
        opener: Optional[Callable[[str, float], Tuple[int, Dict[str, str], bytes]]] = None,
        sleeper: Optional[Callable[[float], None]] = None,
        cache_dir: Optional[Path] = None,
        base_url: str = AMAP_BASE_URL,
    ) -> None:
        self._config = config
        resolution = keys.resolve_key(config=config) if key is None else None
        self._key = key if key is not None else (resolution.key if resolution else None)
        self._opener = opener or _urlopen
        self._sleeper = sleeper or time.sleep
        self._cache_dir = Path(cache_dir) if cache_dir else config.runtime.cache_dir
        self._base_url = base_url.rstrip("/")
        self.request_count = 0

    @property
    def has_key(self) -> bool:
        """是否已配置 Key。"""
        return bool(self._key)

    @property
    def masked_key(self) -> str:
        """打码后的 Key，供日志使用。"""
        return keys.mask_key(self._key)

    def _cache_path(self, key: str) -> Path:
        """缓存文件路径。"""
        return self._cache_dir / f"{key}.json"

    def _read_cache(self, key: str) -> Optional[Dict[str, Any]]:
        """读缓存；未启用或不存在返回 None。"""
        if not self._config.runtime.cache_enabled:
            return None
        path = self._cache_path(key)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write_cache(self, key: str, data: Dict[str, Any]) -> None:
        """写缓存；失败不影响主流程（缓存只是优化）。"""
        if not self._config.runtime.cache_enabled:
            return
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache_path(key).write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            return

    def get(self, path: str, params: Mapping[str, str]) -> AmapResponse:
        """发起一次 GET 请求（带缓存与 429 退避）。

        Raises:
            AmapUnavailable: 无 Key 或重试耗尽仍不可用。
        """
        if not self._key:
            raise AmapUnavailable(
                "未配置 AMAP_KEY，无法访问在线高德。"
                "下一步：python -m src.cli --set-key 配置后重试，或继续用离线模式。"
            )

        full_params: Dict[str, str] = dict(params)
        full_params["key"] = self._key

        cache_id = cache_key(path, full_params)
        cached = self._read_cache(cache_id)
        if cached is not None:
            return AmapResponse(ok=True, data=cached, status=200, cached=True)

        url = f"{self._base_url}{path}?{urllib.parse.urlencode(full_params)}"
        timeout_s = float(self._config.runtime.http_timeout_s or DEFAULT_TIMEOUT_S)
        max_retries = int(self._config.runtime.max_retries)

        last_error = ""
        for attempt in range(max_retries + 1):
            self.request_count += 1
            status, headers, body = self._opener(url, timeout_s)

            if status == 429:
                if attempt >= max_retries:
                    raise AmapUnavailable(
                        "高德接口持续限流（HTTP 429），已重试 "
                        f"{max_retries} 次。下一步：稍后再试，或改用离线演示模式。"
                    )
                wait_s = _retry_after_seconds(headers, attempt)
                self._sleeper(wait_s)
                last_error = f"HTTP 429 限流，等待 {wait_s:.0f} 秒后重试"
                continue

            if status >= 500:
                if attempt >= max_retries:
                    raise AmapUnavailable(
                        f"高德接口返回 HTTP {status}，已重试 {max_retries} 次。"
                        "下一步：稍后再试，或改用离线演示模式。"
                    )
                wait_s = DEFAULT_BACKOFF_S[min(attempt, len(DEFAULT_BACKOFF_S) - 1)]
                self._sleeper(wait_s)
                last_error = f"HTTP {status}"
                continue

            if status != 200:
                raise AmapUnavailable(
                    f"高德接口返回 HTTP {status}（{last_error or '无更多信息'}）。"
                    "下一步：检查 Key 是否有效、配额是否用尽，或改用离线模式。"
                )

            try:
                data = json.loads(body.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as exc:
                raise AmapUnavailable(
                    f"高德接口返回内容不是合法 JSON：{exc}。下一步：稍后重试或改用离线模式。"
                ) from exc

            if str(data.get("status", "0")) != "1":
                infocode = str(data.get("infocode", "") or "")
                if infocode in RETRYABLE_INFOCODES and attempt < max_retries:
                    wait_s = DEFAULT_BACKOFF_S[min(attempt, len(DEFAULT_BACKOFF_S) - 1)]
                    self._sleeper(wait_s)
                    last_error = f"业务限流 {infocode}（{data.get('info', '')}）"
                    continue
                raise AmapUnavailable(
                    "高德接口返回业务错误："
                    f"info={data.get('info', '')} infocode={infocode}。"
                    "下一步：按高德文档核对参数与配额，或改用离线模式。"
                )

            self._write_cache(cache_id, data)
            return AmapResponse(ok=True, data=data, status=status)

        raise AmapUnavailable(
            f"高德接口不可用（{last_error}）。下一步：改用离线演示模式。"
        )


def _retry_after_seconds(headers: Mapping[str, str], attempt: int) -> float:
    """取 Retry-After；缺失或非法时用指数退避序列。"""
    raw = headers.get("retry-after")
    if raw:
        try:
            return max(0.0, float(str(raw).strip()))
        except ValueError:
            pass
    return DEFAULT_BACKOFF_S[min(attempt, len(DEFAULT_BACKOFF_S) - 1)]
