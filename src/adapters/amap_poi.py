"""高德 POI 关键字搜索 + 四叉树递归拆分。

AGENTS.md「POI 搜索与四叉树拆分」：
- 接口 ``GET https://restapi.amap.com/v3/place/text``；
- 同一请求参数翻页最多约 200 条，单一关键词翻页无法覆盖全量；
- 当矩形区域结果数量接近阈值（**建议 180 条**）时四分递归，直到低于阈值；
- 终止条件：结果数低于阈值、网格跨度小于约 **0.005 度**、递归深度超限。

实现限制：
- ``place/text`` 的矩形参数不能作为可靠的区域过滤依据，四叉树不能保证全量覆盖；
- 区级 POI 导入使用独立适配器的行政区检索和边界裁剪流程；
- 递归查询设置预算上限。区域过滤失效或预算耗尽时停止拆分，标记结果可能不完整。
- ``place/polygon`` 可作为进一步评估的区域检索方案，参数与支持范围需按服务文档验证。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..config import QuadtreeConfig, bbox_quadrants
from ..geo.geo_utils import bbox_span_deg
from .amap_client import AmapClient, AmapResponse, AmapUnavailable

#: 关键字搜索路径
TEXT_SEARCH_PATH = "/v3/place/text"
#: 单页返回条数上限（高德 offset 上限 25）
PAGE_SIZE = 25
#: 单个叶子最多翻页数（25 × 8 = 200 条，对应接口上限）
MAX_PAGES = 8
#: 单次查询预算：区域过滤失效时限制递归请求成本。
MAX_REQUESTS = 60
#: 默认城市（片区在西安城墙内）
DEFAULT_CITY = "西安"

BBox = Tuple[float, float, float, float]


@dataclass
class QuadtreeStats:
    """四叉树运行统计，便于自检与日志。"""

    requests: int = 0
    leaves: int = 0
    splits: int = 0
    max_depth: int = 0
    truncated_leaves: int = 0
    budget_exhausted: bool = False
    per_keyword: Dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        """一行中文摘要。"""
        parts = [
            f"请求 {self.requests} 次、叶子 {self.leaves} 个、拆分 {self.splits} 次、"
            f"最大深度 {self.max_depth}"
        ]
        if self.truncated_leaves:
            parts.append(f"触及终止条件仍密集的叶子 {self.truncated_leaves} 个")
        if self.budget_exhausted:
            parts.append(f"请求预算（{MAX_REQUESTS} 次）已用尽，结果可能不完整")
        return "、".join(parts)


def _polygon_param(bbox: BBox) -> str:
    """把 bbox 转成高德 ``polygon`` 参数值。"""
    lon_min, lat_min, lon_max, lat_max = bbox
    return f"{lon_min:.6f},{lat_min:.6f}|{lon_max:.6f},{lat_max:.6f}"


def _should_split(
    count: int, bbox: BBox, depth: int, quadtree: QuadtreeConfig
) -> bool:
    """是否继续四分：结果数达阈值且未触及跨度/深度终止条件。"""
    if count < quadtree.poi_threshold:
        return False
    if depth >= quadtree.max_depth:
        return False
    lon_span, lat_span = bbox_span_deg(bbox)
    return max(lon_span, lat_span) >= quadtree.min_span_deg


def _fetch_pages(
    client: AmapClient,
    keyword: str,
    bbox: BBox,
    quadtree: QuadtreeConfig,
    city: str,
    stats: QuadtreeStats,
) -> List[Dict[str, Any]]:
    """对单个关键词在 bbox 内翻页，返回原始 POI 列表。"""
    collected: List[Dict[str, Any]] = []
    page_size = int(quadtree.page_size or PAGE_SIZE)
    max_pages = min(MAX_PAGES, max(1, 200 // max(page_size, 1)))

    for page in range(1, max_pages + 1):
        if stats.requests >= MAX_REQUESTS:
            # 预算用尽：停止翻页，保留已取到的结果
            stats.budget_exhausted = True
            break
        params = {
            "keywords": keyword,
            "city": city,
            "citylimit": "true",
            "offset": str(page_size),
            "page": str(page),
            "extensions": "all",
            "polygon": _polygon_param(bbox),
        }
        response: AmapResponse = client.get(TEXT_SEARCH_PATH, params)
        stats.requests += 1

        if not response.ok:
            raise AmapUnavailable(response.error or "高德 POI 搜索失败")

        pois = response.pois
        collected.extend(pois)
        stats.per_keyword[keyword] = stats.per_keyword.get(keyword, 0) + len(pois)
        if len(pois) < page_size:
            break

    return collected


def _dedupe(rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """按「名称 + 四舍五入坐标」去重，保留首次出现。"""
    seen = set()
    result: List[Dict[str, Any]] = []
    for row in rows:
        name = str(row.get("name") or "")
        location = str(row.get("location") or "")
        key = (name, location)
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(row))
    return result


def search_pois(
    client: AmapClient,
    keywords: Sequence[str],
    bbox: BBox,
    quadtree: QuadtreeConfig,
    city: str = DEFAULT_CITY,
    stats: Optional[QuadtreeStats] = None,
    depth: int = 0,
) -> List[Dict[str, Any]]:
    """在 bbox 内按多个关键词搜索 POI，必要时四叉树递归。

    Returns:
        去重后的原始 POI 字典列表（高德原始字段，尚未转坐标）。
    """
    current_stats = stats if stats is not None else QuadtreeStats()
    current_stats.max_depth = max(current_stats.max_depth, depth)

    rows: List[Dict[str, Any]] = []
    for keyword in keywords:
        rows.extend(_fetch_pages(client, keyword, bbox, quadtree, city, current_stats))
    rows = _dedupe(rows)

    current_stats.leaves += 1

    if current_stats.requests >= MAX_REQUESTS:
        current_stats.budget_exhausted = True

    if current_stats.budget_exhausted or not _should_split(
        len(rows), bbox, depth, quadtree
    ):
        if len(rows) >= quadtree.poi_threshold:
            # 达到阈值但已触及深度/跨度/预算上限：结果可能仍被截断，如实记录
            current_stats.truncated_leaves += 1
        return rows

    current_stats.splits += 1
    # 预算可能在子查询中耗尽，父区域已取得的记录必须保留。
    merged: List[Dict[str, Any]] = list(rows)
    for child in bbox_quadrants(bbox):
        merged.extend(
            search_pois(
                client=client,
                keywords=keywords,
                bbox=child,
                quadtree=quadtree,
                city=city,
                stats=current_stats,
                depth=depth + 1,
            )
        )
    return _dedupe(merged)


def parse_poi(raw: Mapping[str, Any], category: str) -> Optional[Dict[str, Any]]:
    """把高德原始 POI 转成项目内部记录（坐标为 GCJ02，交由加载流程转换）。

    Returns:
        记录字典；``location`` 缺失或非法时返回 None。
    """
    location = str(raw.get("location") or "")
    parts = location.split(",")
    if len(parts) != 2:
        return None
    try:
        lon = float(parts[0])
        lat = float(parts[1])
    except ValueError:
        return None

    poi_id = str(raw.get("id") or "").strip()
    name = str(raw.get("name") or "").strip()
    if not name:
        return None

    address = raw.get("address")
    if isinstance(address, list):
        address = "".join(str(part) for part in address)

    return {
        "id": f"amap_{poi_id}" if poi_id else f"amap_{name}",
        "name": name,
        "category": category,
        "lon": lon,
        "lat": lat,
        "coord_system": "gcj02",  # 高德返回 GCJ02，入库前统一转换
        "address": str(address or ""),
        "open_hours": "",
        "has_ramp": None,
        "source": "高德样例",
        "confidence": "medium",
        "updated_at": "",
        "notes": "来自高德开放平台 POI 搜索，坐标已转换为 WGS84（如转换不可用则标记待复核）",
    }


def search_category(
    client: AmapClient,
    category: str,
    bbox: BBox,
    quadtree: QuadtreeConfig,
    city: str = DEFAULT_CITY,
) -> Tuple[List[Dict[str, Any]], QuadtreeStats]:
    """按类目搜索 POI 并解析为内部记录。

    Returns:
        (记录列表, 统计)。
    """
    keywords = quadtree.keywords_for(category)
    if not keywords:
        raise AmapUnavailable(
            f"配置中缺少类目 {category!r} 的搜索关键词。"
            "下一步：在 config/default.json 的 quadtree.keywords 中补写。"
        )

    stats = QuadtreeStats()
    raw_rows = search_pois(client, keywords, bbox, quadtree, city=city, stats=stats)

    parsed: List[Dict[str, Any]] = []
    for raw in raw_rows:
        record = parse_poi(raw, category)
        if record is not None:
            parsed.append(record)
    return parsed, stats
