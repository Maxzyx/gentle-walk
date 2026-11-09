"""坐标系统转换：项目内部统一 WGS84。

策略（严格遵循 AGENTS.md「不自写转换公式」）：

1. 数据已是 WGS84 → 原样通过，``method="passthrough"``；
2. GCJ02 / BD09 → 优先调用 ``geotransform`` 的 coordinate 模块做加偏/去偏；
3. ``pyproj`` 只用于「WGS84 ↔ 投影坐标系」的米制换算，**不能**替代 GCJ02 纠偏；
4. 两个库都不可用时**不猜坐标**：原值返回、``status="pending_review"``、给出中文原因，
   由调用方写入「待复核」清单，绝不整体崩溃。

注意：``geotransform`` 的实际函数名以安装后的版本为准，本模块用候选名探测；
探测不到时按「假设/待验证」处理并降级，而不是伪造转换结果。
"""

from __future__ import annotations

import importlib
import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .geo_utils import local_xy_m

# 坐标系统标识
SYSTEM_WGS84 = "wgs84"
SYSTEM_GCJ02 = "gcj02"
SYSTEM_BD09 = "bd09"

STATUS_OK = "ok"
STATUS_PENDING = "pending_review"

#: 常见写法 → 规范标识
_SYSTEM_ALIASES: Dict[str, str] = {
    "wgs84": SYSTEM_WGS84,
    "wgs-84": SYSTEM_WGS84,
    "wgs_84": SYSTEM_WGS84,
    "epsg:4326": SYSTEM_WGS84,
    "4326": SYSTEM_WGS84,
    "gps": SYSTEM_WGS84,
    "gcj02": SYSTEM_GCJ02,
    "gcj-02": SYSTEM_GCJ02,
    "gcj_02": SYSTEM_GCJ02,
    "gcj": SYSTEM_GCJ02,
    "amap": SYSTEM_GCJ02,
    "高德": SYSTEM_GCJ02,
    "火星坐标": SYSTEM_GCJ02,
    "bd09": SYSTEM_BD09,
    "bd-09": SYSTEM_BD09,
    "bd_09": SYSTEM_BD09,
    "baidu": SYSTEM_BD09,
    "百度": SYSTEM_BD09,
}

#: geotransform 中可能的函数名（按优先级尝试，接口以实际版本为准）
_GEOTRANSFORM_CANDIDATES: Mapping[str, Tuple[str, ...]] = {
    SYSTEM_GCJ02: ("gcj02_to_wgs84", "gcj02towgs84", "gcj_to_wgs84", "gcj02_to_wgs"),
    SYSTEM_BD09: ("bd09_to_wgs84", "bd09towgs84", "bd_to_wgs84", "bd09_to_wgs"),
}

#: 纯 Python 替代库：PyPI 包名 ``coordtransform``，导入名是 ``coordTransform``（首字母大写）
_COORDTRANSFORM_IMPORT_NAMES: Tuple[str, ...] = ("coordTransform", "coordtransform")
#: 这些库的函数名与 geotransform 一致，共用候选表
_COORDTRANSFORM_CANDIDATES: Mapping[str, Tuple[str, ...]] = _GEOTRANSFORM_CANDIDATES

Converter = Callable[[float, float], Tuple[float, float]]

#: 后端探测结果缓存（测试可用 ``reset_backend_cache()`` 清空）
_GEOTRANSFORM_CACHE: Dict[str, Any] = {}
_PROJECTION_CACHE: Dict[str, Any] = {}


# --------------------------------------------------------------------------- #
# 结果类型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CoordResult:
    """单点转换结果。``status`` 为 ``pending_review`` 时坐标**未纠偏**。"""

    lon: float
    lat: float
    source_system: str
    status: str
    method: str
    reason: str = ""

    @property
    def ok(self) -> bool:
        """是否已得到可信的 WGS84 坐标。"""
        return self.status == STATUS_OK

    @property
    def needs_review(self) -> bool:
        """是否需要人工复核。"""
        return self.status == STATUS_PENDING


@dataclass(frozen=True)
class ProjectedPoint:
    """局部米制投影结果。"""

    x: float
    y: float
    method: str


# --------------------------------------------------------------------------- #
# 系统标识
# --------------------------------------------------------------------------- #
def normalize_system(raw: Optional[str]) -> Optional[str]:
    """把 ``coord_system`` 字段规范化为 ``wgs84``/``gcj02``/``bd09``；未知返回 None。"""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    return _SYSTEM_ALIASES.get(text)


