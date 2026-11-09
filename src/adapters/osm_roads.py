"""从 OpenStreetMap 公开数据抽取片区步行路网（离线可用，不碰高德）。

为什么用 OSM
------------
碑林区步行路网使用公开数据。OSM 的
``highway`` 标签正好覆盖老人步行最关心的信息：

- ``highway=steps``：台阶，可带 ``step_count`` / ``handrail``；
- ``incline=up|down|10%``：坡度，映射为「斜坡」惩罚；
- ``footway=crossing``：人行横道，映射为过街风险；
- ``surface``：路面材质，进入路线风险提示。

数据获取方式（三种，本模块用第一种）
------------------------------------
1. **Overpass API 分块查询**（本模块默认）：按行政区 relation 取 bbox，再切成
   小网格逐块抓 ``way["highway"]`` + 节点坐标；无账号、无 Key、请求量可控。
2. **区域 PBF 抽取**：使用 pyosmium 裁剪区域数据；
   可作为批量处理与离线归档的替代方案，需额外抽取流程。
3. **OSM 官方 API ``/api/0.6/map?bbox=``**：单次上限 0.25 平方度、5 万节点，
   返回 bbox 内的各类要素，使用时需考虑区域与响应大小限制。

结论：Overpass 分块是「快捷 + 可控」的折中；PBF 抽取留作 `--raw` 之外的备选路径。

数据许可：© OpenStreetMap contributors，ODbL 1.0。写出的 CSV 必须保留
``source=OSM`` 标注，README 中注明出处。
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..geo.geo_utils import bbox_contains, haversine_m
from ..models import (
    WALK_TYPE_CROSSING,
    WALK_TYPE_NORMAL,
    WALK_TYPE_SLOPE,
    WALK_TYPE_STAIRS,
)

#: Overpass 镜像候选，按配置顺序尝试；可达性取决于运行环境。
OVERPASS_ENDPOINTS: Tuple[str, ...] = (
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

#: HTTP 头必须是 latin-1，不能写中文
USER_AGENT = "gentle-walk-mvp/0.1 (OSM road network extractor, local demo)"
DEFAULT_DISTRICT = "碑林区"
#: 单块网格边长（度）；0.02 度约 2 公里，碑林区约 3×2 块
DEFAULT_TILE_DEG = 0.02
#: 单次 Overpass 查询超时（秒）
DEFAULT_TIMEOUT_S = 120.0

#: 可步行道路类型（其余一律排除，避免把老人引到机动车道）
WALKABLE_HIGHWAYS: Tuple[str, ...] = (
    "footway",
    "path",
    "pedestrian",
    "steps",
    "living_street",
    "residential",
    "service",
    "unclassified",
    "track",
    "cycleway",
    "corridor",
    "elevator",
    "platform",
)
#: 机动车道：只有带 sidewalk / footway 标注时才保留（人行道）
MOTOR_HIGHWAYS: Tuple[str, ...] = ("primary", "secondary", "tertiary", "trunk", "motorway")
#: 明确排除的道路类型
EXCLUDED_HIGHWAYS: Tuple[str, ...] = (
    "motorway",
    "trunk",
    "motorway_link",
    "trunk_link",
    "construction",
    "proposed",
    "abandoned",
    "raceway",
    "bus_guideway",
)
#: 明确禁止通行的 access 值
DENY_ACCESS: Tuple[str, ...] = ("private", "no", "customers", "delivery")
#: 默认坡度（百分比）：incline 只写 up/down 时的保守估计
DEFAULT_SLOPE_PCT = 6.0


class OsmUnavailable(RuntimeError):
    """OSM 数据不可用（网络、镜像、行政区找不到等），调用方应降级。"""


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class OsmNetwork:
    """一次抽取的原始结果（未裁剪、未分类）。"""

    district: str
    relation_id: int
    bbox: Tuple[float, float, float, float]  # (lon_min, lat_min, lon_max, lat_max)
    nodes: Dict[int, Tuple[float, float]] = field(default_factory=dict)
    ways: Dict[int, Tuple[Dict[str, str], List[int]]] = field(default_factory=dict)
    rings: List[List[Tuple[float, float]]] = field(default_factory=list)
    holes: List[List[Tuple[float, float]]] = field(default_factory=list)
    requests: int = 0
    tiles: int = 0

    @property
    def way_count(self) -> int:
        """道路条数。"""
        return len(self.ways)

    @property
    def node_count(self) -> int:
        """节点数。"""
        return len(self.nodes)


@dataclass
class NetworkStats:
    """裁剪与分类后的统计，用于 CLI 报告。"""

    ways_total: int = 0
    ways_kept: int = 0
    ways_outside: int = 0
    ways_motor_no_sidewalk: int = 0
    ways_denied: int = 0
    ways_excluded: int = 0
    ways_short: int = 0
    ways_motor_unknown_sidewalk: int = 0
    edges_outside: int = 0
    edges: int = 0
    nodes: int = 0
    length_km: float = 0.0
    stairs_edges: int = 0
    slope_edges: int = 0
    crossing_edges: int = 0
    stairs_with_count: int = 0
    stairs_without_count: int = 0

    def summary_lines(self) -> List[str]:
        """中文统计摘要。"""
        return [
            f"道路 {self.ways_total} 条，保留 {self.ways_kept} 条，"
            f"生成 {self.edges} 段、{self.nodes} 个节点，总长约 {self.length_km:.2f} 公里",
            f"台阶段 {self.stairs_edges}（其中 {self.stairs_with_count} 段有 step_count，"
            f"{self.stairs_without_count} 段按默认惩罚）",
            f"斜坡段 {self.slope_edges}，过街段 {self.crossing_edges}",
            f"排除：边界外 {self.ways_outside}、机动车道无 sidewalk {self.ways_motor_no_sidewalk}、"
            f"禁止通行 {self.ways_denied}、类型排除 {self.ways_excluded}、过短 {self.ways_short}",
            f"输出时丢弃不符合片区裁剪条件的路段 {self.edges_outside} 段",
            f"保留的机动车道（人行道未标注）{self.ways_motor_unknown_sidewalk} 条——"
            "这些路段无法确认人行道，路线中统一提示「以现场为准」",
        ]


# --------------------------------------------------------------------------- #
# Overpass 请求
# --------------------------------------------------------------------------- #
#: Overpass 在查询超时/部分失败时会把错误写进 ``remark``，同时仍可能返回部分要素；
#: 出现这些标记说明结果不完整，不能当作成功
_FATAL_REMARK_MARKERS: Tuple[str, ...] = (
    "runtime error",
    "timed out",
    "timeout",
    "out of memory",
    "load of",
)


def _overpass_post(
    endpoint: str,
    query: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    retries: int = 2,
    sleeper: Any = None,
    opener: Any = None,
) -> Dict[str, Any]:
    """POST 一条 Overpass QL，返回解析后的 JSON。

    Args:
        sleeper: 可选注入的 sleep（测试用）。
        opener: 可选注入的打开函数（默认 ``urllib.request.urlopen``，测试用）。
    """
    sleep = sleeper or time.sleep
    open_url = opener or urllib.request.urlopen
    payload = urllib.parse.urlencode({"data": query}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        method="POST",
    )
    last_error = ""
    for attempt in range(retries + 1):
        try:
            with open_url(request, timeout=timeout_s) as response:
                raw = response.read()
            data = json.loads(raw.decode("utf-8"))
            remark = str(data.get("remark") or "")
            if remark:
                lowered = remark.lower()
                partial = not data.get("elements")
                fatal = any(marker in lowered for marker in _FATAL_REMARK_MARKERS)
                if partial or fatal:
                    # 部分结果不能当作成功：宁可报错重试，也不生成残缺路网
                    last_error = f"Overpass 返回不完整结果：{remark}"
                    if attempt < retries:
                        sleep(1.5 * (attempt + 1))
                        continue
                    raise OsmUnavailable(
                        f"Overpass 查询失败（{endpoint}）：{last_error}。"
                        "下一步：换一个镜像（--endpoint），或稍后重试；"
                        "已有本地原始 JSON 时可用 --raw 离线重建。"
                    )
            return data
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            last_error = f"网络错误：{exc.reason}"
        except (TimeoutError, OSError) as exc:
            last_error = f"连接失败：{exc}"
        except json.JSONDecodeError as exc:
            last_error = f"响应不是合法 JSON（可能被截断）：{exc}"
        if attempt < retries:
            sleep(1.5 * (attempt + 1))
    raise OsmUnavailable(
        f"Overpass 查询失败（{endpoint}）：{last_error}。"
        "下一步：换一个镜像（--endpoint），或检查网络后重试；"
        "已有本地原始 JSON 时可用 --raw 离线重建。"
    )


# --------------------------------------------------------------------------- #
# 行政区边界
# --------------------------------------------------------------------------- #
def find_district(district: str, endpoint: str) -> Tuple[int, Dict[str, str]]:
    """按名称查行政区 relation，返回 (id, tags)。"""
    safe_name = district.replace('"', "")
    query = (
        '[out:json][timeout:60];'
        f'rel["name"="{safe_name}"]["boundary"="administrative"];'
        "out ids tags 5;"
    )
    data = _overpass_post(endpoint, query)
    for element in data.get("elements", []):
        if element.get("type") == "relation":
            return int(element["id"]), dict(element.get("tags", {}))
    raise OsmUnavailable(
        f"OSM 里找不到名为「{district}」的行政区边界。"
        "下一步：用 --district 换名称（如「碑林区」），或先用 Nominatim 查标准名称。"
    )


def district_bounds(relation_id: int, endpoint: str) -> Tuple[float, float, float, float]:
    """取 relation 的 bbox，返回 (lon_min, lat_min, lon_max, lat_max)。"""
    data = _overpass_post(endpoint, f"[out:json][timeout:60];rel({relation_id});out bb;")
    for element in data.get("elements", []):
        bounds = element.get("bounds")
        if bounds:
            return (
                float(bounds["minlon"]),
                float(bounds["minlat"]),
                float(bounds["maxlon"]),
                float(bounds["maxlat"]),
            )
    raise OsmUnavailable(
        f"relation {relation_id} 没有返回 bounds。"
        "下一步：确认该 relation 是边界类型（boundary=administrative）。"
    )


def district_polygon(relation_id: int, endpoint: str) -> Tuple[List[List[Tuple[float, float]]], List[List[Tuple[float, float]]]]:
    """取边界几何并拼成闭合环，返回 (外环列表, 洞/飞地列表)。"""
    data = _overpass_post(endpoint, f"[out:json][timeout:90];rel({relation_id});out geom;")
    relation = next((e for e in data.get("elements", []) if e.get("type") == "relation"), None)
    if relation is None:
        raise OsmUnavailable(
            "边界 relation 没有返回几何（out geom 为空）。"
            "下一步：换镜像重试，或用 --no-polygon 只按 bbox 裁剪。"
        )
    outer_segments: List[List[Tuple[float, float]]] = []
    hole_segments: List[List[Tuple[float, float]]] = []
    for member in relation.get("members", []):
        geometry = member.get("geometry") or []
        if len(geometry) < 2:
            continue
        points = [(float(p["lon"]), float(p["lat"])) for p in geometry if "lon" in p and "lat" in p]
        if len(points) < 2:
            continue
        role = member.get("role") or "outer"
        (hole_segments if role == "subarea" else outer_segments).append(points)
    return _stitch_rings(outer_segments), _stitch_rings(hole_segments)


def _stitch_rings(segments: Sequence[Sequence[Tuple[float, float]]]) -> List[List[Tuple[float, float]]]:
    """把边界碎片按端点拼成闭合环；拼不上的碎片丢弃。"""
    remaining = [[_round_point(p) for p in segment] for segment in segments if len(segment) >= 2]
    rings: List[List[Tuple[float, float]]] = []
    while remaining:
        ring = remaining.pop(0)
        changed = True
        while changed:
            changed = False
            for index, segment in enumerate(remaining):
                if segment[0] == ring[-1]:
                    ring.extend(segment[1:])
                elif segment[-1] == ring[-1]:
                    ring.extend(reversed(segment[:-1]))
                elif segment[-1] == ring[0]:
                    ring[0:0] = segment[:-1]
                elif segment[0] == ring[0]:
                    ring[0:0] = list(reversed(segment[1:]))
                else:
                    continue
                remaining.pop(index)
                changed = True
                break
        if len(ring) >= 4 and ring[0] == ring[-1]:
            rings.append(ring)
    return rings


def _round_point(point: Tuple[float, float]) -> Tuple[float, float]:
    """坐标四舍五入到 1e-7 度（约 1 厘米），用于拼接与点在多边形内判断。"""
    return (round(float(point[0]), 7), round(float(point[1]), 7))


def point_in_rings(
    lon: float,
    lat: float,
    rings: Sequence[Sequence[Tuple[float, float]]],
    holes: Sequence[Sequence[Tuple[float, float]]] = (),
) -> bool:
    """点是否落在任一外环内、且不在任何洞内（射线法）。"""
    if not rings:
        return True
    inside = any(_point_in_ring(lon, lat, ring) for ring in rings)
    if not inside:
        return False
    return not any(_point_in_ring(lon, lat, hole) for hole in holes)


def _point_in_ring(lon: float, lat: float, ring: Sequence[Tuple[float, float]]) -> bool:
    """射线法判断点是否在单个环内。"""
    inside = False
    count = len(ring)
    j = count - 1
    for i in range(count):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


# --------------------------------------------------------------------------- #
# 路网抓取
# --------------------------------------------------------------------------- #
def _tiles(bbox: Tuple[float, float, float, float], tile_deg: float) -> List[Tuple[float, float, float, float]]:
    """把 bbox 切成若干小格，返回 (lon_min, lat_min, lon_max, lat_max) 列表。"""
    lon_min, lat_min, lon_max, lat_max = bbox
    cols = max(1, math.ceil((lon_max - lon_min) / tile_deg))
    rows = max(1, math.ceil((lat_max - lat_min) / tile_deg))
    dx = (lon_max - lon_min) / cols
    dy = (lat_max - lat_min) / rows
    tiles: List[Tuple[float, float, float, float]] = []
    for row in range(rows):
        for col in range(cols):
            tiles.append(
                (
                    lon_min + col * dx,
                    lat_min + row * dy,
                    lon_min + (col + 1) * dx,
                    lat_min + (row + 1) * dy,
                )
            )
    return tiles


def _fetch_tile(
    endpoint: str,
    bbox: Tuple[float, float, float, float],
    timeout_s: float,
    retries: int,
) -> Dict[str, Any]:
    """抓取单块网格内的道路（含节点坐标）。"""
    lon_min, lat_min, lon_max, lat_max = bbox
    query = (
        f"[out:json][timeout:{int(timeout_s)}];"
        f'way["highway"]({lat_min:.7f},{lon_min:.7f},{lat_max:.7f},{lon_max:.7f})->.w;'
        "(.w;.w>;);out body qt;"
    )
    return _overpass_post(endpoint, query, timeout_s=timeout_s, retries=retries)


def fetch_district_network(
    district: str = DEFAULT_DISTRICT,
    endpoint: Optional[str] = None,
    endpoints: Sequence[str] = OVERPASS_ENDPOINTS,
    tile_deg: float = DEFAULT_TILE_DEG,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    retries: int = 2,
    with_polygon: bool = True,
    progress: Optional[Any] = None,
) -> OsmNetwork:
    """抓取一个行政区的步行路网原始数据。

    Args:
        district: 行政区名称，例如「碑林区」。
        endpoint: 指定镜像；为 None 时自动探测 ``endpoints``。
        tile_deg: 网格边长（度）。
        timeout_s: 单次查询超时。
        with_polygon: 是否抓取边界几何（用于精确裁剪）。
        progress: 可选回调 ``progress(已完成块数, 总块数)``。

    Returns:
        OsmNetwork。
    """
    # 逐个镜像尝试：能连通但没有该地区数据的镜像（如只镜像瑞士的节点）要跳过，
    # 不能因为第一个镜像「能连上」就直接失败
    candidates = [endpoint] if endpoint else list(endpoints)
    errors: List[str] = []
    chosen = ""
    relation_id = 0
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    rings: List[List[Tuple[float, float]]] = []
    holes: List[List[Tuple[float, float]]] = []
    for candidate in candidates:
        try:
            candidate_id, _tags = find_district(district, candidate)
            candidate_bbox = district_bounds(candidate_id, candidate)
            candidate_rings: List[List[Tuple[float, float]]] = []
            candidate_holes: List[List[Tuple[float, float]]] = []
            if with_polygon:
                candidate_rings, candidate_holes = district_polygon(candidate_id, candidate)
        except OsmUnavailable as exc:
            errors.append(f"{candidate}：{exc}")
            continue
        chosen, relation_id, bbox = candidate, candidate_id, candidate_bbox
        rings, holes = candidate_rings, candidate_holes
        break
    if not chosen:
        raise OsmUnavailable(
            f"所有 Overpass 镜像都没查到「{district}」的行政区数据："
            + "；".join(errors)
            + "。下一步：换一个镜像（--endpoint），或用 --raw 指定已有的原始 JSON 离线重建。"
        )

    network = OsmNetwork(district=district, relation_id=relation_id, bbox=bbox)
    network.rings, network.holes = rings, holes

    tiles = _tiles(bbox, tile_deg)
    network.tiles = len(tiles)
    for index, tile in enumerate(tiles, start=1):
        data = _fetch_tile(chosen, tile, timeout_s, retries)
        network.requests += 1
        for element in data.get("elements", []):
            if element.get("type") == "node":
                network.nodes[int(element["id"])] = (
                    float(element["lon"]),
                    float(element["lat"]),
                )
            elif element.get("type") == "way":
                tags = {str(k): str(v) for k, v in element.get("tags", {}).items()}
                refs = [int(ref) for ref in element.get("nodes", [])]
                if len(refs) >= 2:
                    network.ways[int(element["id"])] = (tags, refs)
        if progress is not None:
            progress(index, len(tiles))
    return network


# --------------------------------------------------------------------------- #
# 分类与建图
# --------------------------------------------------------------------------- #
def _parse_step_count(raw: Optional[str]) -> Optional[int]:
    """解析 step_count，允许 "12"、"12级" 这类写法。"""
    if not raw:
        return None
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if not digits:
        return None
    value = int(digits)
    return value if 0 < value <= 500 else None


def _parse_incline(raw: Optional[str]) -> Optional[float]:
    """解析 incline，返回坡度百分比；只写 up/down 时用保守默认值。"""
    if not raw:
        return None
    text = str(raw).strip().lower()
    if text in ("up", "yes", "steep"):
        return DEFAULT_SLOPE_PCT
    if text in ("down", "steep_down"):
        return DEFAULT_SLOPE_PCT
    try:
        value = float(text.rstrip("%"))
    except ValueError:
        return None
    if not math.isfinite(value) or value == 0:
        return None
    return min(abs(value), 45.0)


def _is_crossing(tags: Mapping[str, str]) -> bool:
    """是否人行横道。"""
    if tags.get("highway") == "crossing":
        return True
    if tags.get("footway") == "crossing":
        return True
    if tags.get("path") == "crossing":
        return True
    return tags.get("crossing") in ("marked", "unmarked", "traffic_signals", "zebra")


def _has_sidewalk(tags: Mapping[str, str]) -> bool:
    """只认可明确肯定的人行道信息，不能把字符串 no 当成 True。"""
    sidewalk = str(tags.get("sidewalk") or "").strip().lower()
    if sidewalk in ("both", "left", "right", "yes", "separate"):
        return True
    for name in ("sidewalk:both", "sidewalk:left", "sidewalk:right"):
        if str(tags.get(name) or "").strip().lower() in ("yes", "separate"):
            return True
    if str(tags.get("footway") or "").strip().lower() in ("sidewalk", "crossing"):
        return True
    return str(tags.get("pedestrian") or "").strip().lower() == "yes"


def classify_way(
    tags: Mapping[str, str],
    allow_motor_without_sidewalk: bool = False,
) -> Tuple[int, Optional[int], Optional[float], str, str]:
    """把 OSM 标签映射为项目字段。

    Returns:
        ``(walk_type, step_count, slope_pct, surface, skip_reason)``；
        ``skip_reason`` 非空表示该路段应排除。
    """
    highway = str(tags.get("highway") or "").strip().lower()
    if not highway:
        return WALK_TYPE_NORMAL, None, None, "", "缺少 highway 标签"
    if highway in EXCLUDED_HIGHWAYS:
        return WALK_TYPE_NORMAL, None, None, "", f"类型排除（{highway}）"
    foot = str(tags.get("foot") or "").strip().lower()
    if foot in DENY_ACCESS or foot == "use_sidepath":
        return WALK_TYPE_NORMAL, None, None, "", "foot 禁止在本道路步行"
    if str(tags.get("access") or "").strip().lower() in DENY_ACCESS:
        return WALK_TYPE_NORMAL, None, None, "", "access 禁止通行"
    if highway in MOTOR_HIGHWAYS and not _has_sidewalk(tags):
        # 默认排除（AGENTS.md：不把步行用户引导到机动车道）；
        # 但中文 OSM 普遍不标 sidewalk，全排除会让老城区路网碎成孤岛，
        # 因此提供开关：保留但统计为「人行道未标注」，由路线风险提示兜底。
        if not allow_motor_without_sidewalk:
            return WALK_TYPE_NORMAL, None, None, "", "机动车道且无 sidewalk 标注"
    if highway not in WALKABLE_HIGHWAYS and highway not in MOTOR_HIGHWAYS:
        return WALK_TYPE_NORMAL, None, None, "", f"非步行道路（{highway}）"

    surface = (tags.get("surface") or "").strip()
    incline = _parse_incline(tags.get("incline"))

    if highway == "steps":
        step_count = _parse_step_count(tags.get("step_count"))
        return WALK_TYPE_STAIRS, step_count, incline, surface, ""
    if _is_crossing(tags):
        return WALK_TYPE_CROSSING, None, None, surface, ""
    if incline is not None or tags.get("ramp") in ("yes", "1", "true"):
        return WALK_TYPE_SLOPE, None, incline or DEFAULT_SLOPE_PCT, surface, ""
    return WALK_TYPE_NORMAL, None, None, surface, ""


def _way_touches_polygon(
    refs: Sequence[int],
    nodes: Mapping[int, Tuple[float, float]],
    rings: Sequence[Sequence[Tuple[float, float]]],
    holes: Sequence[Sequence[Tuple[float, float]]],
) -> bool:
    """道路是否与片区有交集（按段判断，避免整条路被边界切掉造成孤岛）。"""
    points = [nodes[ref] for ref in refs if ref in nodes]
    for index in range(len(points) - 1):
        lon1, lat1 = points[index]
        lon2, lat2 = points[index + 1]
        if point_in_rings(lon1, lat1, rings, holes):
            return True
        if point_in_rings(lon2, lat2, rings, holes):
            return True
        if point_in_rings((lon1 + lon2) / 2.0, (lat1 + lat2) / 2.0, rings, holes):
            return True
    return False


def _way_midpoint(refs: Sequence[int], nodes: Mapping[int, Tuple[float, float]]) -> Optional[Tuple[float, float]]:
    """取道路中点坐标（用于判断是否落在片区内）。"""
    points = [nodes[ref] for ref in refs if ref in nodes]
    if not points:
        return None
    middle = points[len(points) // 2]
    return middle


def build_network(
    network: OsmNetwork,
    use_polygon: bool = True,
    allow_motor_without_sidewalk: bool = False,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], NetworkStats]:
    """把原始 OSM 数据裁剪、分类、去重成项目节点/边表。

    Returns:
        ``(node_rows, edge_rows, stats)``；行字段与 ``data/graph_nodes.csv``、
        ``data/graph_edges.csv`` 的表头一致。
    """
    stats = NetworkStats(ways_total=network.way_count)
    rings = network.rings if use_polygon else []
    holes = network.holes if use_polygon else []
    if use_polygon and not rings:
        # 拼不出闭合环时若继续按 bbox 输出，会把邻区道路也算作碑林区，必须报错
        raise OsmUnavailable(
            f"边界多边形不可用（«{network.district}» 的边界碎片没有拼出闭合环），"
            "无法按片区裁剪路网。下一步：换一个镜像（--endpoint）重试，"
            "或显式加 --no-polygon 只按 bbox 生成（并接受邻区道路混入）。"
        )

    kept: List[Tuple[int, List[int], int, Optional[int], Optional[float], str, bool]] = []
    for way_id, (tags, refs) in network.ways.items():
        walk_type, step_count, slope_pct, surface, skip_reason = classify_way(
            tags, allow_motor_without_sidewalk=allow_motor_without_sidewalk
        )
        if skip_reason:
            if "边界" in skip_reason:
                stats.ways_outside += 1
            elif "机动车道" in skip_reason:
                stats.ways_motor_no_sidewalk += 1
            elif "access" in skip_reason or "foot" in skip_reason:
                stats.ways_denied += 1
            else:
                stats.ways_excluded += 1
            continue
        if len(refs) < 2:
            stats.ways_short += 1
            continue
        if rings and not _way_touches_polygon(refs, network.nodes, rings, holes):
            stats.ways_outside += 1
            continue
        highway = str(tags.get("highway") or "").strip().lower()
        pedestrian_uncertain = highway in MOTOR_HIGHWAYS and not _has_sidewalk(tags)
        if pedestrian_uncertain:
            stats.ways_motor_unknown_sidewalk += 1
        kept.append((way_id, refs, walk_type, step_count, slope_pct, surface, pedestrian_uncertain))

    used_nodes: Dict[int, Tuple[float, float]] = {}
    edges: List[Dict[str, Any]] = []
    seen_pairs: set = set()
    for way_id, refs, walk_type, step_count, slope_pct, surface, pedestrian_uncertain in kept:
        touched = False
        # 必须保留原始相邻关系；缺失节点不能被删除后连接两侧。
        for u, v in zip(refs, refs[1:]):
            if u == v or u not in network.nodes or v not in network.nodes:
                continue
            lon1, lat1 = network.nodes[u]
            lon2, lat2 = network.nodes[v]
            if not rings and not (
                bbox_contains(network.bbox, (lon1, lat1))
                and bbox_contains(network.bbox, (lon2, lat2))
            ):
                # bbox 降级只保留两端均在框内的原始段，不制造裁剪节点。
                stats.edges_outside += 1
                continue
            if rings and not (
                point_in_rings(lon1, lat1, rings, holes)
                or point_in_rings(lon2, lat2, rings, holes)
            ):
                # 跨边界的一段保留（一端在内），两端都在区外的路段丢弃
                stats.edges_outside += 1
                continue
            touched = True
            length_m = haversine_m(lon1, lat1, lon2, lat2)
            if length_m < 0.5:
                continue
            pair = (min(u, v), max(u, v))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            used_nodes[u] = (lon1, lat1)
            used_nodes[v] = (lon2, lat2)
            edges.append(
                {
                    "u": f"osm_{u}",
                    "v": f"osm_{v}",
                    "length_m": round(length_m, 1),
                    "walk_type": walk_type,
                    "step_count": "" if step_count is None else step_count,
                    "slope_pct": "" if slope_pct is None else round(slope_pct, 1),
                    "surface": surface,
                    "source": "OSM",
                    "pedestrian_uncertain": int(pedestrian_uncertain),
                }
            )
            stats.edges += 1
            stats.length_km += length_m / 1000.0
            if walk_type == WALK_TYPE_STAIRS:
                stats.stairs_edges += 1
                if step_count is None:
                    stats.stairs_without_count += 1
                else:
                    stats.stairs_with_count += 1
            elif walk_type == WALK_TYPE_SLOPE:
                stats.slope_edges += 1
            elif walk_type == WALK_TYPE_CROSSING:
                stats.crossing_edges += 1
        if touched:
            stats.ways_kept += 1
        else:
            stats.ways_outside += 1

    node_rows = [
        {
            "node_id": f"osm_{node_id}",
            "lon": round(lon, 7),
            "lat": round(lat, 7),
            "coord_system": "wgs84",
            "source": "OSM",
        }
        for node_id, (lon, lat) in used_nodes.items()
    ]
    stats.nodes = len(node_rows)
    return node_rows, edges, stats


# --------------------------------------------------------------------------- #
# 写盘与 CLI
# --------------------------------------------------------------------------- #
NODE_FIELDS: Tuple[str, ...] = ("node_id", "lon", "lat", "coord_system", "source")
EDGE_FIELDS: Tuple[str, ...] = (
    "u",
    "v",
    "length_m",
    "walk_type",
    "step_count",
    "slope_pct",
    "surface",
    "source",
    "pedestrian_uncertain",
)


def write_network(
    node_rows: Sequence[Mapping[str, Any]],
    edge_rows: Sequence[Mapping[str, Any]],
    out_dir: Path,
) -> Tuple[Path, Path]:
    """写出 ``graph_nodes.csv`` 与 ``graph_edges.csv``（utf-8，表头与样例一致）。"""
    import csv

    out_dir.mkdir(parents=True, exist_ok=True)
    nodes_path = out_dir / "graph_nodes.csv"
    edges_path = out_dir / "graph_edges.csv"
    with nodes_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(NODE_FIELDS))
        writer.writeheader()
        writer.writerows(node_rows)
    with edges_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(EDGE_FIELDS))
        writer.writeheader()
        writer.writerows(edge_rows)
    return nodes_path, edges_path


def save_raw(network: OsmNetwork, path: Path) -> Path:
    """把原始抓取结果存成 JSON（缓存/复现用）。"""
    payload = {
        "district": network.district,
        "relation_id": network.relation_id,
        "bbox": list(network.bbox),
        "rings": [[list(point) for point in ring] for ring in network.rings],
        "holes": [[list(point) for point in hole] for hole in network.holes],
        "nodes": {str(key): list(value) for key, value in network.nodes.items()},
        "ways": {
            str(key): {"tags": tags, "nodes": refs}
            for key, (tags, refs) in network.ways.items()
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def load_raw(path: Path) -> OsmNetwork:
    """从 ``save_raw`` 的 JSON 重建 OsmNetwork（离线重建，不再打网络）。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    network = OsmNetwork(
        district=str(payload.get("district", "")),
        relation_id=int(payload.get("relation_id", 0)),
        bbox=tuple(float(value) for value in payload["bbox"]),  # type: ignore[arg-type]
    )
    network.nodes = {
        int(key): (float(value[0]), float(value[1]))
        for key, value in payload.get("nodes", {}).items()
    }
    network.ways = {
        int(key): (
            {str(k): str(v) for k, v in item.get("tags", {}).items()},
            [int(ref) for ref in item.get("nodes", [])],
        )
        for key, item in payload.get("ways", {}).items()
    }
    network.rings = [
        [(float(p[0]), float(p[1])) for p in ring] for ring in payload.get("rings", [])
    ]
    network.holes = [
        [(float(p[0]), float(p[1])) for p in hole] for hole in payload.get("holes", [])
    ]
    return network


