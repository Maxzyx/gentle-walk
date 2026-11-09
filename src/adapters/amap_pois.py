"""按行政区抓取高德 POI（省着用：区级限定 + 分页上限 + 请求预算 + 哈希缓存）。

区域限定
--------
使用 ``city=<adcode>`` + ``citylimit=true`` 做行政区限定，再按本地边界裁剪。
碑林区 adcode 为 ``610103``；矩形参数不作为可靠的片区过滤依据。

请求预算
--------
为控制查询成本，默认每个关键词最多 3 页、单类目总请求不超过 12 次；
同一参数由 ``AmapClient`` 做 sha256 缓存，重复运行不再打网络。

坐标
----
高德返回 GCJ02；入库前统一转 WGS84（``coord_transform.to_wgs84``，优先
geotransform / coordTransform）。转换失败或落在片区外的条目单独计数，不混入 CSV。
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import date
from math import hypot
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..geo.coord_transform import to_wgs84
from ..models import CATEGORY_GREENGROCER, CATEGORY_CLINIC
from .amap_client import AmapClient, AmapNetworkError, AmapUnavailable

#: 碑林区行政区划代码（高德 adcode）
DISTRICT_ADCODES: Dict[str, str] = {"碑林区": "610103"}

#: 每个类目的检索关键词（AGENTS.md 要求菜店/医院都要多关键词覆盖）
CATEGORY_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    CATEGORY_GREENGROCER: ("菜市场", "生鲜超市", "蔬菜店"),
    CATEGORY_CLINIC: ("社区卫生服务站", "社区医院"),
}

#: 类型关键词过滤：高德 ``type`` 字段形如「购物服务;综合市场;蔬菜市场」
CATEGORY_TYPE_HINTS: Dict[str, Tuple[str, ...]] = {
    CATEGORY_GREENGROCER: ("市场", "蔬菜", "生鲜", "超市", "水果", "农贸"),
    CATEGORY_CLINIC: ("医院", "卫生", "诊所", "卫生院", "医疗", "保健"),
}

PAGE_SIZE = 25
DEFAULT_MAX_PAGES = 3
DEFAULT_REQUEST_BUDGET = 12
PLACE_TEXT_PATH = "/v3/place/text"

#: 边界容差（度，约 1 米），用于处理紧贴边界的坐标舍入误差。
#: 这是片区筛选启发式规则，真实设施归属仍需现场核实。
BOUNDARY_TOLERANCE_DEG = 1.0 / 111_000.0

#: 输出 CSV 表头，与 ``data/pois_*.csv`` 样例一致
POI_FIELDS: Tuple[str, ...] = (
    "id",
    "name",
    "category",
    "lon",
    "lat",
    "coord_system",
    "address",
    "open_hours",
    "has_ramp",
    "source",
    "confidence",
    "updated_at",
    "notes",
)


class PoiFetchError(RuntimeError):
    """POI 抓取失败（无 Key、网络、区县代码未知等），调用方应提示或降级。"""


# --------------------------------------------------------------------------- #
# 抓取
# --------------------------------------------------------------------------- #
def fetch_category(
    client: AmapClient,
    category: str,
    adcode: str,
    max_pages: int = DEFAULT_MAX_PAGES,
    request_budget: int = DEFAULT_REQUEST_BUDGET,
    keywords: Optional[Sequence[str]] = None,
    page_size: int = PAGE_SIZE,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """抓取一个类目的原始 POI（未转换坐标、未裁剪）。

    Args:
        client: 高德客户端（负责 Key、缓存、限流退避）。
        category: ``greengrocer`` 或 ``clinic``。
        adcode: 行政区划代码，例如碑林区 ``610103``。
        max_pages: 每个关键词最多翻几页。
        request_budget: 该类目允许的最大请求数。
        keywords: 覆盖默认关键词（测试用）。

    Returns:
        ``(raw_pois, stats)``；stats 记录请求数、页数、预算是否耗尽等。
    """
    chosen_keywords = tuple(keywords or CATEGORY_KEYWORDS.get(category, ()))
    if not chosen_keywords:
        raise PoiFetchError(f"未知类目 {category}。下一步：使用 greengrocer 或 clinic。")

    seen: Dict[str, Dict[str, Any]] = {}
    stats: Dict[str, Any] = {
        "category": category,
        "requests": 0,
        "pages": 0,
        "raw": 0,
        "budget_exhausted": False,
        "truncated": False,
        "errors": [],
        "keywords": {},
    }

    for keyword in chosen_keywords:
        if stats["budget_exhausted"]:
            break
        keyword_pages = 0
        page = 1
        while page <= max_pages:
            if stats["requests"] >= request_budget:
                stats["budget_exhausted"] = True
                break
            stats["requests"] += 1
            try:
                response = client.get(
                    PLACE_TEXT_PATH,
                    {
                        "keywords": keyword,
                        "city": adcode,
                        "citylimit": "true",
                        "offset": str(page_size),
                        "page": str(page),
                        "extensions": "base",
                    },
                )
            except (AmapUnavailable, AmapNetworkError) as exc:
                stats["errors"].append(f"{keyword} 第{page}页：{exc}")
                break
            if not response.ok:
                stats["errors"].append(
                    f"{keyword} 第{page}页：{response.error or response.status}"
                )
                break
            pois = response.pois
            for poi in pois:
                poi_id = str(poi.get("id") or "")
                if poi_id and poi_id not in seen:
                    seen[poi_id] = poi
            keyword_pages += 1
            stats["pages"] += 1
            if len(pois) < page_size or page * page_size >= response.count:
                break
            if page == max_pages:
                stats["truncated"] = True
            page += 1
        stats["keywords"][keyword] = keyword_pages

    stats["raw"] = len(seen)
    return list(seen.values()), stats


# --------------------------------------------------------------------------- #
# 转换与裁剪
# --------------------------------------------------------------------------- #
def load_rings(path: Path) -> Tuple[List[List[Tuple[float, float]]], List[List[Tuple[float, float]]]]:
    """从 JSON 读取候选片区边界。

    支持两种格式：``{"rings": [...], "holes": [...]}``，或
    ``osm_roads.save_raw`` 写出的完整原始 JSON（含 rings/holes）。
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rings = payload.get("rings") or []
    holes = payload.get("holes") or []
    return (
        [[(float(point[0]), float(point[1])) for point in ring] for ring in rings],
        [[(float(point[0]), float(point[1])) for point in hole] for hole in holes],
    )