def system_label(system: Optional[str]) -> str:
    """坐标系统中文名，便于输出。"""
    return {
        SYSTEM_WGS84: "WGS84",
        SYSTEM_GCJ02: "GCJ02（高德/火星坐标）",
        SYSTEM_BD09: "BD09（百度坐标）",
    }.get(system or "", "未知坐标系统")


# --------------------------------------------------------------------------- #
# 后端探测
# --------------------------------------------------------------------------- #
def detect_backends() -> Dict[str, Optional[str]]:
    """探测可用转换后端及其版本；缺失为 None。"""
    result: Dict[str, Optional[str]] = {
        "pyproj": None,
        "geotransform": None,
        "coordtransform": None,
    }
    try:
        import pyproj  # noqa: PLC0415

        result["pyproj"] = str(getattr(pyproj, "__version__", "?"))
    except Exception:  # noqa: BLE001
        pass
    try:
        import geotransform  # noqa: PLC0415

        result["geotransform"] = str(getattr(geotransform, "__version__", "?"))
    except Exception:  # noqa: BLE001
        pass
    for module_name in _COORDTRANSFORM_IMPORT_NAMES:
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001
            continue
        result["coordtransform"] = str(getattr(module, "__version__", "?"))
        break
    return result


def reset_backend_cache() -> None:
    """清空后端探测缓存（测试用）。"""
    _GEOTRANSFORM_CACHE.clear()
    _PROJECTION_CACHE.clear()


def _unwrap_pair(value: Any) -> Tuple[float, float]:
    """把第三方库返回的坐标统一解成 ``(lon, lat)``。"""
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        return (float(value[0]), float(value[1]))
    for lon_name, lat_name in (("lon", "lat"), ("lng", "lat"), ("x", "y")):
        if hasattr(value, lon_name) and hasattr(value, lat_name):
            return (float(getattr(value, lon_name)), float(getattr(value, lat_name)))
    raise TypeError(f"无法解析转换结果类型：{type(value)!r}")


def _wrap_converter(fn: Callable[..., Any]) -> Converter:
    """把第三方转换函数包装成 ``(lon, lat) -> (lon, lat)``。"""

    def _converter(lon: float, lat: float) -> Tuple[float, float]:
        return _unwrap_pair(fn(lon, lat))

    return _converter


def _scan_converters(
    modules: Sequence[Any],
    backend: str,
    found: Dict[str, Converter],
    backends: Dict[str, str],
    candidates: Mapping[str, Tuple[str, ...]],
) -> None:
    """从若干模块里按候选名找转换函数，记录「坐标系统 → 后端名」。"""
    for system, names in candidates.items():
        if system in found:
            continue
        for module in modules:
            if module is None:
                continue
            for name in names:
                fn = getattr(module, name, None)
                if callable(fn):
                    found[system] = _wrap_converter(fn)
                    backends[system] = backend
                    break
            if system in found:
                break


def _discover_geotransform() -> Dict[str, Converter]:
    """探测可用的 GCJ02/BD09 → WGS84 转换函数。

    依次尝试：

    1. ``geotransform`` 的可用坐标转换接口；
    2. ``coordTransform``（PyPI 包名 ``coordtransform``）的纯 Python 转换接口。
    """
    if "converters" in _GEOTRANSFORM_CACHE:
        return _GEOTRANSFORM_CACHE["converters"]

    found: Dict[str, Converter] = {}
    backends: Dict[str, str] = {}

    try:  # 1) geotransform
        import geotransform  # noqa: PLC0415

        coordinate_module = getattr(geotransform, "coordinate", None)
        _scan_converters(
            [coordinate_module, geotransform],
            "geotransform",
            found,
            backends,
            _GEOTRANSFORM_CANDIDATES,
        )
    except Exception:  # noqa: BLE001
        pass

    for module_name in _COORDTRANSFORM_IMPORT_NAMES:  # 2) coordTransform
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001
            continue
        _scan_converters(
            [module], "coordTransform", found, backends, _COORDTRANSFORM_CANDIDATES
        )
        break

    _GEOTRANSFORM_CACHE["converters"] = found
    _GEOTRANSFORM_CACHE["backends"] = backends
    return found


