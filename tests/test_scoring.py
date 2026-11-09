"""``src/scoring.py`` 测试：台阶/坡度/过街/障碍惩罚与休息点奖励。"""

from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple

import pytest

from src.config import ScoringConfig, load_config
from src.data_loader import Dataset
from src.graph_builder import WalkGraph, build_graph, edge_key
from src.models import GraphEdge, GraphNode, Obstacle, Poi, RestPoint
from src.scoring import (
    crossing_cost,
    evaluate_route,
    fatigue_segments,
    nearby_rest_points,
    obstacle_penalty,
    score_edge,
    slope_cost,
    stairs_cost,
    time_cost,
)


@pytest.fixture()
def scoring() -> ScoringConfig:
    """项目真实评分配置（只读取）。"""
    return load_config().scoring


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


def _graph(
    edges: Sequence[GraphEdge],
    obstacles: Sequence[Obstacle] = (),
    rest_points: Sequence[RestPoint] = (),
) -> WalkGraph:
    """用给定边构造一个线性路网图（自动补节点坐标）。"""
    node_ids: List[str] = []
    for edge in edges:
        for node_id in (edge.u, edge.v):
            if node_id not in node_ids:
                node_ids.append(node_id)

    # 节点沿经度方向均匀排布，每步约 0.001 度 ≈ 92 米
    nodes = [
        _node(node_id, 108.9450 + index * 0.001, 34.2610)
        for index, node_id in enumerate(node_ids)
    ]
    dataset = Dataset(
        nodes=nodes,
        edges=list(edges),
        obstacles=list(obstacles),
        rest_points=list(rest_points),
    )
    return build_graph(dataset)


def _poi(has_ramp: Optional[bool] = None) -> Poi:
    """构造目的地。"""
    return Poi(
        id="shop_x",
        name="测试菜店",
        category="greengrocer",
        lon=108.9460,
        lat=34.2610,
        has_ramp=has_ramp,
    )


# --------------------------------------------------------------------------- #
# 时间成本
# --------------------------------------------------------------------------- #
def test_time_cost_uses_senior_speed(scoring: ScoringConfig) -> None:
    """90 米按 0.9 m/s 计 100 秒，即 1.667 分钟。"""
    assert time_cost(90.0, scoring) == pytest.approx(90.0 / 0.9 / 60.0)
    assert time_cost(0.0, scoring) == 0.0
    assert time_cost(-10.0, scoring) == 0.0


# --------------------------------------------------------------------------- #
# 台阶
# --------------------------------------------------------------------------- #
def test_stairs_cost_with_step_count(scoring: ScoringConfig) -> None:
    """台阶惩罚 = 基础 0.5 + 0.9 × 级数。"""
    edge = _edge("a", "b", 20, walk_type=21, step_count=8)
    cost, notes = stairs_cost(edge, scoring)
    assert cost == pytest.approx(0.5 + 0.9 * 8)
    assert notes == ()


def test_stairs_cost_default_when_missing(scoring: ScoringConfig) -> None:
    """级数缺失时用默认中等惩罚并提示以现场为准。"""
    edge = _edge("a", "b", 20, walk_type=21)
    cost, notes = stairs_cost(edge, scoring)
    assert cost == pytest.approx(scoring.stairs_default_when_missing)
    assert any("以现场为准" in note for note in notes)


def test_stairs_cost_zero_for_normal_road(scoring: ScoringConfig) -> None:
    """普通步道不产生台阶惩罚。"""
    assert stairs_cost(_edge("a", "b", 20, walk_type=0), scoring) == (0.0, ())


# --------------------------------------------------------------------------- #
# 坡度
# --------------------------------------------------------------------------- #
def test_slope_cost_linear(scoring: ScoringConfig) -> None:
    """缓坡按线性公式计罚。"""
    edge = _edge("a", "b", 20, walk_type=22, slope_pct=6.0)
    cost, notes = slope_cost(edge, scoring)
    assert cost == pytest.approx(3.0 + 0.6 * 6.0)
    assert notes == ()


def test_slope_cost_steep_multiplier(scoring: ScoringConfig) -> None:
    """坡度 ≥ 8% 时乘以陡坡系数。"""
    edge = _edge("a", "b", 20, walk_type=22, slope_pct=9.5)
    cost, _ = slope_cost(edge, scoring)
    assert cost == pytest.approx((3.0 + 0.6 * 9.5) * 1.5)