def _parse_location(raw: Any) -> Optional[Tuple[float, float]]:
    """解析高德 ``location`` 字段（``"lon,lat"``）。"""
    if not raw:
        return None
    parts = str(raw).split(",")
    if len(parts) != 2:
        return None
    try:
        return (float(parts[0]), float(parts[1]))
    except ValueError:
        return None


def _poi_address(poi: Mapping[str, Any]) -> str:
    """拼出地址（高德可能只给 adname + address）。"""
    address = str(poi.get("address") or "").strip()
    if address and not address.isdigit() and "[]" not in address:
        return address
    district = str(poi.get("adname") or "").strip()
    return district


def _type_matches(poi: Mapping[str, Any], hints: Sequence[str]) -> bool:
    """POI 类型是否命中该类目关键词。"""
    type_text = str(poi.get("type") or "")
    return any(hint in type_text for hint in hints)


def is_inside_area(
    lon: float,
    lat: float,
    rings: Optional[Sequence[Sequence[Tuple[float, float]]]],
    holes: Optional[Sequence[Sequence[Tuple[float, float]]]] = None,
    tolerance_deg: float = BOUNDARY_TOLERANCE_DEG,
) -> bool:
    """点是否属于片区（边界上带容差）。

    使用小范围边界容差处理坐标舍入误差，不代表已验证设施归属。
    """
    from .osm_roads import point_in_rings

    if not rings:
        return True
    if point_in_rings(lon, lat, rings, holes or []):
        return True
    for ring in rings:
        for index in range(len(ring) - 1):
            x1, y1 = ring[index]
            x2, y2 = ring[index + 1]
            dx, dy = x2 - x1, y2 - y1
            length_sq = dx * dx + dy * dy
            if length_sq == 0:
                ratio = 0.0
            else:
                ratio = max(0.0, min(1.0, ((lon - x1) * dx + (lat - y1) * dy) / length_sq))
            if hypot(lon - (x1 + ratio * dx), lat - (y1 + ratio * dy)) <= tolerance_deg:
                return True
    return False


