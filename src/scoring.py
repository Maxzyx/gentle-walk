"""路线评分：时间成本 + 台阶/坡度/过街/障碍惩罚 − 休息点/坡道奖励。

权重来源与依据（AGENTS.md 第 42 行）：
- 高德 ``WalkStep.walk_type``：阶梯 21、斜坡 22、人行横道 3、扶梯 9、直梯 10，
  直接映射为台阶/陡坡惩罚项；
- ``step_count`` 等细粒度字段缺失时用「默认中等惩罚值」，并在风险提示标注
  「台阶/坡度细节未标注，以现场为准」；
- 全部系数集中在 ``config/default.json`` 的 ``scoring`` 段，属 MVP 默认值，
  标注「假设/待验证」，需现场调研后校准。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .config import ScoringConfig
from .geo.geo_utils import haversine_m, local_xy_m, point_segment_distance_m, polyline_length_m
from .graph_builder import WalkGraph, edge_key
from .models import (
    WALK_TYPE_LABELS,
    GraphEdge,
    Obstacle,
    Poi,
    RestPoint,
    RouteStats,
    is_crossing,
    is_slope,
    is_stairs,
)

Point = Tuple[float, float]

#: 与路型重复、不再重复计罚的障碍类型（避免台阶被算两次）
_DEDUP_BY_KIND = {
    "stairs": is_stairs,
    "slope": is_slope,
    "crossing": is_crossing,
}


@dataclass(frozen=True)
class EdgeScore:
    """单条边的评分明细。"""

    total: float = 0.0
    time_cost: float = 0.0
    stairs_cost: float = 0.0
    slope_cost: float = 0.0
    crossing_cost: float = 0.0
    obstacle_cost: float = 0.0
    notes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RouteEvaluation:
    """一条路线的评分结果。"""

    cost: float
    stats: RouteStats
    notes: Tuple[str, ...] = ()
    edge_scores: Tuple[EdgeScore, ...] = ()
    data_source: str = "未标注来源"
    data_confidence: str = "low"


# --------------------------------------------------------------------------- #
# 单项成本
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EdgeHazards:
    """道路标签和现场障碍合并，供评分、统计、文字步骤共用。"""

    stairs_segments: int = 0
    stairs_total: int = 0
    stairs_unknown_segments: int = 0
    slope_segments: int = 0
    max_slope_pct: float = 0.0
    slope_unknown_segments: int = 0
    crossings: int = 0


def _known_steps(value: Optional[int]) -> Optional[int]:
    """非法或负的台阶数按未知处理。"""
    if value is None or not math.isfinite(float(value)) or value < 0:
        return None
    return int(value)


def _known_slope(value: Optional[float]) -> Optional[float]:
    """无向步行图中，上下坡按坡度绝对值计罚。"""
    if value is None or not math.isfinite(float(value)):
        return None
    return abs(float(value))


def edge_hazards(edge: GraphEdge, obstacles: Sequence[Obstacle]) -> EdgeHazards:
    """同型道路与挂载障碍采用较保守明细；普通道路的独立障碍分别计数。

    MVP 规则：同型道路与现场记录视为同处，取已知最大值避免重复计数。
    边未标该类型时，不同 id 的障碍各算一处；无法区分位置时需现场复核。
    """
    unique = {obstacle.id: obstacle for obstacle in obstacles}
    counts = [_known_steps(o.step_count) for o in unique.values() if o.type == "stairs"]
    if is_stairs(edge.walk_type):
        known = [v for v in [_known_steps(edge.step_count)] + counts if v is not None]
        counts = [max(known)] if known else [None]
    slopes = [_known_slope(o.slope_pct) for o in unique.values() if o.type == "slope"]
    if is_slope(edge.walk_type):
        known = [v for v in [_known_slope(edge.slope_pct)] + slopes if v is not None]
        slopes = [max(known)] if known else [None]
    crossings = sum(o.type == "crossing" for o in unique.values())
    if is_crossing(edge.walk_type):
        crossings = max(1, crossings)
    return EdgeHazards(
        stairs_segments=len(counts),
        stairs_total=sum(v for v in counts if v is not None),
        stairs_unknown_segments=sum(v is None for v in counts),
        slope_segments=len(slopes),
        max_slope_pct=max((v for v in slopes if v is not None), default=0.0),
        slope_unknown_segments=sum(v is None for v in slopes),
        crossings=crossings,
    )


def time_cost(length_m: float, scoring: ScoringConfig) -> float:
    """按老人慢速步速折算的时间成本（等效分钟）。"""
    speed = scoring.senior_speed_mps if scoring.senior_speed_mps > 0 else 0.9
    return max(0.0, length_m) / speed / 60.0


def stairs_cost(edge: GraphEdge, scoring: ScoringConfig) -> Tuple[float, Tuple[str, ...]]:
    """台阶惩罚：``基础 + 每级 × 级数``；级数缺失用默认中等惩罚。"""
    if not is_stairs(edge.walk_type):
        return 0.0, ()
    steps = _known_steps(edge.step_count)
    if steps is None:
        return scoring.stairs_default_when_missing, (
            "台阶数量未标注，已按默认中等惩罚估算（以现场为准）",
        )
    cost = scoring.stairs_base + scoring.stairs_per_step * steps
    return cost, ()


def slope_cost(edge: GraphEdge, scoring: ScoringConfig) -> Tuple[float, Tuple[str, ...]]:
    """坡度惩罚：``基础 + 每百分点 × 坡度``，陡坡再乘系数；坡度缺失用默认值。"""
    if not is_slope(edge.walk_type):
        return 0.0, ()
    slope = _known_slope(edge.slope_pct)
    if slope is None:
        return scoring.slope_default_when_missing, (
            "坡度未标注，已按默认中等惩罚估算（以现场为准）",
        )
    cost = scoring.slope_base + scoring.slope_per_pct * slope
    if slope >= scoring.slope_steep_pct:
        cost *= scoring.slope_steep_multiplier
    return cost, ()


def crossing_cost(edge: GraphEdge, scoring: ScoringConfig) -> float:
    """过街惩罚：人行横道每次计固定成本。"""
    if not is_crossing(edge.walk_type):
        return 0.0
    return scoring.crossing_cost


def obstacle_penalty(
    obstacles: Sequence[Obstacle], edge: GraphEdge, scoring: ScoringConfig
) -> Tuple[float, Tuple[str, ...]]:
    """障碍点惩罚。

    与路段自身路型重复的障碍（如「台阶」障碍挂在阶梯路段上）不再重复计罚，
    避免同一处台阶被算两次。
    """
    total = 0.0
    notes: List[str] = []
    penalties = list(scoring.obstacle_by_severity) or [2.0, 4.0, 8.0]

    for obstacle in obstacles:
        dedup_check = _DEDUP_BY_KIND.get(obstacle.type)
        if dedup_check is not None and dedup_check(edge.walk_type):
            continue
        index = max(0, min(len(penalties) - 1, int(obstacle.severity) - 1))
        total += penalties[index]
        notes.append(f"障碍「{obstacle.name}」按严重度 {obstacle.severity} 计罚")
    return total, tuple(notes)


def score_edge(
    edge: GraphEdge,
    obstacles: Sequence[Obstacle],
    scoring: ScoringConfig,
) -> EdgeScore:
    """道路类型与现场障碍共用同一份明细，避免漏算或重复计罚。"""
    hazards = edge_hazards(edge, obstacles)
    t_cost = time_cost(edge.length_m, scoring)
    known_stairs = hazards.stairs_segments - hazards.stairs_unknown_segments
    s_cost = (
        known_stairs * scoring.stairs_base
        + hazards.stairs_total * scoring.stairs_per_step
        + hazards.stairs_unknown_segments * scoring.stairs_default_when_missing
    )
    s_notes = ("台阶数量未标注，已按默认中等惩罚估算（以现场为准）",) if hazards.stairs_unknown_segments else ()
    sl_cost = 0.0
    if hazards.slope_segments:
        known_cost = scoring.slope_base + scoring.slope_per_pct * hazards.max_slope_pct
        if hazards.max_slope_pct >= scoring.slope_steep_pct:
            known_cost *= scoring.slope_steep_multiplier
        sl_cost = max(
            known_cost if hazards.slope_segments > hazards.slope_unknown_segments else 0.0,
            scoring.slope_default_when_missing if hazards.slope_unknown_segments else 0.0,
        )
    sl_notes = ("坡度未标注，已按默认中等惩罚估算（以现场为准）",) if hazards.slope_unknown_segments else ()
    c_cost = hazards.crossings * scoring.crossing_cost
    other = [o for o in obstacles if o.type not in _DEDUP_BY_KIND]
    o_cost, o_notes = obstacle_penalty(other, edge, scoring)
    return EdgeScore(
        total=t_cost + s_cost + sl_cost + c_cost + o_cost,
        time_cost=t_cost, stairs_cost=s_cost, slope_cost=sl_cost,
        crossing_cost=c_cost, obstacle_cost=o_cost,
        notes=s_notes + sl_notes + o_notes,
    )


# --------------------------------------------------------------------------- #
# 休息点与疲劳
# --------------------------------------------------------------------------- #
def nearby_rest_points(
    polyline: Sequence[Point], rest_points: Sequence[RestPoint], buffer_m: float
) -> List[RestPoint]:
    """找出折线缓冲区内的休息点。"""
    if not polyline or not rest_points:
        return []
    result: List[RestPoint] = []
    for rest in rest_points:
        if _point_near_polyline(rest.point, polyline, buffer_m):
            result.append(rest)
    return result


def _point_near_polyline(point: Point, polyline: Sequence[Point], buffer_m: float) -> bool:
    """点是否落在折线缓冲区（米）内。"""
    if len(polyline) == 1:
        return haversine_m(*point, *polyline[0]) <= buffer_m
    for index in range(len(polyline) - 1):
        if (
            point_segment_distance_m(point, polyline[index], polyline[index + 1])
            <= buffer_m
        ):
            return True
    return False


def fatigue_segments(
    polyline: Sequence[Point],
    rest_points: Sequence[RestPoint],
    buffer_m: float,
    threshold_m: float,
) -> int:
    """按缓冲圆实际覆盖的沿线区间计算疲劳，不依赖路网节点切分。

    使用片区内统一局部米制坐标，线段与圆的交区间取并集。
    这是启发式休息覆盖规则，不代表休息点实际可达，需现场校准。
    """
    if len(polyline) < 2 or threshold_m <= 0 or not rest_points:
        return 0
    origin = polyline[0]
    points = [local_xy_m(point, origin) for point in polyline]
    centers = [local_xy_m(rest.point, origin) for rest in rest_points]
    radius = max(0.0, buffer_m)
    intervals: List[Tuple[float, float]] = []
    total = 0.0
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        if length <= 1e-9:
            continue
        ux, uy = dx / length, dy / length
        for rx, ry in centers:
            vx, vy = rx - ax, ry - ay
            along = vx * ux + vy * uy
            perpendicular_sq = max(0.0, vx * vx + vy * vy - along * along)
            if perpendicular_sq > radius * radius:
                continue
            reach = math.sqrt(max(0.0, radius * radius - perpendicular_sq))
            begin, end = max(0.0, along - reach), min(length, along + reach)
            if end >= begin:
                intervals.append((total + begin, total + end))
        total += length
    cursor = 0.0
    segments = 0
    for begin, end in sorted(intervals):
        if begin > cursor:
            segments += int(math.floor((begin - cursor + 1e-7) / threshold_m))
        cursor = max(cursor, end)
    return segments + int(math.floor((total - cursor + 1e-7) / threshold_m))


# --------------------------------------------------------------------------- #
# 路线级评估
# --------------------------------------------------------------------------- #
def _path_edges(walk_graph: WalkGraph, node_path: Sequence[str]) -> List[GraphEdge]:
    """把节点序列翻译成边序列；缺边时抛 ValueError（说明下一步）。"""
    edges: List[GraphEdge] = []
    for index in range(len(node_path) - 1):
        key = edge_key(node_path[index], node_path[index + 1])
        edge = walk_graph.edges.get(key)
        if edge is None:
            raise ValueError(
                f"路径包含不存在的边 {key}。下一步：检查路网数据是否完整、"
                "或重新生成路线。"
            )
        edges.append(edge)
    return edges


def evaluate_route(
    walk_graph: WalkGraph,
    node_path: Sequence[str],
    destination: Optional[Poi],
    scoring: ScoringConfig,
) -> RouteEvaluation:
    """对一条候选路线计算总成本与统计量。

    总成本 = Σ 边成本 + 疲劳惩罚 − 休息点奖励 − 目的地坡道奖励 + 无坡道台阶附加。
    """
    edges = _path_edges(walk_graph, node_path)
    polyline = walk_graph.polyline(node_path)

    edge_scores: List[EdgeScore] = []
    notes: List[str] = []

    distance_m = 0.0
    stairs_segments = 0
    stairs_total = 0
    max_slope = 0.0
    crossings = 0
    obstacle_count = 0
    max_obstacle_severity = 0
    no_railing_m = 0.0
    unknown_segments = 0
    stairs_unknown_segments = 0
    slope_segments = 0
    slope_unknown_segments = 0
    pedestrian_uncertain_segments = 0
    data_sources = set()

    for edge in edges:
        obstacles = walk_graph.edge_obstacles(edge.u, edge.v)
        score = score_edge(edge, obstacles, scoring)
        edge_scores.append(score)
        notes.extend(score.notes)

        distance_m += edge.length_m
        hazards = edge_hazards(edge, obstacles)
        stairs_segments += hazards.stairs_segments
        stairs_total += hazards.stairs_total
        stairs_unknown_segments += hazards.stairs_unknown_segments
        slope_segments += hazards.slope_segments
        slope_unknown_segments += hazards.slope_unknown_segments
        max_slope = max(max_slope, hazards.max_slope_pct)
        crossings += hazards.crossings
        pedestrian_uncertain_segments += int(edge.pedestrian_uncertain)
        data_sources.add(edge.source or "未标注来源")
        if edge.walk_type not in WALK_TYPE_LABELS:
            unknown_segments += 1

        for obstacle in obstacles:
            data_sources.add(obstacle.source or "未标注来源")
            obstacle_count += 1
            max_obstacle_severity = max(max_obstacle_severity, int(obstacle.severity))
            if obstacle.type == "no_railing":
                no_railing_m += edge.length_m

    rest_nearby = nearby_rest_points(polyline, walk_graph.rest_points, scoring.rest_buffer_m)
    fatigue = fatigue_segments(
        polyline,
        walk_graph.rest_points,
        scoring.rest_buffer_m,
        scoring.fatigue_threshold_m,
    )
    if not walk_graph.rest_points:
        notes.append("数据中未记录休息点，本次未计入疲劳惩罚（不代表沿途真的没有可歇脚处）")

    rest_bonus = min(
        len(rest_nearby) * scoring.rest_reward_per_point, scoring.rest_reward_cap
    )
    fatigue_cost = fatigue * scoring.fatigue_cost

    ramp_bonus = 0.0
    ramp_penalty = 0.0
    if destination is not None:
        if destination.has_ramp is True:
            ramp_bonus = scoring.ramp_bonus
        elif destination.has_ramp is False and stairs_segments > 0:
            ramp_penalty = scoring.no_ramp_stairs_penalty
            notes.append(
                f"目的地「{destination.name}」无坡道且路线含台阶，附加惩罚"
            )

    base_cost = sum(score.total for score in edge_scores)
    # 奖励可能超过基础成本（例如极短路线旁就有休息点），成本下限归零便于阅读与比较
    cost = max(0.0, base_cost + fatigue_cost - rest_bonus - ramp_bonus + ramp_penalty)

    stats = RouteStats(
        distance_m=round(distance_m, 1),
        duration_min=round(time_cost(distance_m, scoring), 1),
        stairs_segments=stairs_segments,
        stairs_total=stairs_total,
        max_slope_pct=round(max_slope, 1),
        crossings=crossings,
        obstacle_count=obstacle_count,
        max_obstacle_severity=max_obstacle_severity,
        fatigue_segments=fatigue,
        rest_points_nearby=len(rest_nearby),
        no_railing_m=round(no_railing_m, 1),
        unknown_segments=unknown_segments,
        stairs_unknown_segments=stairs_unknown_segments,
        slope_segments=slope_segments,
        slope_unknown_segments=slope_unknown_segments,
        pedestrian_uncertain_segments=pedestrian_uncertain_segments,
    )

    # 去重但保持顺序，避免同一提示重复刷屏
    unique_notes = tuple(dict.fromkeys(notes))
    return RouteEvaluation(
        cost=round(cost, 3),
        stats=stats,
        notes=unique_notes,
        edge_scores=tuple(edge_scores),
        data_source="、".join(sorted(data_sources)) or "未标注来源",
        data_confidence="low",
    )


def polyline_of(walk_graph: WalkGraph, node_path: Sequence[str]) -> Tuple[Point, ...]:
    """节点序列 → 折线（供外部复用，避免重复实现）。"""
    return walk_graph.polyline(node_path)


def total_length_m(polyline: Sequence[Point]) -> float:
    """折线总长（米），供输出与测试使用。"""
    return polyline_length_m(polyline)
