"""高德区级 POI 抓取模块测试（全部离线，不联网）。

覆盖：
- 行政区限定的请求参数（city=adcode + citylimit）；
- 分页停止条件、请求预算、跨关键词去重；
- 类型过滤、坐标转换降级、片区多边形裁剪、按名称去重；
- 主连通分量过滤（孤岛 POI 剔除）；
- 边界 JSON 读取与 CSV 表头。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import networkx as nx
import pytest

from src.adapters import amap_pois as ap
from src.geo.coord_transform import CoordResult
from src.graph_builder import WalkGraph
from src.models import GraphNode

SQUARE = [(108.94, 34.25), (108.96, 34.25), (108.96, 34.27), (108.94, 34.27), (108.94, 34.25)]


class FakeResponse:
    """伪造 AmapResponse。"""

    def __init__(self, pois: List[Dict[str, Any]], count: int, ok: bool = True) -> None:
        self.pois = pois
        self.count = count
        self.ok = ok
        self.error = "" if ok else "boom"
        self.status = 200 if ok else 500


class FakeClient:
    """伪造 AmapClient：按 (keywords, page) 返回预置分页。"""

    def __init__(self, pages: Dict[Any, FakeResponse], default_ok: bool = True) -> None:
        self.pages = pages
        self.default_ok = default_ok
        self.calls: List[Dict[str, str]] = []

    def get(self, path: str, params: Dict[str, str]) -> FakeResponse:
        self.calls.append(dict(params))
        key = (params["keywords"], int(params["page"]))
        if key in self.pages:
            return self.pages[key]
        return FakeResponse([], 0, ok=self.default_ok)


def _poi(poi_id: str, name: str, location: str, type_text: str = "购物服务;综合市场;蔬菜市场") -> Dict[str, Any]:
    return {"id": poi_id, "name": name, "location": location, "type": type_text, "address": "测试路 1 号"}


# --------------------------------------------------------------------------- #
# 抓取
# --------------------------------------------------------------------------- #
def test_fetch_category_uses_district_params() -> None:
    """请求必须带 city=adcode + citylimit=true，避免跨区混入。"""
    client = FakeClient({("菜市场", 1): FakeResponse([_poi("1", "A", "108.95,34.26")], count=1)})
    raw, stats = ap.fetch_category(client, "greengrocer", "610103", keywords=("菜市场",))

    assert len(raw) == 1
    assert stats["requests"] == 1
    call = client.calls[0]
    assert call["city"] == "610103"
    assert call["citylimit"] == "true"
    assert call["offset"] == "25"


def test_fetch_category_stops_when_page_short() -> None:
    """返回不足一页即停止翻页。"""
    client = FakeClient(
        {
            ("菜市场", 1): FakeResponse([_poi(str(i), f"P{i}", "108.95,34.26") for i in range(3)], count=3),
        }
    )
    raw, stats = ap.fetch_category(client, "greengrocer", "610103", keywords=("菜市场",), max_pages=5)
    assert stats["requests"] == 1
    assert len(raw) == 3


def test_fetch_category_paginates_until_max_pages() -> None:
    """结果满页且总数未取完时继续翻页，受 max_pages 限制。"""
    full_page = lambda start: FakeResponse(  # noqa: E731
        [_poi(str(start + i), f"P{start + i}", "108.95,34.26") for i in range(25)], count=200
    )
    client = FakeClient({("菜市场", 1): full_page(0), ("菜市场", 2): full_page(25), ("菜市场", 3): full_page(50)})
    _raw, stats = ap.fetch_category(client, "greengrocer", "610103", keywords=("菜市场",), max_pages=2)
    assert stats["pages"] == 2
    assert stats["budget_exhausted"] is False


def test_fetch_category_respects_request_budget() -> None:
    """请求预算耗尽时停止，并标记 budget_exhausted。"""
    full_page = FakeResponse([_poi(str(i), f"P{i}", "108.95,34.26") for i in range(25)], count=500)
    client = FakeClient({("a", 1): full_page, ("a", 2): full_page, ("b", 1): full_page})
    _raw, stats = ap.fetch_category(
        client, "greengrocer", "610103", keywords=("a", "b"), max_pages=5, request_budget=1
    )
    assert stats["requests"] == 1
    assert stats["budget_exhausted"] is True


def test_fetch_category_dedupes_across_keywords() -> None:
    """多个关键词返回同一 POI 时按 id 去重。"""
    same = _poi("dup", "同一家店", "108.95,34.26")
    client = FakeClient(
        {
            ("a", 1): FakeResponse([same], count=1),
            ("b", 1): FakeResponse([same], count=1),
        }
    )
    raw, _stats = ap.fetch_category(client, "greengrocer", "610103", keywords=("a", "b"))
    assert len(raw) == 1


def test_fetch_category_reports_error_without_crash() -> None:
    """接口失败时记录错误并返回已拿到的数据，不抛异常。"""
    client = FakeClient({}, default_ok=False)
    raw, stats = ap.fetch_category(client, "clinic", "610103", keywords=("社区卫生服务站",))
    assert raw == []
    assert stats["requests"] == 1
    assert stats["errors"] and "boom" in stats["errors"][0]


# --------------------------------------------------------------------------- #
# 行构建
# --------------------------------------------------------------------------- #
def test_build_rows_converts_filters_and_clips(monkeypatch: pytest.MonkeyPatch) -> None:
    """合法 POI 转 WGS84、类型不符/区外/转换失败分别计数。"""
    def fake_to_wgs84(lon: float, lat: float, system: str) -> CoordResult:
        if lon > 108.999:  # 模拟转换失败
            return CoordResult(lon, lat, system, "pending_review", "none", "缺库")
        return CoordResult(lon + 0.001, lat + 0.001, system, "ok", "coordTransform")

    monkeypatch.setattr(ap, "to_wgs84", fake_to_wgs84)
    raw = [
        _poi("1", "好店", "108.950,34.260"),  # 转换后 108.951,34.261 区内
        _poi("2", "区外店", "108.900,34.260"),  # 转换后 108.901 区外
        _poi("3", "银行", "108.950,34.260", type_text="金融保险服务;银行"),  # 类型不符
        _poi("4", "失败", "109.010,34.260"),  # 转换失败
    ]
    rows, stats = ap.build_rows(raw, "greengrocer", rings=[SQUARE], holes=[], adcode="610103")

    assert [row["name"] for row in rows] == ["好店"]
    assert stats["converted"] == 1
    assert stats["outside"] == 1
    assert stats["type_rejected"] == 1
    assert stats["pending"] == 1
    assert rows[0]["coord_system"] == "wgs84"
    assert rows[0]["source"] == "高德"
    assert rows[0]["id"].startswith("amap_")


def test_build_rows_keeps_poi_within_boundary_tolerance(monkeypatch: pytest.MonkeyPatch) -> None:
    """边界容差内的合成点保留，超过容差的合成点排除。"""
    monkeypatch.setattr(
        ap,
        "to_wgs84",
        lambda lon, lat, system: CoordResult(lon, lat, system, "ok", "coordTransform"),
    )
    square = [(108.94, 34.25), (108.96, 34.25), (108.96, 34.27), (108.94, 34.27), (108.94, 34.25)]
    raw = [
        # 偏出西边界约 0.1 米（1e-6 度）：应保留
        _poi("1", "边界上小店", "108.939999,34.260"),
        # 偏出西边界约 100 米：应丢弃
        _poi("2", "邻区小店", "108.939000,34.260"),
    ]
    rows, stats = ap.build_rows(raw, "greengrocer", rings=[square], holes=[], adcode="610103")

    assert [row["name"] for row in rows] == ["边界上小店"]
    assert stats["outside"] == 1


def test_build_rows_dedupes_by_name_and_location(monkeypatch: pytest.MonkeyPatch) -> None:
    """同名同位置的两个 POI 只保留一条。"""
    monkeypatch.setattr(
        ap,
        "to_wgs84",
        lambda lon, lat, system: CoordResult(lon, lat, system, "ok", "coordTransform"),
    )
    raw = [_poi("1", "同名店", "108.950,34.260"), _poi("2", "同名店", "108.950,34.260")]
    rows, stats = ap.build_rows(raw, "greengrocer")
    assert len(rows) == 1
    assert stats["duplicated"] == 1


def test_build_rows_without_polygon_keeps_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """未提供边界时不裁剪，只做转换与类型过滤。"""
    monkeypatch.setattr(
        ap,
        "to_wgs84",
        lambda lon, lat, system: CoordResult(lon, lat, system, "ok", "coordTransform"),
    )
    rows, _stats = ap.build_rows([_poi("1", "远店", "109.500,34.900")], "greengrocer")
    assert len(rows) == 1


# --------------------------------------------------------------------------- #
# 连通性过滤
# --------------------------------------------------------------------------- #
def _walk_graph() -> WalkGraph:
    """构造一个主分量 + 孤立小岛的路网。"""
    graph = nx.Graph()
    graph.add_edges_from([("osm_1", "osm_2"), ("osm_2", "osm_3")])
    graph.add_edge("osm_9", "osm_10")  # 孤立小岛
    nodes = {
        "osm_1": GraphNode("osm_1", 108.950, 34.260),
        "osm_2": GraphNode("osm_2", 108.951, 34.260),
        "osm_3": GraphNode("osm_3", 108.952, 34.260),
        "osm_9": GraphNode("osm_9", 108.940, 34.255),
        "osm_10": GraphNode("osm_10", 108.941, 34.255),
    }
    return WalkGraph(graph=graph, nodes=nodes)


def test_filter_connected_rows_drops_island_pois() -> None:
    """最近节点不在主分量的 POI 被剔除。"""
    rows = [
        {"name": "主网店", "lon": "108.951", "lat": "34.260"},
        {"name": "孤岛店", "lon": "108.9405", "lat": "34.255"},
    ]
    kept, dropped = ap.filter_connected_rows(rows, _walk_graph())
    assert [row["name"] for row in kept] == ["主网店"]
    assert dropped == 1


def test_filter_connected_rows_keeps_all_when_graph_empty() -> None:
    """空路网时不过滤（例如校验数据未就绪）。"""
    rows = [{"name": "店", "lon": "108.95", "lat": "34.26"}]
    kept, dropped = ap.filter_connected_rows(rows, WalkGraph(graph=nx.Graph()))
    assert kept == rows
    assert dropped == 0


# --------------------------------------------------------------------------- #
# 边界读取与写盘
# --------------------------------------------------------------------------- #
def test_load_rings_reads_boundary_and_raw_network(tmp_path: Path) -> None:
    """兼容「boundary.json」与 osm_roads 原始 JSON 两种格式。"""
    boundary = tmp_path / "boundary.json"
    boundary.write_text(
        json.dumps({"rings": [[[108.94, 34.25], [108.96, 34.25], [108.96, 34.27]]], "holes": []}),
        encoding="utf-8",
    )
    rings, holes = ap.load_rings(boundary)
    assert rings[0][0] == (108.94, 34.25)
    assert holes == []

    full = tmp_path / "raw.json"
    full.write_text(
        json.dumps(
            {
                "district": "碑林区",
                "relation_id": 1,
                "bbox": [108.9, 34.2, 109.0, 34.3],
                "rings": [[[108.94, 34.25], [108.96, 34.25], [108.96, 34.27]]],
                "holes": [],
                "nodes": {},
                "ways": {},
            }
        ),
        encoding="utf-8",
    )
    rings2, _holes2 = ap.load_rings(full)
    assert rings2[0][2] == (108.96, 34.27)


def test_write_poi_csv_headers_match_sample(tmp_path: Path) -> None:
    """写出的表头与 data/pois_*.csv 一致，加载器才能直接读。"""
    row = {
        "id": "amap_1",
        "name": "测试店",
        "category": "greengrocer",
        "lon": "108.95",
        "lat": "34.26",
        "coord_system": "wgs84",
        "address": "测试路",
        "open_hours": "",
        "has_ramp": "",
        "source": "高德",
        "confidence": "medium",
        "updated_at": "2026-11-02",
        "notes": "",
    }
    path = ap.write_poi_csv([row], tmp_path / "pois_greengrocer.csv")
    assert path.read_text(encoding="utf-8").splitlines()[0] == (
        "id,name,category,lon,lat,coord_system,address,open_hours,has_ramp,source,confidence,updated_at,notes"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("108.95,34.26", (108.95, 34.26)), ("", None), ("abc", None), ("1,2,3", None)],
)
def test_parse_location(raw, expected) -> None:
    """location 字段容错解析。"""
    assert ap._parse_location(raw) == expected



@pytest.mark.parametrize("failure", ["business", "network", "truncated", "empty"])
def test_failed_or_partial_import_preserves_existing_files(tmp_path, monkeypatch, failure) -> None:
    from src.adapters.amap_client import AmapUnavailable, AmapNetworkError
    before = {}
    for name in ("pois_greengrocer.csv", "pois_clinic.csv"):
        path = tmp_path / name
        path.write_text("existing-valid-data\n")
        before[name] = path.read_bytes()
    class Client:
        has_key = True
        def get(self, path, params):
            if failure == "business": raise AmapUnavailable("invalid dummy key")
            if failure == "network": raise AmapNetworkError("offline")
            if failure == "empty": return FakeResponse([], 0)
            return FakeResponse([_poi(str(i), f"P{i}", "108.95,34.26") for i in range(25)], 500)
    monkeypatch.setattr(ap, "AmapClient", lambda config: Client())
    monkeypatch.setattr(ap, "to_wgs84", lambda lon, lat, system: CoordResult(lon, lat, system, "ok", "test"))
    assert ap.main(["--out", str(tmp_path), "--max-pages", "1", "--write"]) == 1
    assert {name: (tmp_path / name).read_bytes() for name in before} == before