def build_rows(
    raw_pois: Sequence[Mapping[str, Any]],
    category: str,
    rings: Optional[Sequence[Sequence[Tuple[float, float]]]] = None,
    holes: Optional[Sequence[Sequence[Tuple[float, float]]]] = None,
    adcode: str = "",
) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    """原始 POI → CSV 行：类型过滤、坐标转换、片区裁剪、按名称去重。

    边界判定采用 :data:`BOUNDARY_TOLERANCE_DEG`（约 1 米）容差。
    该规则只处理边界附近的坐标误差，设施归属仍需现场核实。

    Returns:
        ``(rows, stats)``；stats 含 ``converted/pending/outside/type_rejected/duplicated``。
    """
    stats = {
        "converted": 0,
        "pending": 0,
        "outside": 0,
        "type_rejected": 0,
        "duplicated": 0,
        "no_location": 0,
    }
    seen_names: set = set()
    rows: List[Dict[str, str]] = []
    hints = CATEGORY_TYPE_HINTS.get(category, ())
    today = date.today().isoformat()

    for poi in raw_pois:
        if hints and not _type_matches(poi, hints):
            stats["type_rejected"] += 1
            continue
        location = _parse_location(poi.get("location"))
        if location is None:
            stats["no_location"] += 1
            continue
        converted = to_wgs84(location[0], location[1], "gcj02")
        if not converted.ok:
            stats["pending"] += 1
            continue
        if not is_inside_area(converted.lon, converted.lat, rings, holes):
            stats["outside"] += 1
            continue
        name = str(poi.get("name") or "").strip()
        dedupe_key = (name, round(converted.lon, 5), round(converted.lat, 5))
        if dedupe_key in seen_names:
            stats["duplicated"] += 1
            continue
        seen_names.add(dedupe_key)
        stats["converted"] += 1
        rows.append(
            {
                "id": f"amap_{poi.get('id')}" if poi.get("id") else f"amap_p{len(rows) + 1}",
                "name": name,
                "category": category,
                "lon": f"{converted.lon:.7f}",
                "lat": f"{converted.lat:.7f}",
                "coord_system": "wgs84",
                "address": _poi_address(poi),
                "open_hours": "",
                "has_ramp": "",
                "source": "高德",
                "confidence": "medium",
                "updated_at": today,
                "notes": (
                    f"高德 POI 检索（city={adcode} 区级限定，method={converted.method}），"
                    "坐标已由 GCJ02 转 WGS84；开放时间与入口坡道未知，以现场为准"
                ),
            }
        )
    return rows, stats


def filter_connected_rows(
    rows: Sequence[Mapping[str, str]],
    walk_graph: Any,
) -> Tuple[List[Dict[str, str]], int]:
    """只保留最近路网节点位于主连通分量的 POI。

    OSM 路网存在「内部步道与小广场自成孤岛」的情况：POI 在片区内、也在 500 米吸附
    范围内，但从主路网无法走到。这里按连通分量过滤，避免列表里出现永远规划不出路线的目的地。

    Returns:
        ``(kept_rows, dropped)``。
    """
    import networkx as nx

    from ..graph_builder import nearest_node

    graph = walk_graph.graph
    if graph.number_of_nodes() == 0:
        return list(rows), 0
    main_component = max(nx.connected_components(graph), key=len)
    kept: List[Dict[str, str]] = []
    dropped = 0
    for row in rows:
        try:
            lon = float(row["lon"])
            lat = float(row["lat"])
        except (KeyError, TypeError, ValueError):
            dropped += 1
            continue
        node_id = nearest_node(walk_graph, (lon, lat))
        if node_id is None or node_id not in main_component:
            dropped += 1
            continue
        kept.append(dict(row))
    return kept, dropped


