"""``src/router.py`` 与 ``src/risk.py`` 测试：主路线、备选、无路线、风险分级。"""

from __future__ import annotations

from typing import List, Optional, Sequence

import pytest

from src.config import AppConfig, load_config
from src.data_loader import Dataset
from src.graph_builder import build_graph
from src.models import GraphEdge, GraphNode, Obstacle, Poi, RestPoint, RouteStats
from src.risk import build_risks, classify_risk, risk_summary
from src.router import (
    MAX_ALTERNATIVES,
    SNAP_MAX_M,
    build_steps,
    describe_route,
    direction_label,
    plan_routes,
)


@pytest.fixture()
def config() -> AppConfig:
    """真实配置（只读取）。"""
    return load_config()


def _node(node_id: str, lon: float, lat: float) -> GraphNode:
    """构造节点。"""
    return GraphNode(node_id=node_id, lon=lon, lat=lat, coord_system="wgs84")


def _edge(
    u: str,
    v: str,
    length_m: float,
    walk_type: int = 0,
    step_count: Optional[int] = None,
    slope_pct: Optional[float] = None,
) -> GraphEdge:
    """构造边。"""
    return GraphEdge(
        u=u,
        v=v,
        length_m=length_m,
        walk_type=walk_type,
        step_count=step_count,
        slope_pct=slope_pct,
    )


def _poi(lon: float = 108.9460, lat: float = 34.2610, has_ramp: Optional[bool] = True) -> Poi:
    """构造目的地。"""
    return Poi(
        id="shop_x",
        name="测试菜店",
        category="greengrocer",
        lon=lon,
        lat=lat,
        has_ramp=has_ramp,
        source="样例合成",
        confidence="low",
    )


def _two_path_graph() -> object:
    """构造「短但有台阶」与「长但平缓」两条路径。"""
    nodes = [
        _node("A", 108.9440, 34.2610),
        _node("B", 108.9450, 34.2610),
        _node("C", 108.9460, 34.2610),
        _node("D", 108.9440, 34.2620),
        _node("E", 108.9460, 34.2620),
    ]
    edges = [
        _edge("A", "B", 92, walk_type=21, step_count=8),
        _edge("B", "C", 92),
        _edge("A", "D", 111),
        _edge("D", "E", 184),
        _edge("E", "C", 111),
    ]
    return build_graph(Dataset(nodes=nodes, edges=edges))


# --------------------------------------------------------------------------- #
# 方向与步骤
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("end", "expected"),
    [
        ((108.9460, 34.2610), "东"),
        ((108.9440, 34.2610), "西"),
        ((108.9450, 34.2620), "北"),
        ((108.9450, 34.2600), "南"),
        ((108.9460, 34.2620), "东北"),
    ],
)
def test_direction_label(end, expected: str) -> None:
    """八方向判定。"""
    assert direction_label((108.9450, 34.2610), end) == expected


def test_build_steps_describes_stairs_and_crossing(config: AppConfig) -> None:
    """步骤文字包含方向、距离与路型说明。"""
    nodes = [
        _node("A", 108.9440, 34.2610),
        _node("B", 108.9450, 34.2610),
        _node("C", 108.9460, 34.2610),
    ]
    edges = [_edge("A", "B", 92, walk_type=21, step_count=8), _edge("B", "C", 92, walk_type=3)]
    walk_graph = build_graph(Dataset(nodes=nodes, edges=edges))

    steps = build_steps(walk_graph, ["A", "B", "C"], config)

    assert len(steps) == 2
    assert "阶梯" in steps[0].instruction and "8" in steps[0].instruction
    assert "人行横道" in steps[1].instruction
    assert steps[1].note  # 过街提示


# --------------------------------------------------------------------------- #
# 主路线与备选
# --------------------------------------------------------------------------- #
def test_primary_route_prefers_flat_even_if_longer(config: AppConfig) -> None:
    """平缓优先：宁可绕远也不走台阶。"""
    walk_graph = _two_path_graph()
    destination = _poi()

    outcome = plan_routes(walk_graph, (108.9440, 34.2610), destination, config)

    assert outcome.ok
    primary = outcome.routes[0]
    assert primary.is_primary is True
    assert primary.stats.stairs_total == 0
    assert primary.stats.distance_m > 184.0  # 确实走了更长的平缓路线


def test_routes_sorted_by_cost_and_limited(config: AppConfig) -> None:
    """返回路线按成本升序，数量不超过 1 + MAX_ALTERNATIVES。"""
    walk_graph = _two_path_graph()

    outcome = plan_routes(walk_graph, (108.9440, 34.2610), _poi(), config)

    assert 1 <= len(outcome.routes) <= 1 + MAX_ALTERNATIVES
    costs = [route.cost for route in outcome.routes]
    assert costs == sorted(costs)
    assert outcome.routes[0].is_primary