def test_slope_cost_default_when_missing(scoring: ScoringConfig) -> None:
    """坡度缺失时用默认值并提示。"""
    edge = _edge("a", "b", 20, walk_type=22)
    cost, notes = slope_cost(edge, scoring)
    assert cost == pytest.approx(scoring.slope_default_when_missing)
    assert any("以现场为准" in note for note in notes)


# --------------------------------------------------------------------------- #
# 过街与障碍
# --------------------------------------------------------------------------- #
def test_crossing_cost(scoring: ScoringConfig) -> None:
    """人行横道每次固定惩罚。"""
    assert crossing_cost(_edge("a", "b", 10, walk_type=3), scoring) == pytest.approx(2.5)
    assert crossing_cost(_edge("a", "b", 10, walk_type=0), scoring) == 0.0


def test_obstacle_penalty_by_severity(scoring: ScoringConfig) -> None:
    """障碍惩罚按严重度取 [2,4,8]。"""
    edge = _edge("a", "b", 20, walk_type=0)
    obstacles = [
        Obstacle(id="o1", name="施工", type="construction", lon=108.945, lat=34.261, severity=1),
        Obstacle(id="o2", name="湿滑", type="slippery", lon=108.945, lat=34.261, severity=3),
    ]
    cost, notes = obstacle_penalty(obstacles, edge, scoring)
    assert cost == pytest.approx(2.0 + 8.0)
    assert len(notes) == 2


def test_obstacle_penalty_skips_duplicate_kind(scoring: ScoringConfig) -> None:
    """台阶障碍挂在阶梯路段上时不重复计罚。"""
    stairs_edge = _edge("a", "b", 20, walk_type=21, step_count=8)
    obstacle = Obstacle(
        id="o1", name="台阶", type="stairs", lon=108.945, lat=34.261, severity=2
    )
    cost, _ = obstacle_penalty([obstacle], stairs_edge, scoring)
    assert cost == 0.0


def test_obstacle_penalty_applies_on_other_road(scoring: ScoringConfig) -> None:
    """台阶障碍挂在普通步道上时正常计罚。"""
    normal_edge = _edge("a", "b", 20, walk_type=0)
    obstacle = Obstacle(
        id="o1", name="台阶", type="stairs", lon=108.945, lat=34.261, severity=2
    )
    cost, _ = obstacle_penalty([obstacle], normal_edge, scoring)
    assert cost == pytest.approx(4.0)


def test_score_edge_components(scoring: ScoringConfig) -> None:
    """边评分明细各项相加等于总分。"""
    edge = _edge("a", "b", 92, walk_type=21, step_count=5)
    obstacle = Obstacle(
        id="o1", name="无护栏", type="no_railing", lon=108.945, lat=34.261, severity=1
    )
    score = score_edge(edge, [obstacle], scoring)

    assert score.total == pytest.approx(
        score.time_cost + score.stairs_cost + score.slope_cost
        + score.crossing_cost + score.obstacle_cost
    )
    assert score.stairs_cost > 0
    assert score.obstacle_cost == pytest.approx(2.0)


# --------------------------------------------------------------------------- #
# 休息点与疲劳
# --------------------------------------------------------------------------- #
def test_nearby_rest_points_detects_buffer(scoring: ScoringConfig) -> None:
    """折线 200 米缓冲内的休息点被识别。"""
    polyline = [(108.9450, 34.2610), (108.9470, 34.2610)]
    near = RestPoint(id="r1", name="近处", type="bench", lon=108.9460, lat=34.2611)
    far = RestPoint(id="r2", name="远处", type="bench", lon=108.9500, lat=34.2611)

    found = nearby_rest_points(polyline, [near, far], 200.0)

    assert [item.id for item in found] == ["r1"]


def test_fatigue_segments_without_rest(scoring: ScoringConfig) -> None:
    """有休息点数据但沿途都不在缓冲区时，按 200 米阈值累计段数。"""
    polyline = [(108.9450, 34.2610), (108.9490, 34.2610)]  # 约 368 米
    far_rest = [
        RestPoint(id="r1", name="远处休息点", type="bench", lon=108.9600, lat=34.2610)
    ]
    segments = fatigue_segments(polyline, far_rest, 200.0, 200.0)
    assert segments == 1


def test_fatigue_zero_when_no_rest_data(scoring: ScoringConfig) -> None:
    """完全没有休息点数据时不判疲劳（数据缺失，而非路线问题）。"""
    polyline = [(108.9450, 34.2610), (108.9490, 34.2610)]
    assert fatigue_segments(polyline, [], 200.0, 200.0) == 0