def active_converter_backends() -> Dict[str, str]:
    """当前生效的转换后端：``{"gcj02": "coordTransform", ...}``（无则空表）。"""
    _discover_geotransform()
    return dict(_GEOTRANSFORM_CACHE.get("backends", {}))


# --------------------------------------------------------------------------- #
# 转换
# --------------------------------------------------------------------------- #
def _valid_lonlat(lon: float, lat: float) -> bool:
    """坐标必须有限且在经纬度合法范围内。"""
    try:
        return math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90
    except (TypeError, ValueError):
        return False


def to_wgs84(
    lon: float,
    lat: float,
    coord_system: Optional[str],
    converters: Optional[Mapping[str, Converter]] = None,
) -> CoordResult:
    """把任意已知坐标系统的点转换为 WGS84。

    Args:
        lon: 经度（十进制度）。
        lat: 纬度（十进制度）。
        coord_system: 原始坐标系统字段，允许常见别名。
        converters: 可注入的转换函数表（测试用），默认探测 geotransform。

    Returns:
        CoordResult；无法转换时 ``status="pending_review"`` 且坐标保持原值。
    """
    if not _valid_lonlat(lon, lat):
        return CoordResult(
            lon=lon, lat=lat, source_system=str(coord_system or ""),
            status=STATUS_PENDING, method="none",
            reason="坐标不是合法有限经纬度，已排除。下一步：检查经纬度顺序与数值范围。",
        )
    system = normalize_system(coord_system)
    if system is None:
        return CoordResult(
            lon=lon,
            lat=lat,
            source_system=str(coord_system or ""),
            status=STATUS_PENDING,
            method="none",
            reason=(
                "coord_system 缺失或无法识别，坐标未做任何转换。"
                "下一步：在数据中补写 coord_system（wgs84/gcj02/bd09）后重新入库。"
            ),
        )

    if system == SYSTEM_WGS84:
        return CoordResult(
            lon=lon,
            lat=lat,
            source_system=system,
            status=STATUS_OK,
            method="passthrough",
            reason="",
        )

    table = converters if converters is not None else _discover_geotransform()
    # 注入转换器时沿用历史标签；自动探测时用实际后端名（geotransform / coordTransform）
    backend_label = (
        "geotransform"
        if converters is not None
        else _GEOTRANSFORM_CACHE.get("backends", {}).get(system, "converter")
    )
    converter = table.get(system)
    if converter is not None:
        try:
            new_lon, new_lat = converter(lon, lat)
            if not _valid_lonlat(new_lon, new_lat):
                raise ValueError("转换后坐标不是合法有限经纬度")
        except Exception as exc:  # noqa: BLE001 - 第三方库异常不应拖垮主链路
            return CoordResult(
                lon=lon,
                lat=lat,
                source_system=system,
                status=STATUS_PENDING,
                method=backend_label,
                reason=(
                    f"调用坐标转换后端（{backend_label}）失败：{exc}。坐标未纠偏，"
                    "下一步：检查该库版本与接口后重试，或改用 WGS84 数据。"
                ),
            )
        return CoordResult(
            lon=new_lon,
            lat=new_lat,
            source_system=system,
            status=STATUS_OK,
            method=backend_label,
            reason="",
        )

    return CoordResult(
        lon=lon,
        lat=lat,
        source_system=system,
        status=STATUS_PENDING,
        method="none",
        reason=(
            f"缺少坐标转换后端，无法把 {system_label(system)} 转为 WGS84，"
            "坐标未纠偏（在线数据约有 50—500 米偏移，以现场为准）。"
            "下一步：安装 coordtransform 或可用的 geotransform 坐标转换后端；"
            "pyproj 用于米制投影，GCJ02/BD09 纠偏需要对应后端；"
            "离线样例数据本身为 WGS84，不受影响。"
        ),
    )