def test_single_route_adds_no_alternative_note(config: AppConfig) -> None:
    """只有一条可行路线时提示没有备选。"""
    nodes = [_node("A", 108.9440, 34.2610), _node("B", 108.9450, 34.2610)]
    edges = [_edge("A", "B", 92)]
    walk_graph = build_graph(Dataset(nodes=nodes, edges=edges))

    outcome = plan_routes(walk_graph, (108.9440, 34.2610), _poi(108.9450, 34.2610), config)

    assert len(outcome.routes) == 1
    assert any("没有备选" in note for note in outcome.notes)


def test_direct_route_when_same_node(config: AppConfig) -> None:
    """同一吸附节点不代表已有可步行连接。"""
    nodes = [_node("A", 108.9440, 34.2610)]
    walk_graph = build_graph(Dataset(nodes=nodes, edges=[]))

    outcome = plan_routes(walk_graph, (108.9440, 34.2610), _poi(108.9441, 34.2611), config)

    assert outcome.ok
    assert "尚无已验证接驳路线" in outcome.routes[0].steps[0].instruction
    assert outcome.routes[0].stats.distance_m > 0


# --------------------------------------------------------------------------- #
# 异常与降级
# --------------------------------------------------------------------------- #
def test_empty_graph_returns_note(config: AppConfig) -> None:
    """空路网不抛异常，返回可操作提示。"""
    walk_graph = build_graph(Dataset())

    outcome = plan_routes(walk_graph, (108.9440, 34.2610), _poi(), config)

    assert outcome.ok is False
    assert any("下一步" in note for note in outcome.notes)


def test_no_path_returns_note(config: AppConfig) -> None:
    """路网不连通时给出「未找到可行路线」。"""
    nodes = [
        _node("A", 108.9440, 34.2610),
        _node("B", 108.9450, 34.2610),
        _node("X", 108.9500, 34.2650),
        _node("Y", 108.9510, 34.2650),
    ]
    edges = [_edge("A", "B", 92), _edge("X", "Y", 92)]
    walk_graph = build_graph(Dataset(nodes=nodes, edges=edges))

    outcome = plan_routes(walk_graph, (108.9440, 34.2610), _poi(108.9500, 34.2650), config)

    assert outcome.ok is False
    assert any("未找到" in note for note in outcome.notes)


def test_origin_too_far_from_network(config: AppConfig) -> None:
    """起点离路网超过阈值时给出提示。"""
    nodes = [_node("A", 108.9440, 34.2610), _node("B", 108.9450, 34.2610)]
    walk_graph = build_graph(Dataset(nodes=nodes, edges=[_edge("A", "B", 92)]))

    far = (108.9440 + SNAP_MAX_M / 90000.0 + 0.01, 34.2610)
    outcome = plan_routes(walk_graph, far, _poi(108.9450, 34.2610), config)

    assert outcome.ok is False
    assert any("起点" in note for note in outcome.notes)


def test_destination_too_far_from_network(config: AppConfig) -> None:
    """目的地离路网超过阈值时给出提示。"""
    nodes = [_node("A", 108.9440, 34.2610), _node("B", 108.9450, 34.2610)]
    walk_graph = build_graph(Dataset(nodes=nodes, edges=[_edge("A", "B", 92)]))

    far_poi = _poi(108.9440 + SNAP_MAX_M / 90000.0 + 0.01, 34.2610)
    outcome = plan_routes(walk_graph, (108.9440, 34.2610), far_poi, config)

    assert outcome.ok is False
    assert any("目的地" in note for note in outcome.notes)


# --------------------------------------------------------------------------- #
# 风险
# --------------------------------------------------------------------------- #
def test_classify_risk_levels(config: AppConfig) -> None:
    """风险分级阈值：无台阶低、台阶中、台阶多高。"""
    low = RouteStats(distance_m=100, stairs_total=0, max_slope_pct=0.0, crossings=0)
    medium = RouteStats(distance_m=100, stairs_total=4, max_slope_pct=0.0, crossings=0)
    high_stairs = RouteStats(distance_m=100, stairs_total=20, max_slope_pct=0.0)
    high_slope = RouteStats(distance_m=100, stairs_total=0, max_slope_pct=9.0)

    assert classify_risk(low, config.risk) == "低"
    assert classify_risk(medium, config.risk) == "中"
    assert classify_risk(high_stairs, config.risk) == "高"
    assert classify_risk(high_slope, config.risk) == "高"


def test_build_risks_mentions_stairs_and_source(config: AppConfig) -> None:
    """台阶风险带来源与置信度，便于判断可信度。"""
    stats = RouteStats(distance_m=200, stairs_segments=1, stairs_total=8, max_slope_pct=0.0)

    risks = build_risks(stats, _poi(has_ramp=True), config.risk, data_source="样例合成")

    titles = [risk.title for risk in risks]
    assert "有台阶" in titles
    stair_risk = next(risk for risk in risks if risk.title == "有台阶")
    assert stair_risk.source == "样例合成"
    assert stair_risk.confidence == "low"
    assert "8" in stair_risk.detail