def test_fatigue_segments_resets_near_rest(scoring: ScoringConfig) -> None:
    """途经休息点时累加距离归零。"""
    polyline = [
        (108.9450, 34.2610),
        (108.9460, 34.2610),
        (108.9480, 34.2610),
    ]
    # 休息点在中点附近，第二段开始前累加被清零
    rest = RestPoint(id="r1", name="中途", type="bench", lon=108.9460, lat=34.2610)
    segments = fatigue_segments(polyline, [rest], 200.0, 200.0)
    assert segments == 0


def test_fatigue_zero_for_short_route(scoring: ScoringConfig) -> None:
    """短于阈值的路线不计疲劳。"""
    polyline = [(108.9450, 34.2610), (108.9455, 34.2610)]
    assert fatigue_segments(polyline, [], 200.0, 200.0) == 0


# --------------------------------------------------------------------------- #
# 路线评估
# --------------------------------------------------------------------------- #
def test_evaluate_route_counts_stairs_and_crossings(scoring: ScoringConfig) -> None:
    """路线统计台阶段数、级数、过街次数与总长。"""
    edges = [
        _edge("n1", "n2", 92, walk_type=21, step_count=8),
        _edge("n2", "n3", 92, walk_type=3),
        _edge("n3", "n4", 92, walk_type=0),
    ]
    walk_graph = _graph(edges)

    evaluation = evaluate_route(walk_graph, ["n1", "n2", "n3", "n4"], _poi(), scoring)

    assert evaluation.stats.distance_m == pytest.approx(276.0)
    assert evaluation.stats.stairs_segments == 1
    assert evaluation.stats.stairs_total == 8
    assert evaluation.stats.crossings == 1
    assert evaluation.stats.duration_min == pytest.approx(276.0 / 0.9 / 60.0, rel=0.01)


def test_evaluate_route_max_slope(scoring: ScoringConfig) -> None:
    """最大坡度取路线中最大值。"""
    edges = [
        _edge("n1", "n2", 92, walk_type=22, slope_pct=6.0),
        _edge("n2", "n3", 92, walk_type=22, slope_pct=9.5),
    ]
    walk_graph = _graph(edges)

    evaluation = evaluate_route(walk_graph, ["n1", "n2", "n3"], _poi(), scoring)

    assert evaluation.stats.max_slope_pct == pytest.approx(9.5)


def test_evaluate_route_rest_bonus_and_cap(scoring: ScoringConfig) -> None:
    """休息点奖励按个计，且有封顶。"""
    edges = [_edge("n1", "n2", 92, walk_type=0)]
    rest_points = [
        RestPoint(id=f"r{i}", name=f"休息{i}", type="bench", lon=108.9450, lat=34.2610)
        for i in range(10)
    ]
    walk_graph = _graph(edges, rest_points=rest_points)

    evaluation = evaluate_route(walk_graph, ["n1", "n2"], _poi(), scoring)

    assert evaluation.stats.rest_points_nearby == 10
    base = evaluation.edge_scores[0].total
    # 奖励可能超过基础成本，成本下限归零
    assert evaluation.cost == pytest.approx(
        max(0.0, base - min(10 * scoring.rest_reward_per_point, scoring.rest_reward_cap)),
        abs=0.01,
    )


def test_evaluate_route_ramp_bonus(scoring: ScoringConfig) -> None:
    """目的地有坡道时给予奖励（成本更低）。"""
    edges = [_edge("n1", "n2", 92, walk_type=0)]
    walk_graph = _graph(edges)

    with_ramp = evaluate_route(walk_graph, ["n1", "n2"], _poi(has_ramp=True), scoring)
    without = evaluate_route(walk_graph, ["n1", "n2"], _poi(has_ramp=None), scoring)

    assert with_ramp.cost < without.cost
    assert without.cost - with_ramp.cost == pytest.approx(scoring.ramp_bonus, abs=0.01)


def test_evaluate_route_no_ramp_with_stairs_penalty(scoring: ScoringConfig) -> None:
    """目的地无坡道且路线含台阶时附加惩罚。"""
    edges = [_edge("n1", "n2", 92, walk_type=21, step_count=3)]
    walk_graph = _graph(edges)

    evaluation = evaluate_route(walk_graph, ["n1", "n2"], _poi(has_ramp=False), scoring)

    assert any("无坡道" in note for note in evaluation.notes)
    assert evaluation.cost == pytest.approx(
        evaluation.edge_scores[0].total + scoring.no_ramp_stairs_penalty, abs=0.01
    )