def convert_records(
    records: Sequence[Mapping[str, Any]],
    lon_key: str = "lon",
    lat_key: str = "lat",
    system_key: str = "coord_system",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """批量转换，返回（已转换记录, 待复核记录）。

    已转换记录会带上 ``coord_system="wgs84"`` 与 ``coord_method``；
    待复核记录保留原字段并附加 ``review_reason``，由调用方汇总提示。
    """
    converted: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []

    for record in records:
        try:
            lon = float(record[lon_key])
            lat = float(record[lat_key])
        except (KeyError, TypeError, ValueError):
            item = dict(record)
            item["review_reason"] = "经纬度缺失或不是数字，无法转换。下一步：补齐坐标后重新入库。"
            pending.append(item)
            continue

        result = to_wgs84(lon, lat, record.get(system_key))
        item = dict(record)
        item[lon_key] = result.lon
        item[lat_key] = result.lat
        item["coord_method"] = result.method
        if result.ok:
            item["coord_system"] = SYSTEM_WGS84
            converted.append(item)
        else:
            item["coord_system"] = result.source_system
            item["review_reason"] = result.reason
            pending.append(item)

    return converted, pending


# --------------------------------------------------------------------------- #
# 米制投影（pyproj 优先，降级局部近似）
# --------------------------------------------------------------------------- #
def _pyproj_to_utm(lon: float, lat: float, anchor: Optional[Tuple[float, float]] = None) -> Optional[ProjectedPoint]:
    """用 pyproj 投影到 WGS84 / UTM，返回绝对米制坐标；不可用返回 None。"""
    try:
        from pyproj import Transformer  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None

    ref_lon, ref_lat = anchor if anchor is not None else (lon, lat)
    if not _valid_lonlat(lon, lat) or not _valid_lonlat(ref_lon, ref_lat):
        return None
    zone = min(60, max(1, int((ref_lon + 180.0) / 6.0) + 1))
    epsg = (32600 if ref_lat >= 0 else 32700) + zone
    cache_key = f"utm:{epsg}"
    transformer = _PROJECTION_CACHE.get(cache_key)
    if transformer is None:
        try:
            transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
        except Exception:  # noqa: BLE001
            return None
        _PROJECTION_CACHE[cache_key] = transformer
    try:
        x, y = transformer.transform(lon, lat)
    except Exception:  # noqa: BLE001
        return None
    if not math.isfinite(x) or not math.isfinite(y):
        return None
    return ProjectedPoint(x=float(x), y=float(y), method="pyproj-utm")


def project_to_meters(
    lon: float,
    lat: float,
    origin: Optional[Tuple[float, float]] = None,
) -> ProjectedPoint:
    """把点投影为米制坐标。

    ``origin`` 给定时返回相对原点的偏移；未给定时 pyproj 返回绝对 UTM 坐标，
    降级路径返回以 ``origin``（缺省为自身）为原点的局部坐标。
    """
    projected = _pyproj_to_utm(lon, lat, origin)
    if projected is not None:
        if origin is None:
            return projected
        base = _pyproj_to_utm(origin[0], origin[1], origin)
        if base is not None:
            return ProjectedPoint(
                x=projected.x - base.x, y=projected.y - base.y, method="pyproj-utm"
            )

    anchor = origin if origin is not None else (lon, lat)
    x, y = local_xy_m((lon, lat), anchor)
    return ProjectedPoint(x=x, y=y, method="local-equirectangular")


def distance_m(
    a: Tuple[float, float], b: Tuple[float, float], origin: Optional[Tuple[float, float]] = None
) -> float:
    """米制距离：优先 pyproj，缺失时用局部等距近似。"""
    from .geo_utils import haversine_m  # 局部导入避免循环引用

    anchor = origin if origin is not None else a
    projected_a = _pyproj_to_utm(a[0], a[1], anchor)
    projected_b = _pyproj_to_utm(b[0], b[1], anchor)
    if projected_a is not None and projected_b is not None:
        dx = projected_b.x - projected_a.x
        dy = projected_b.y - projected_a.y
        return (dx * dx + dy * dy) ** 0.5
    return haversine_m(a[0], a[1], b[0], b[1])


def backend_summary() -> str:
    """一行中文说明当前坐标转换能力，供 CLI/自检输出。"""
    backends = detect_backends()
    parts = []
    if backends["pyproj"]:
        parts.append(f"pyproj {backends['pyproj']}（米制投影）")
    if backends["geotransform"]:
        parts.append(f"geotransform {backends['geotransform']}（GCJ02/BD09 纠偏）")
    if backends.get("coordtransform"):
        parts.append(f"coordTransform {backends['coordtransform']}（GCJ02/BD09 纠偏）")
    if not parts:
        return (
            "坐标转换：无可用后端（pyproj、geotransform、coordTransform 均缺失）→ "
            "在线 GCJ02 数据不纠偏，标记待复核；离线样例为 WGS84 不受影响"
        )
    return "坐标转换：" + "、".join(parts)