def _slug(district: str) -> str:
    """把中文区名转成可用的文件名（避免依赖拼音库，用哈希后缀保证唯一）。"""
    import hashlib

    digest = hashlib.sha1(district.encode("utf-8")).hexdigest()[:6]
    return f"osm_network_{digest}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口：抓取行政区路网并写成项目 CSV。"""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m src.adapters.osm_roads",
        description="从 OpenStreetMap 抽取片区步行路网，输出 graph_nodes.csv / graph_edges.csv",
    )
    parser.add_argument("--district", default=DEFAULT_DISTRICT, help="行政区名称，默认碑林区")
    parser.add_argument("--out", default="data/osm_beilin", help="输出目录")
    parser.add_argument("--cache", default="cache/osm", help="原始 JSON 缓存目录")
    parser.add_argument("--endpoint", default=None, help="指定 Overpass 镜像")
    parser.add_argument("--tile-deg", type=float, default=DEFAULT_TILE_DEG, help="网格边长（度）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="单次查询超时（秒）")
    parser.add_argument("--raw", default=None, help="用已有原始 JSON 离线重建（不再联网）")
    parser.add_argument("--no-polygon", action="store_true", help="只按 bbox 裁剪，不抓边界多边形")
    parser.add_argument(
        "--include-motor-roads",
        action="store_true",
        help="保留未标注 sidewalk 的机动车道（中文 OSM 常见，用于保持路网连通；路线会提示以现场为准）",
    )
    parser.add_argument("--save-raw", action="store_true", help="把原始抓取结果写入缓存目录")
    args = parser.parse_args(argv)

    cache_path = Path(args.cache)
    try:
        if args.raw:
            network = load_raw(Path(args.raw))
            print(f"已从本地 JSON 读取：{args.raw}（{network.way_count} 条道路）")
        else:
            def progress(done: int, total: int) -> None:
                print(f"  抓取进度 {done}/{total} 块", flush=True)

            network = fetch_district_network(
                district=args.district,
                endpoint=args.endpoint,
                tile_deg=args.tile_deg,
                timeout_s=args.timeout,
                with_polygon=not args.no_polygon,
                progress=progress,
            )
            print(
                f"抓取完成：{network.district}（relation {network.relation_id}），"
                f"{network.way_count} 条道路、{network.node_count} 个节点、{network.requests} 次请求"
            )
            if args.save_raw:
                raw_path = save_raw(network, cache_path / f"{_slug(args.district)}_raw.json")
                print(f"原始数据已缓存：{raw_path}")
    except OsmUnavailable as exc:
        print(f"OSM 路网不可用：{exc}")
        return 1

    try:
        node_rows, edge_rows, stats = build_network(
            network,
            use_polygon=not args.no_polygon,
            allow_motor_without_sidewalk=args.include_motor_roads,
        )
    except OsmUnavailable as exc:
        print(f"OSM 路网裁剪失败：{exc}")
        return 1
    for line in stats.summary_lines():
        print(f"· {line}")

    nodes_path, edges_path = write_network(node_rows, edge_rows, Path(args.out))
    print(f"已写出：{nodes_path}（{len(node_rows)} 行）、{edges_path}（{len(edge_rows)} 行）")
    print(
        "数据来源：© OpenStreetMap contributors，ODbL 1.0；"
        "下一步：把配置的 data_dir 指向该目录后运行 python -m src.cli 验证路线。"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
