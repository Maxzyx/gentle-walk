"""配置加载：``config/default.json`` + 环境变量覆盖。

设计原则（AGENTS.md「配置外置」）：
- 代码里只保留读取逻辑与安全默认值，片区参数写在 JSON 里，换片区只改配置；
- 环境变量优先级高于 JSON，便于在线演示时临时开关；
- 不在本模块读取 ``AMAP_KEY`` 的值（Key 的解析与打码统一走 ``src/keys.py``）。
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

#: 项目根目录（``src/config.py`` 的上两级）
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH: Path = PROJECT_ROOT / "config" / "default.json"

BBox = Tuple[float, float, float, float]  # (lon_min, lat_min, lon_max, lat_max)
Point = Tuple[float, float]  # (lon, lat)


# --------------------------------------------------------------------------- #
# 环境变量工具
# --------------------------------------------------------------------------- #
def _env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    """读取字符串环境变量，空串视为未设置。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def _env_bool(name: str, default: bool) -> bool:
    """读取布尔环境变量，接受 true/false/1/0/yes/no/on/off。"""
    raw = _env_str(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "y", "on"}:
        return True
    if lowered in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _env_float(name: str, default: float) -> float:
    """读取浮点环境变量，非法值回退默认。"""
    raw = _env_str(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    """读取整型环境变量，非法值回退默认。"""
    raw = _env_str(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# --------------------------------------------------------------------------- #
# 配置数据类
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AreaConfig:
    """片区配置。"""

    name: str
    bbox: BBox
    center: Point
    margin_deg: float = 0.05
    default_origin_name: str = "样例起点"

    def contains(self, lon: float, lat: float, pad: Optional[float] = None) -> bool:
        """判断坐标是否落在片区 bbox（可放宽 ``pad`` 度）。"""
        margin = self.margin_deg if pad is None else pad
        lon_min, lat_min, lon_max, lat_max = self.bbox
        return (
            lon_min - margin <= lon <= lon_max + margin
            and lat_min - margin <= lat <= lat_max + margin
        )


@dataclass(frozen=True)
class RuntimeConfig:
    """运行期开关：离线模式、目录、HTTP 超时与重试、钥匙串服务名。"""

    offline_demo: bool = True
    data_dir: Path = PROJECT_ROOT / "data"
    cache_dir: Path = PROJECT_ROOT / "cache" / "amap"
    cache_enabled: bool = True
    http_timeout_s: float = 8.0
    max_retries: int = 5
    retry_backoff_base_s: float = 1.0
    keyring_service: str = "laochengqu-amap"


@dataclass(frozen=True)
class ScoringConfig:
    """路线评分权重（单位：等效分钟）。系数为 MVP 默认值，待现场校准。"""

    senior_speed_mps: float = 0.9
    stairs_base: float = 0.5
    stairs_per_step: float = 0.9
    stairs_default_when_missing: float = 8.0
    slope_base: float = 3.0
    slope_per_pct: float = 0.6
    slope_steep_pct: float = 8.0
    slope_steep_multiplier: float = 1.5
    slope_default_when_missing: float = 6.0
    crossing_cost: float = 2.5
    obstacle_by_severity: Tuple[float, ...] = (2.0, 4.0, 8.0)
    fatigue_threshold_m: float = 200.0
    fatigue_cost: float = 3.0
    rest_buffer_m: float = 200.0
    rest_reward_per_point: float = 1.0
    rest_reward_cap: float = 5.0
    ramp_bonus: float = 1.5
    no_ramp_stairs_penalty: float = 2.0


@dataclass(frozen=True)
class RiskConfig:
    """风险分级阈值。"""

    low_max_stairs: int = 0
    medium_max_stairs: int = 10
    high_min_stairs: int = 11
    low_max_slope_pct: float = 5.0
    steep_slope_pct: float = 8.0
    low_max_crossings: int = 1
    medium_max_crossings: int = 3


@dataclass(frozen=True)
class QuadtreeConfig:
    """POI 四叉树拆分参数（AGENTS.md：阈值 180 条、0.005 度、深度上限）。"""

    poi_threshold: int = 180
    min_span_deg: float = 0.005
    max_depth: int = 6
    page_size: int = 25
    keywords: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)

    def keywords_for(self, category: str) -> Tuple[str, ...]:
        """取某类目的关键词组，未知类目返回空元组。"""
        return tuple(self.keywords.get(category, ()))


@dataclass(frozen=True)
class AppConfig:
    """聚合配置。"""

    area: AreaConfig
    runtime: RuntimeConfig
    scoring: ScoringConfig
    risk: RiskConfig
    quadtree: QuadtreeConfig
    source_path: Path

    @property
    def offline_demo(self) -> bool:
        """是否强制离线演示（AGENTS.md 默认 True）。"""
        return self.runtime.offline_demo


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #
class ConfigError(RuntimeError):
    """配置文件缺失或结构非法。错误信息面向用户，提示下一步怎么做。"""


_TRUE_WORDS = {"1", "true", "yes", "y", "on"}
_FALSE_WORDS = {"0", "false", "no", "n", "off"}


def _override(env_map: Mapping[str, str], name: str, default: Any, caster):
    """从 env 映射读取并按 ``caster`` 转换；未设置或非法时返回 ``default``。"""
    raw_value = env_map.get(name)
    if raw_value is None or not str(raw_value).strip():
        return default
    text = str(raw_value).strip()
    if caster is bool:
        lowered = text.lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
        return default
    try:
        return caster(text)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_bbox(raw: Any, fallback: BBox) -> BBox:
    """缺失用默认，显式非法坐标由配置异常报告。"""
    if raw is None:
        return fallback
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise ValueError("area.bbox 必须包含四个经纬度")
    values = tuple(float(value) for value in raw)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("area.bbox 不能包含 NaN 或无穷大")
    a, b, c, d = values
    if not (-180 <= a < c <= 180 and -90 <= b < d <= 90):
        raise ValueError("area.bbox 范围或经纬度顺序不合法")
    return a, b, c, d


def _as_point(raw: Any, fallback: Point) -> Point:
    """显式非法起点不能冒充有效坐标。"""
    if raw is None:
        return fallback
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ValueError("area.center 必须包含经度和纬度")
    lon, lat = float(raw[0]), float(raw[1])
    if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError("area.center 不是合法的有限经纬度")
    return lon, lat


def _as_keywords(raw: Any) -> Dict[str, Tuple[str, ...]]:
    """把关键词字典规范化成 ``{类目: 关键词元组}``，丢弃空串。"""
    result: Dict[str, Tuple[str, ...]] = {}
    if not isinstance(raw, dict):
        return result
    for category, words in raw.items():
        if isinstance(words, (list, tuple)):
            cleaned = tuple(str(w).strip() for w in words if str(w).strip())
            if cleaned:
                result[str(category)] = cleaned
    return result


def _read_json(path: Path) -> Dict[str, Any]:
    """读取 JSON 配置；缺失或损坏时抛出带中文处置建议的 ConfigError。"""
    if not path.exists():
        raise ConfigError(
            f"未找到配置文件：{path}。下一步：确认 config/default.json 存在，"
            "或在命令行用 --config 指定配置文件路径。"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            f"配置文件无法读取：{path}（{exc}）。下一步：检查文件权限后重试。"
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"配置文件不是合法 JSON：{path} 第 {exc.lineno} 行第 {exc.colno} 列，"
            f"原因：{exc.msg}。下一步：修正该行逗号或引号后重试。"
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(
            f"配置文件顶层必须是 JSON 对象：{path}。下一步：参考 config/default.json 结构重写。"
        )
    return data


def _load_config(
    path: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> AppConfig:
    """加载配置并应用环境变量覆盖。

    Args:
        path: 配置文件路径，默认 ``config/default.json``。
        env: 环境变量映射，默认取 ``os.environ``；测试可注入伪环境。

    Raises:
        ConfigError: 文件缺失、非 JSON 或顶层结构非法。
    """
    resolved = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not resolved.is_absolute():
        resolved = PROJECT_ROOT / resolved

    # 允许通过环境变量（或注入的 env 映射）覆盖配置文件路径本身
    env_map: Mapping[str, str] = os.environ if env is None else env
    env_path_raw = env_map.get("APP_CONFIG")
    if path is None and env_path_raw and env_path_raw.strip():
        candidate = Path(env_path_raw.strip())
        resolved = candidate if candidate.is_absolute() else PROJECT_ROOT / candidate

    raw = _read_json(resolved)

    sections = {}
    for name in ("area", "runtime", "scoring", "risk", "quadtree"):
        value = raw.get(name)
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise ValueError(f"{name} 必须是 JSON 对象")
        sections[name] = value
    area_raw = sections["area"]
    runtime_raw = sections["runtime"]
    scoring_raw = sections["scoring"]
    risk_raw = sections["risk"]
    quad_raw = sections["quadtree"]

    area = AreaConfig(
        name=str(area_raw.get("name") or "西安城墙内-样例片区"),
        bbox=_as_bbox(area_raw.get("bbox"), (108.928, 34.247, 108.963, 34.276)),
        center=_as_point(area_raw.get("center"), (108.9455, 34.2611)),
        margin_deg=float(area_raw.get("margin_deg", 0.05)),
        default_origin_name=str(area_raw.get("default_origin_name") or "样例起点"),
    )

    def _env_or(*, env_name: str, default: Any, caster):
        """优先取注入的 env 映射，其次取 JSON 值，最后用默认值。"""
        return _override(env_map, env_name, default, caster)

    data_dir_value = str(runtime_raw.get("data_dir") or "data")
    cache_dir_value = str(runtime_raw.get("cache_dir") or "cache/amap")
    data_dir_raw = _override(env_map, "DATA_DIR", data_dir_value, str)
    cache_dir_raw = _override(env_map, "CACHE_DIR", cache_dir_value, str)

    data_dir = Path(data_dir_raw) if data_dir_raw else PROJECT_ROOT / "data"
    cache_dir = Path(cache_dir_raw) if cache_dir_raw else PROJECT_ROOT / "cache" / "amap"
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir
    if not cache_dir.is_absolute():
        cache_dir = PROJECT_ROOT / cache_dir

    runtime = RuntimeConfig(
        offline_demo=_env_or(
            env_name="OFFLINE_DEMO",
            default=bool(runtime_raw.get("offline_demo", True)),
            caster=bool,
        ),
        data_dir=data_dir,
        cache_dir=cache_dir,
        cache_enabled=_env_or(
            env_name="CACHE_ENABLED",
            default=bool(runtime_raw.get("cache_enabled", True)),
            caster=bool,
        ),
        http_timeout_s=_env_or(
            env_name="AMAP_TIMEOUT_S",
            default=float(runtime_raw.get("http_timeout_s", 8.0)),
            caster=float,
        ),
        max_retries=_env_or(
            env_name="AMAP_MAX_RETRIES",
            default=int(runtime_raw.get("max_retries", 5)),
            caster=int,
        ),
        retry_backoff_base_s=float(runtime_raw.get("retry_backoff_base_s", 1.0)),
        keyring_service=str(runtime_raw.get("keyring_service") or "laochengqu-amap"),
    )

    scoring = ScoringConfig(
        senior_speed_mps=_env_or(
            env_name="SENIOR_SPEED_MPS",
            default=float(scoring_raw.get("senior_speed_mps", 0.9)),
            caster=float,
        ),
        stairs_base=float(scoring_raw.get("stairs_base", 0.5)),
        stairs_per_step=float(scoring_raw.get("stairs_per_step", 0.9)),
        stairs_default_when_missing=float(
            scoring_raw.get("stairs_default_when_missing", 8.0)
        ),
        slope_base=float(scoring_raw.get("slope_base", 3.0)),
        slope_per_pct=float(scoring_raw.get("slope_per_pct", 0.6)),
        slope_steep_pct=float(scoring_raw.get("slope_steep_pct", 8.0)),
        slope_steep_multiplier=float(scoring_raw.get("slope_steep_multiplier", 1.5)),
        slope_default_when_missing=float(
            scoring_raw.get("slope_default_when_missing", 6.0)
        ),
        crossing_cost=float(scoring_raw.get("crossing_cost", 2.5)),
        obstacle_by_severity=_as_floats(
            scoring_raw.get("obstacle_by_severity"), (2.0, 4.0, 8.0)
        ),
        fatigue_threshold_m=float(scoring_raw.get("fatigue_threshold_m", 200.0)),
        fatigue_cost=float(scoring_raw.get("fatigue_cost", 3.0)),
        rest_buffer_m=float(scoring_raw.get("rest_buffer_m", 200.0)),
        rest_reward_per_point=float(scoring_raw.get("rest_reward_per_point", 1.0)),
        rest_reward_cap=float(scoring_raw.get("rest_reward_cap", 5.0)),
        ramp_bonus=float(scoring_raw.get("ramp_bonus", 1.5)),
        no_ramp_stairs_penalty=float(scoring_raw.get("no_ramp_stairs_penalty", 2.0)),
    )

    risk = RiskConfig(
        low_max_stairs=int(risk_raw.get("low_max_stairs", 0)),
        medium_max_stairs=int(risk_raw.get("medium_max_stairs", 10)),
        high_min_stairs=int(risk_raw.get("high_min_stairs", 11)),
        low_max_slope_pct=float(risk_raw.get("low_max_slope_pct", 5.0)),
        steep_slope_pct=float(risk_raw.get("steep_slope_pct", 8.0)),
        low_max_crossings=int(risk_raw.get("low_max_crossings", 1)),
        medium_max_crossings=int(risk_raw.get("medium_max_crossings", 3)),
    )

    quadtree = QuadtreeConfig(
        poi_threshold=_env_or(
            env_name="POI_QUADTREE_THRESHOLD",
            default=int(quad_raw.get("poi_threshold", 180)),
            caster=int,
        ),
        min_span_deg=float(quad_raw.get("min_span_deg", 0.005)),
        max_depth=_env_or(
            env_name="POI_QUADTREE_MAX_DEPTH",
            default=int(quad_raw.get("max_depth", 6)),
            caster=int,
        ),
        page_size=int(quad_raw.get("page_size", 25)),
        keywords=_as_keywords(quad_raw.get("keywords")),
    )

    return AppConfig(
        area=area,
        runtime=runtime,
        scoring=scoring,
        risk=risk,
        quadtree=quadtree,
        source_path=resolved,
    )



def _validate_config(config: AppConfig) -> None:
    """运行前拒绝负权重、无限等待与非法网格设置。"""
    for section, fields in (("scoring", vars(config.scoring)), ("risk", vars(config.risk))):
        for name, raw in fields.items():
            values = raw if isinstance(raw, tuple) else (raw,)
            for value in values:
                if not math.isfinite(float(value)) or float(value) < 0:
                    raise ValueError(f"{section}.{name} 必须是有限非负数")
    for name, value in {
        "scoring.senior_speed_mps": config.scoring.senior_speed_mps,
        "runtime.http_timeout_s": config.runtime.http_timeout_s,
        "quadtree.poi_threshold": config.quadtree.poi_threshold,
        "quadtree.min_span_deg": config.quadtree.min_span_deg,
    }.items():
        if not math.isfinite(float(value)) or value <= 0:
            raise ValueError(f"{name} 必须是有限正数")
    for name, value in {
        "area.margin_deg": config.area.margin_deg,
        "runtime.retry_backoff_base_s": config.runtime.retry_backoff_base_s,
        "quadtree.max_depth": config.quadtree.max_depth,
    }.items():
        if not math.isfinite(float(value)) or value < 0:
            raise ValueError(f"{name} 必须是有限非负数")
    if not 0 <= config.runtime.max_retries <= 5:
        raise ValueError("runtime.max_retries 必须在 0—5 之间")
    if not 1 <= config.quadtree.page_size <= 25:
        raise ValueError("quadtree.page_size 必须在 1—25 之间")


def load_config(
    path: Optional[Path] = None, env: Optional[Mapping[str, str]] = None,
) -> AppConfig:
    """统一把配置字段错误转换为含处置建议的 ConfigError。"""
    try:
        config = _load_config(path=path, env=env)
        _validate_config(config)
        return config
    except ConfigError:
        raise
    except (ValueError, TypeError, AttributeError, OverflowError, UnicodeDecodeError) as exc:
        raise ConfigError(
            f"配置字段或类型不合法：{exc}。下一步：参考 config/default.json 修正对应字段后重试。"
        ) from exc


def _as_floats(raw: Any, fallback: Tuple[float, ...]) -> Tuple[float, ...]:
    """把 JSON 数组转成浮点元组，非法则回退默认。"""
    if not isinstance(raw, (list, tuple)) or not raw:
        return fallback
    try:
        values = tuple(float(v) for v in raw)
    except (TypeError, ValueError):
        return fallback
    return values if values else fallback


def bbox_quadrants(bbox: BBox) -> Sequence[BBox]:
    """把矩形四等分为四个子矩形，供 POI 四叉树递归使用。

    顺序固定为：西南、东南、西北、东北，便于日志与测试比对。
    """
    lon_min, lat_min, lon_max, lat_max = bbox
    lon_mid = (lon_min + lon_max) / 2.0
    lat_mid = (lat_min + lat_max) / 2.0
    return (
        (lon_min, lat_min, lon_mid, lat_mid),
        (lon_mid, lat_min, lon_max, lat_mid),
        (lon_min, lat_mid, lon_mid, lat_max),
        (lon_mid, lat_mid, lon_max, lat_max),
    )