def write_poi_csv(rows: Sequence[Mapping[str, str]], path: Path) -> Path:
    """写出 POI CSV（utf-8，表头与样例一致）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(POI_FIELDS))
        writer.writeheader()
        writer.writerows(rows)
    return path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口：抓取片区 POI 并写成项目 CSV。"""
    import argparse

    from ..config import load_config

    parser = argparse.ArgumentParser(
        prog="python -m src.adapters.amap_pois",
        description="按行政区抓取高德 POI（区级限定、请求预算、哈希缓存），输出 poi CSV",
    )
    parser.add_argument("--district", default="碑林区", help="行政区名称，默认碑林区")
    parser.add_argument("--adcode", default=None, help="行政区划代码；默认按 district 查表")
    parser.add_argument("--out", default="data/osm_beilin", help="输出目录")
    parser.add_argument("--ring-json", default=None, help="边界 JSON（rings/holes，或 osm_roads 原始 JSON）")
    parser.add_argument("--bbox", default=None, help="lon_min,lat_min,lon_max,lat_max（无边界时按矩形裁剪）")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES, help="每关键词最多页数")
    parser.add_argument("--budget", type=int, default=DEFAULT_REQUEST_BUDGET, help="单类目最大请求数")
    parser.add_argument("--write", action="store_true", help="实际写出 CSV（默认只试算）")
    parser.add_argument("--config", default="config/beilin.json", help="连通性校验用的配置（决定 data_dir）")
    parser.add_argument(
        "--require-connected",
        action="store_true",
        help="只保留最近路网节点在主连通分量的 POI（过滤永远规划不出路线的孤岛 POI）",
    )
    args = parser.parse_args(argv)

    adcode = args.adcode or DISTRICT_ADCODES.get(args.district)
    if not adcode:
        print(
            f"未知行政区「{args.district}」的 adcode。"
            "下一步：用 --adcode 指定（碑林区为 610103）。"
        )
        return 2

    rings: Optional[List[List[Tuple[float, float]]]] = None
    holes: Optional[List[List[Tuple[float, float]]]] = None
    if args.ring_json:
        rings, holes = load_rings(Path(args.ring_json))
        print(f"片区边界：{len(rings)} 个外环、{len(holes)} 个洞（来自 {args.ring_json}）")
    elif args.bbox:
        lon_min, lat_min, lon_max, lat_max = (float(v) for v in args.bbox.split(","))
        rings = [
            [
                (lon_min, lat_min),
                (lon_max, lat_min),
                (lon_max, lat_max),
                (lon_min, lat_max),
                (lon_min, lat_min),
            ]
        ]
        print("未提供边界 JSON，按 bbox 矩形裁剪（邻区 POI 可能混入）")
    else:
        print("未提供 --ring-json 或 --bbox，跳过片区裁剪（仅按行政区限定）。")

    config = load_config()
    client = AmapClient(config)
    if not client.has_key:
        print("未配置 AMAP_KEY，无法抓取。下一步：python -m src.cli --set-key 配置后重试。")
        return 1

    out_dir = Path(args.out)
    walk_graph = None
    if args.require_connected:
        from dataclasses import replace as _replace

        from ..data_loader import load_dataset
        from ..graph_builder import build_graph

        try:
            base_config = load_config(Path(args.config))
            scoped_config = _replace(
                base_config,
                runtime=_replace(base_config.runtime, data_dir=out_dir.resolve()),
            )
            walk_graph = build_graph(load_dataset(scoped_config), scoped_config)
            if not walk_graph.node_count or not walk_graph.edge_count:
                raise ValueError("路网缺少可用节点或连接边")
            print(f"连通性校验：已加载 {walk_graph.node_count} 个路网节点")
        except Exception as exc:  # noqa: BLE001 - 校验失败不应阻断数据抓取
            print(f"连通性校验不可用（{exc}），已停止并保留原有数据。下一步：检查 --config 与路网 CSV。")
            return 1

    exit_code = 0
    staged: List[Tuple[Path, List[Dict[str, str]]]] = []
    for category in (CATEGORY_GREENGROCER, CATEGORY_CLINIC):
        raw, fetch_stats = fetch_category(
            client,
            category,
            adcode=adcode,
            max_pages=args.max_pages,
            request_budget=args.budget,
        )
        rows, build_stats = build_rows(raw, category, rings, holes, adcode=adcode)
        dropped_isolated = 0
        if walk_graph is not None:
            rows, dropped_isolated = filter_connected_rows(rows, walk_graph)
        print(
            f"[{category}] 请求 {fetch_stats['requests']} 次、{fetch_stats['pages']} 页，"
            f"原始 {fetch_stats['raw']} 条 → 有效 {len(rows)} 条 "
            f"（类型不符 {build_stats['type_rejected']}、区外 {build_stats['outside']}、"
            f"坐标待复核 {build_stats['pending']}、重复 {build_stats['duplicated']}）"
        )
        if dropped_isolated:
            print(f"  连通性过滤：丢弃 {dropped_isolated} 个位于孤立路网小岛的 POI（路线永远不可达）")
        if fetch_stats["budget_exhausted"]:
            print(f"  注意：{category} 触发请求预算上限，覆盖可能不完整")
            exit_code = 1
        for error in fetch_stats["errors"][:3]:
            print(f"  错误：{error}")
        if fetch_stats["truncated"]:
            print("  结果触及分页上限，尚未完整；下一步：增大 --max-pages 和 --budget 后重试。")
        if fetch_stats["errors"] or fetch_stats["truncated"] or build_stats["pending"] or not rows:
            exit_code = 1
        staged.append((out_dir / f"pois_{category}.csv", rows))
    if args.write:
        if exit_code:
            print("本次抓取失败、为空或不完整，已保留原有两个 POI 文件。下一步：修正问题后重试。")
        else:
            for path, rows in staged:
                write_poi_csv(rows, path)
                print(f"  已写出：{path}（{len(rows)} 行）")
    return exit_code


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    sys.exit(main())
