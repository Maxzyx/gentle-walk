"""OSM 路网适配器测试（全部离线，不联网）。

覆盖：
- OSM 标签 → 项目 walk_type 的映射（台阶/过街/斜坡/机动车道/禁止通行）；
- 边界多边形拼接与点在多边形内判断；
- 建图时的裁剪、去重、节点前缀、统计；
- 原始 JSON 的保存/加载往返；
- CSV 表头与样例数据一致。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.adapters import osm_roads as osm
from src.models import (
    WALK_TYPE_CROSSING,
    WALK_TYPE_NORMAL,
    WALK_TYPE_SLOPE,
    WALK_TYPE_STAIRS,
)

#: 测试用矩形片区：lon 108.94–108.96、lat 34.25–34.27
SQUARE = [(108.94, 34.25), (108.96, 34.25), (108.96, 34.27), (108.94, 34.27), (108.94, 34.25)]


def _network(ways, nodes, rings=None, holes=None) -> osm.OsmNetwork:
    """构造一个 OsmNetwork，避免测试里反复写样板。"""
    return osm.OsmNetwork(
        district="测试区",
        relation_id=1,
        bbox=(108.94, 34.25, 108.96, 34.27),
        nodes=dict(nodes),
        ways=dict(ways),
        rings=list(rings or []),
        holes=list(holes or []),
    )


# --------------------------------------------------------------------------- #
# 标签映射
# --------------------------------------------------------------------------- #
def test_steps_maps_to_21_with_step_count() -> None:
    """台阶：walk_type=21，step_count 取数字部分。"""
    walk_type, step_count, slope, surface, skip = osm.classify_way(
        {"highway": "steps", "step_count": "12级", "surface": "stone"}
    )
    assert (walk_type, step_count, slope, surface, skip) == (WALK_TYPE_STAIRS, 12, None, "stone", "")


def test_steps_without_step_count_keeps_none() -> None:
    """台阶缺 step_count：留空，由评分默认惩罚兜底。"""
    walk_type, step_count, _slope, _surface, skip = osm.classify_way({"highway": "steps"})
    assert walk_type == WALK_TYPE_STAIRS
    assert step_count is None
    assert skip == ""


def test_crossing_maps_to_3() -> None:
    """人行横道：footway=crossing → walk_type=3。"""
    walk_type, _count, _slope, _surface, skip = osm.classify_way(
        {"highway": "footway", "footway": "crossing"}
    )
    assert (walk_type, skip) == (WALK_TYPE_CROSSING, "")


def test_incline_maps_to_slope_22() -> None:
    """带坡度的步道：walk_type=22，坡度按百分比解析。"""
    walk_type, _count, slope, _surface, skip = osm.classify_way(
        {"highway": "footway", "incline": "10%"}
    )
    assert (walk_type, slope, skip) == (WALK_TYPE_SLOPE, 10.0, "")


def test_incline_up_uses_conservative_default() -> None:
    """incline=up 没有具体数值时用保守默认坡度。"""
    walk_type, _count, slope, _surface, _skip = osm.classify_way(
        {"highway": "path", "incline": "up"}
    )
    assert walk_type == WALK_TYPE_SLOPE
    assert slope == osm.DEFAULT_SLOPE_PCT


def test_ramp_tag_maps_to_slope() -> None:
    """ramp=yes 也按斜坡处理（老人关心坡道）。"""
    walk_type, _count, slope, _surface, _skip = osm.classify_way(
        {"highway": "footway", "ramp": "yes"}
    )
    assert walk_type == WALK_TYPE_SLOPE
    assert slope == osm.DEFAULT_SLOPE_PCT


def test_motor_road_without_sidewalk_is_skipped_by_default() -> None:
    """机动车道没人行道标注：默认排除（安全边界）。"""
    _walk_type, _count, _slope, _surface, skip = osm.classify_way({"highway": "primary"})
    assert "机动车道" in skip


def test_motor_road_kept_when_flag_enabled() -> None:
    """显式开关打开后，机动车道保留为普通步道。"""
    walk_type, _count, _slope, _surface, skip = osm.classify_way(
        {"highway": "primary"}, allow_motor_without_sidewalk=True
    )
    assert (walk_type, skip) == (WALK_TYPE_NORMAL, "")


def test_motor_road_with_sidewalk_is_kept() -> None:
    """带 sidewalk 标注的机动车道可步行。"""
    walk_type, _count, _slope, _surface, skip = osm.classify_way(
        {"highway": "secondary", "sidewalk": "both"}
    )
    assert (walk_type, skip) == (WALK_TYPE_NORMAL, "")


def test_private_access_is_skipped() -> None:
    """access=private 的路段不进入步行路网。"""
    _walk_type, _count, _slope, _surface, skip = osm.classify_way(
        {"highway": "footway", "access": "private"}
    )
    assert "access" in skip


def test_construction_is_skipped() -> None:
    """在建道路排除。"""
    _walk_type, _count, _slope, _surface, skip = osm.classify_way({"highway": "construction"})
    assert "类型排除" in skip


# --------------------------------------------------------------------------- #
# 解析辅助
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("12", 12), ("12级", 12), ("0", None), ("", None), (None, None), ("999", None), ("abc", None)],
)
def test_parse_step_count(raw, expected) -> None:
    """step_count 容错解析。"""
    assert osm._parse_step_count(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("10%", 10.0), ("10", 10.0), ("-8", 8.0), ("up", osm.DEFAULT_SLOPE_PCT), ("down", osm.DEFAULT_SLOPE_PCT), ("0", None), ("flat", None)],
)
def test_parse_incline(raw, expected) -> None:
    """incline 容错解析：统一成非负百分比。"""
    assert osm._parse_incline(raw) == expected


# --------------------------------------------------------------------------- #
# 多边形
# --------------------------------------------------------------------------- #
def test_point_in_ring_inside_and_outside() -> None:
    """射线法：矩形内为真，外为假。"""
    assert osm._point_in_ring(108.95, 34.26, SQUARE) is True
    assert osm._point_in_ring(108.90, 34.26, SQUARE) is False


def test_point_in_rings_respects_holes() -> None:
    """洞（飞地）内的点不算在片区内。"""
    hole = [(108.948, 34.255), (108.952, 34.255), (108.952, 34.259), (108.948, 34.259), (108.948, 34.255)]
    assert osm.point_in_rings(108.95, 34.257, [SQUARE], [hole]) is False
    assert osm.point_in_rings(108.955, 34.26, [SQUARE], [hole]) is True


def test_stitch_rings_joins_fragments() -> None:
    """边界碎片能按端点拼成闭合环。"""
    first = [(0.0, 0.0), (1.0, 0.0)]
    second = [(1.0, 0.0), (1.0, 1.0)]
    third = [(1.0, 1.0), (0.0, 1.0)]
    fourth = [(0.0, 1.0), (0.0, 0.0)]
    rings = osm._stitch_rings([first, third, second, fourth])
    assert len(rings) == 1
    assert rings[0][0] == rings[0][-1]
    assert len(rings[0]) == 5


def test_stitch_rings_drops_open_fragments() -> None:
    """拼不闭合的碎片被丢弃，不产生半开环。"""
    assert osm._stitch_rings([[(0.0, 0.0), (1.0, 0.0)]]) == []


# --------------------------------------------------------------------------- #
# 建图
# --------------------------------------------------------------------------- #
def test_build_network_basic_mapping_and_prefix() -> None:
    """节点加 osm_ 前缀，source=OSM，长度按 haversine 计算。"""
    network = _network(
        ways={10: ({"highway": "footway"}, [1, 2])},
        nodes={1: (108.9500, 34.2600), 2: (108.9510, 34.2600)},
    )
    node_rows, edge_rows, stats = osm.build_network(network, use_polygon=False)

    assert [row["node_id"] for row in node_rows] == ["osm_1", "osm_2"]
    assert all(row["source"] == "OSM" and row["coord_system"] == "wgs84" for row in node_rows)
    assert len(edge_rows) == 1
    edge = edge_rows[0]
    assert (edge["u"], edge["v"]) == ("osm_1", "osm_2")
    assert edge["source"] == "OSM"
    assert edge["length_m"] == pytest.approx(92.2, abs=1.0)
    assert stats.ways_kept == 1 and stats.edges == 1 and stats.nodes == 2


def test_build_network_clips_ways_outside_polygon() -> None:
    """片区外的道路被排除并计入统计。"""
    network = _network(
        ways={
            10: ({"highway": "footway"}, [1, 2]),  # 片区外
            11: ({"highway": "footway"}, [3, 4]),  # 片区内
        },
        nodes={
            1: (108.90, 34.20),
            2: (108.901, 34.20),
            3: (108.950, 34.260),
            4: (108.951, 34.260),
        },
        rings=[SQUARE],
    )
    _node_rows, edge_rows, stats = osm.build_network(network)

    assert stats.ways_outside == 1
    assert stats.ways_kept == 1
    assert len(edge_rows) == 1
    assert edge_rows[0]["u"] == "osm_3"


def test_build_network_keeps_boundary_crossing_way() -> None:
    """跨边界的道路只要有一段落在片区内就保留（避免路网被切碎）。"""
    network = _network(
        ways={10: ({"highway": "footway"}, [1, 2])},
        nodes={1: (108.9399, 34.260), 2: (108.9405, 34.260)},  # 1 在外、2 在内
        rings=[SQUARE],
    )
    _node_rows, edge_rows, stats = osm.build_network(network)
    assert stats.ways_kept == 1
    assert len(edge_rows) == 1


def test_build_network_dedupes_shared_segments() -> None:
    """同一对节点被两条 way 引用时只保留一段。"""
    network = _network(
        ways={
            10: ({"highway": "footway"}, [1, 2, 3]),
            11: ({"highway": "residential"}, [1, 2]),
        },
        nodes={
            1: (108.9500, 34.2600),
            2: (108.9510, 34.2600),
            3: (108.9520, 34.2600),
        },
    )
    _node_rows, edge_rows, _stats = osm.build_network(network, use_polygon=False)
    pairs = {(row["u"], row["v"]) for row in edge_rows}
    assert len(edge_rows) == 2
    assert len(pairs) == 2


def test_build_network_counts_stairs_and_crossings() -> None:
    """统计台阶段/过街段数量。"""
    network = _network(
        ways={
            10: ({"highway": "steps", "step_count": "5"}, [1, 2]),
            11: ({"highway": "footway", "footway": "crossing"}, [2, 3]),
        },
        nodes={
            1: (108.9500, 34.2600),
            2: (108.9505, 34.2600),
            3: (108.9510, 34.2600),
        },
    )
    _node_rows, _edge_rows, stats = osm.build_network(network, use_polygon=False)
    assert stats.stairs_edges == 1
    assert stats.stairs_with_count == 1
    assert stats.crossing_edges == 1


# --------------------------------------------------------------------------- #
# 原始数据往返与 CSV
# --------------------------------------------------------------------------- #
def test_save_and_load_raw_roundtrip(tmp_path: Path) -> None:
    """原始 JSON 保存后能完整重建（离线复现）。"""
    network = _network(
        ways={10: ({"highway": "footway"}, [1, 2])},
        nodes={1: (108.9500, 34.2600), 2: (108.9510, 34.2600)},
        rings=[SQUARE],
    )
    path = osm.save_raw(network, tmp_path / "raw.json")
    restored = osm.load_raw(path)

    assert restored.district == network.district
    assert restored.relation_id == network.relation_id
    assert restored.nodes == network.nodes
    assert restored.ways == network.ways
    assert restored.rings == network.rings


def test_write_network_headers_match_sample_data(tmp_path: Path) -> None:
    """写出的表头必须与 data/ 样例 CSV 一致，加载器才能直接读。"""
    node_rows = [{"node_id": "osm_1", "lon": 108.95, "lat": 34.26, "coord_system": "wgs84", "source": "OSM"}]
    edge_rows = [
        {
            "u": "osm_1",
            "v": "osm_2",
            "length_m": 92.2,
            "walk_type": 0,
            "step_count": "",
            "slope_pct": "",
            "surface": "",
            "source": "OSM",
        }
    ]
    nodes_path, edges_path = osm.write_network(node_rows, edge_rows, tmp_path)

    assert nodes_path.read_text(encoding="utf-8").splitlines()[0] == "node_id,lon,lat,coord_system,source"
    assert edges_path.read_text(encoding="utf-8").splitlines()[0] == (
        "u,v,length_m,walk_type,step_count,slope_pct,surface,source,pedestrian_uncertain"
    )
    import csv

    with nodes_path.open(encoding="utf-8") as handle:
        first = next(csv.DictReader(handle))
    assert first["node_id"] == "osm_1"
    assert first["source"] == "OSM"

# --------------------------------------------------------------------------- #
# 修复回归：越界路段 / 缺失多边形 / 部分结果 / 镜像回退
# --------------------------------------------------------------------------- #
def test_build_network_drops_edges_with_both_endpoints_outside() -> None:
    """跨边界道路保留，但两端都在区外的路段必须丢弃（只做碑林区）。"""
    network = _network(
        ways={10: ({"highway": "footway"}, [1, 2])},
        nodes={1: (108.930, 34.260), 2: (108.970, 34.260)},  # 两端外、中点在区内
        rings=[SQUARE],
    )
    _node_rows, edge_rows, stats = osm.build_network(network)

    assert edge_rows == []
    assert stats.edges_outside == 1
    assert stats.ways_outside == 1


def test_build_network_raises_when_polygon_missing() -> None:
    """要求按片区裁剪却没有闭合环时必须报错，不能静默按 bbox 输出。"""
    network = _network(
        ways={10: ({"highway": "footway"}, [1, 2])},
        nodes={1: (108.9500, 34.2600), 2: (108.9510, 34.2600)},
        rings=[],
    )
    with pytest.raises(osm.OsmUnavailable) as excinfo:
        osm.build_network(network, use_polygon=True)
    assert "边界多边形不可用" in str(excinfo.value)
    assert "下一步" in str(excinfo.value)


class _FakeResponse:
    """伪造 urlopen 的返回对象。"""

    def __init__(self, payload) -> None:
        self._payload = payload

    def read(self) -> bytes:
        import json as _json

        return _json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> bool:
        return False


def test_overpass_post_rejects_partial_result_with_remark() -> None:
    """remark 表示部分失败时（即使带部分要素）必须报错，不能生成残缺路网。"""
    def opener(_request, timeout=0):
        return _FakeResponse(
            {
                "elements": [{"type": "node", "id": 1}],
                "remark": "runtime error: Query timed out in \"query\" at line 1.",
            }
        )

    with pytest.raises(osm.OsmUnavailable) as excinfo:
        osm._overpass_post("https://example.invalid", "q", retries=0, opener=opener, sleeper=lambda _s: None)
    assert "不完整" in str(excinfo.value)


def test_overpass_post_accepts_clean_response() -> None:
    """普通响应（无 remark）正常返回。"""

    def opener(_request, timeout=0):
        return _FakeResponse({"elements": [{"type": "node", "id": 1}]})

    data = osm._overpass_post("https://example.invalid", "q", retries=0, opener=opener)
    assert len(data["elements"]) == 1


def test_fetch_district_network_falls_back_to_next_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """第一个镜像能连上但没有数据时，应自动换下一个镜像。"""
    def fake_find(district: str, endpoint: str):
        if "bad" in endpoint:
            raise osm.OsmUnavailable("没有中国数据")
        return 3226088, {"name": district}

    monkeypatch.setattr(osm, "find_district", fake_find)
    monkeypatch.setattr(osm, "district_bounds", lambda _rid, _ep: (108.90, 34.23, 109.00, 34.27))
    monkeypatch.setattr(osm, "_fetch_tile", lambda *_a, **_k: {"elements": []})

    network = osm.fetch_district_network(
        endpoints=("https://bad.invalid/api", "https://good.invalid/api"),
        with_polygon=False,
    )
    assert network.relation_id == 3226088


def test_fetch_district_network_reports_all_endpoints_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """所有镜像都没有数据时，报错信息要列清楚每个镜像的原因。"""
    def fake_find(_district: str, endpoint: str):
        raise osm.OsmUnavailable(f"{endpoint} 无数据")

    monkeypatch.setattr(osm, "find_district", fake_find)
    with pytest.raises(osm.OsmUnavailable) as excinfo:
        osm.fetch_district_network(endpoints=("https://a.invalid", "https://b.invalid"), with_polygon=False)
    assert "所有 Overpass 镜像" in str(excinfo.value)


@pytest.mark.parametrize("foot", ["no", "private", "use_sidepath"])
def test_explicit_foot_denial_is_never_overridden(foot: str) -> None:
    tags = {"highway": "primary", "sidewalk": "both", "foot": foot}
    assert "foot" in osm.classify_way(tags, allow_motor_without_sidewalk=True)[-1]


@pytest.mark.parametrize("name", ["sidewalk:both", "sidewalk:left", "sidewalk:right"])
def test_negative_sidewalk_tags_do_not_allow_motor_road(name: str) -> None:
    assert "机动车道" in osm.classify_way({"highway": "primary", name: "no"})[-1]


@pytest.mark.parametrize("incline", [None, "8%"])
def test_allowed_motor_road_preserves_uncertainty(incline) -> None:
    tags = {"highway": "primary"}
    if incline is not None:
        tags["incline"] = incline
    network = _network({10: (tags, [1, 2])}, {1: (108.95, 34.26), 2: (108.951, 34.26)})
    _, rows, stats = osm.build_network(network, use_polygon=False, allow_motor_without_sidewalk=True)
    assert rows[0]["pedestrian_uncertain"] == 1
    assert stats.ways_motor_unknown_sidewalk == 1


def test_missing_node_does_not_bridge_gap() -> None:
    network = _network(
        {10: ({"highway": "footway"}, [1, 2, 3, 4])},
        {1: (108.95, 34.26), 3: (108.951, 34.26), 4: (108.952, 34.26)},
    )
    _, rows, _ = osm.build_network(network, use_polygon=False)
    assert [(row["u"], row["v"]) for row in rows] == [("osm_3", "osm_4")]


def test_bbox_mode_excludes_outside_and_crossing_segments() -> None:
    network = _network(
        {10: ({"highway": "footway"}, [1, 2, 3, 4])},
        {1: (108.90, 34.26), 2: (108.93, 34.26), 3: (108.95, 34.26), 4: (108.951, 34.26)},
    )
    _, rows, stats = osm.build_network(network, use_polygon=False)
    assert [(row["u"], row["v"]) for row in rows] == [("osm_3", "osm_4")]
    assert stats.edges_outside == 2