def test_build_risks_fallback_when_clean(config: AppConfig) -> None:
    """无任何障碍时给出兜底提示而不是空列表。"""
    stats = RouteStats(distance_m=120)

    risks = build_risks(stats, _poi(has_ramp=True), config.risk)

    assert len(risks) == 1
    assert risks[0].level == "低"
    assert "现场" in risks[0].detail


def test_build_risks_no_ramp_destination(config: AppConfig) -> None:
    """目的地无坡道时提示家属陪同。"""
    stats = RouteStats(distance_m=200, stairs_segments=1, stairs_total=3)

    risks = build_risks(stats, _poi(has_ramp=False), config.risk)

    assert any("无坡道" in risk.title for risk in risks)


def test_build_risks_sorted_by_level(config: AppConfig) -> None:
    """风险按 高→中→低 排序。"""
    stats = RouteStats(
        distance_m=800,
        stairs_segments=2,
        stairs_total=25,
        max_slope_pct=9.0,
        crossings=5,
        no_railing_m=30.0,
        fatigue_segments=2,
        obstacle_count=2,
        max_obstacle_severity=3,
    )

    risks = build_risks(stats, _poi(), config.risk)

    order = {"高": 0, "中": 1, "低": 2}
    levels = [order[risk.level] for risk in risks]
    assert levels == sorted(levels)


def test_risk_summary_counts(config: AppConfig) -> None:
    """摘要统计各等级数量。"""
    stats = RouteStats(distance_m=200, stairs_segments=1, stairs_total=4)
    risks = build_risks(stats, _poi(), config.risk)

    summary = risk_summary(risks)

    assert "风险" in summary


def test_describe_route_contains_key_numbers(config: AppConfig) -> None:
    """文字路线包含距离、时间与步骤。"""
    walk_graph = _two_path_graph()

    outcome = plan_routes(walk_graph, (108.9440, 34.2610), _poi(), config)
    lines = describe_route(outcome.routes[0])

    text = "\n".join(lines)
    assert "总距离约" in text
    assert "预计步行" in text
    assert "路线步骤" in text
    assert "评分" in text


def test_unverified_destination_connector_is_counted(config: AppConfig) -> None:
    nodes = [_node("A", 108.94, 34.261), _node("B", 108.941, 34.261)]
    graph = build_graph(Dataset(nodes=nodes, edges=[_edge("A", "B", 92)]))
    route = plan_routes(graph, (108.94, 34.261), _poi(108.946, 34.261), config).routes[0]
    assert route.stats.unverified_connector_m > 450
    assert route.stats.distance_m > 540
    assert route.steps[-1].kind_label == "未验证接驳"
    assert any("接驳" in risk.title for risk in route.risks)
    assert route.polyline[-1] == (108.941, 34.261)  # 不凭空画出穿墙直线


def test_same_node_does_not_claim_nearby_or_negative_cost(config: AppConfig) -> None:
    graph = build_graph(Dataset(nodes=[_node("A", 108.945, 34.261)]))
    route = plan_routes(graph, (108.94, 34.261), _poi(108.95, 34.261, has_ramp=True), config).routes[0]
    assert route.distance_m > 900
    assert "就在附近" not in route.steps[0].instruction
    assert route.steps[0].walk_type == -1
    assert route.cost >= 0


def test_osm_risk_uses_route_source(config: AppConfig) -> None:
    from src.models import GraphEdge
    graph = build_graph(Dataset(
        nodes=[_node("a", 108.94, 34.261), _node("b", 108.941, 34.261)],
        edges=[GraphEdge("a", "b", 92, 21, 12, source="OSM")],
    ))
    route = plan_routes(graph, (108.94, 34.261), _poi(108.941, 34.261), config).routes[0]
    risk = next(risk for risk in route.risks if risk.title == "有台阶")
    assert risk.source == "OSM"
    assert risk.confidence == "low"



def test_same_node_preserves_destination_ramp_warning(config: AppConfig) -> None:
    graph = build_graph(Dataset(nodes=[_node("a", 108.94, 34.261)]))
    route = plan_routes(graph, (108.94, 34.261), _poi(108.94, 34.261, has_ramp=False), config).routes[0]
    assert any(risk.title == "目的地入口无坡道" for risk in route.risks)
    assert not any(risk.title == "起终点接驳路段未验证" for risk in route.risks)


def test_steps_use_merged_hazard_values(config: AppConfig) -> None:
    from src.models import Obstacle
    graph = build_graph(Dataset(
        nodes=[_node("a", 108.94, 34.261), _node("b", 108.941, 34.261)],
        edges=[_edge("a", "b", 92, 21, step_count=8)],
        obstacles=[Obstacle("o", "现场阶梯", "stairs", 108.9405, 34.261, step_count=20)],
    ))
    route = plan_routes(graph, (108.94, 34.261), _poi(108.941, 34.261), config).routes[0]
    assert "20 级" in route.steps[0].instruction
    assert "8 级" not in route.steps[0].instruction
    assert route.stats.stairs_total == 20