def test_evaluate_route_no_railing_length(scoring: ScoringConfig) -> None:
    """无护栏路段长度被累计。"""
    edges = [_edge("n1", "n2", 92, walk_type=0)]
    obstacle = Obstacle(
        id="o1", name="无护栏", type="no_railing", lon=108.9450, lat=34.2610, severity=1
    )
    walk_graph = _graph(edges, obstacles=[obstacle])

    evaluation = evaluate_route(walk_graph, ["n1", "n2"], _poi(), scoring)

    assert evaluation.stats.no_railing_m == pytest.approx(92.0)


def test_evaluate_route_unknown_walk_type_counted(scoring: ScoringConfig) -> None:
    """未识别路型被计入统计，供风险提示使用。"""
    edges = [_edge("n1", "n2", 92, walk_type=99)]
    walk_graph = _graph(edges)

    evaluation = evaluate_route(walk_graph, ["n1", "n2"], _poi(), scoring)

    assert evaluation.stats.unknown_segments == 1


def test_evaluate_route_missing_edge_raises(scoring: ScoringConfig) -> None:
    """路径包含不存在的边时给出中文错误与下一步。"""
    walk_graph = _graph([_edge("n1", "n2", 92)])

    with pytest.raises(ValueError) as excinfo:
        evaluate_route(walk_graph, ["n1", "n3"], _poi(), scoring)
    assert "下一步" in str(excinfo.value)


def test_evaluate_route_prefers_flat_alternative(scoring: ScoringConfig) -> None:
    """同等距离下，平缓路线成本应低于含台阶路线。"""
    flat = _graph([_edge("n1", "n2", 100, walk_type=0)])
    stairs = _graph([_edge("n1", "n2", 100, walk_type=21, step_count=10)])

    flat_eval = evaluate_route(flat, ["n1", "n2"], _poi(), scoring)
    stairs_eval = evaluate_route(stairs, ["n1", "n2"], _poi(), scoring)

    assert flat_eval.cost < stairs_eval.cost



def test_obstacle_steps_count_and_do_not_duplicate(scoring: ScoringConfig) -> None:
    from src.scoring import edge_hazards
    obstacle = Obstacle("o", "现场台阶", "stairs", 108.9455, 34.261, step_count=20, source="现场")
    normal = _edge("a", "b", 92)
    assert edge_hazards(normal, [obstacle]).stairs_total == 20
    assert score_edge(normal, [obstacle], scoring).stairs_cost == pytest.approx(18.5)
    stairs = _edge("a", "b", 92, 21, step_count=8)
    assert edge_hazards(stairs, [obstacle]).stairs_total == 20


def test_negative_slope_is_absolute(scoring: ScoringConfig) -> None:
    assert slope_cost(_edge("a", "b", 92, 22, slope_pct=-8), scoring) == slope_cost(
        _edge("a", "b", 92, 22, slope_pct=8), scoring
    )


def test_unknown_hazards_and_motor_risk_are_preserved(scoring: ScoringConfig) -> None:
    from src.risk import build_risks
    from src.config import RiskConfig
    from src.router import stairs_text, slope_text
    graph = _graph([GraphEdge("a", "b", 92, 21, pedestrian_uncertain=True),
                    GraphEdge("b", "c", 92, 22)])
    evaluation = evaluate_route(graph, ["a", "b", "c"], _poi(), scoring)
    assert evaluation.stats.stairs_unknown_segments == 1
    assert evaluation.stats.slope_unknown_segments == 1
    assert "未知" in stairs_text(evaluation.stats)
    assert "未知" in slope_text(evaluation.stats)
    risks = build_risks(evaluation.stats, _poi(), RiskConfig())
    assert any(r.title == "有坡道/斜坡" and r.level == "中" for r in risks)
    assert any("人行道未确认" in r.title for r in risks)


def test_fatigue_is_independent_of_node_segmentation(scoring: ScoringConfig) -> None:
    long = [(108.94, 34.261), (108.962, 34.261)]
    split = [(108.94 + i * .001, 34.261) for i in range(23)]
    rest = [RestPoint("r", "起点长椅", "bench", 108.94, 34.261)]
    expected = fatigue_segments(long, rest, 200, 200)
    assert expected > 0
    assert fatigue_segments(split, rest, 200, 200) == expected
