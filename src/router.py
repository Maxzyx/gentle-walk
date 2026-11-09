"""路线规划：``networkx`` 加权最短路 + 备选方案。

遵循 AGENTS.md「不自研寻路算法」：最短路与候选路径生成全部交给 networkx，
本模块只负责「把评分变成权重、把节点序列变成可读步骤、把备选方案排序」。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import networkx as nx

from .config import AppConfig
from .geo.geo_utils import haversine_m, local_xy_m
from .graph_builder import WalkGraph, edge_key, nearest_node
from .models import (
    Poi,
    RiskItem,
    Route,
    RouteStats,
    RouteStep,
    is_crossing,
    is_slope,
    is_stairs,
    walk_type_label,
)
from .risk import build_risks
from .scoring import edge_hazards, evaluate_route, score_edge, time_cost

#: 起点/目的地吸附到路网的最大距离（米）；超出视为「离路网太远」
SNAP_MAX_M = 500.0
#: 参与排序的候选路径上限
MAX_CANDIDATES = 3
#: 除主路线外最多给出的备选数量
MAX_ALTERNATIVES = 2

#: 八方向中文名
_DIRECTION_LABELS = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")


@dataclass
class PlanOutcome:
    """一次规划的结果。"""

    routes: List[Route] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """是否至少有一条路线。"""
        return bool(self.routes)


# --------------------------------------------------------------------------- #
# 方向与步骤
# --------------------------------------------------------------------------- #
def direction_label(
    start: Tuple[float, float], end: Tuple[float, float]
) -> str:
    """根据两点坐标给出八方向中文描述（正北为 0 度、顺时针）。"""
    x, y = local_xy_m(end, start)  # x=东向、y=北向（米）
    if abs(x) < 1e-6 and abs(y) < 1e-6:
        return ""
    bearing = math.degrees(math.atan2(x, y)) % 360.0
    index = int((bearing + 22.5) // 45) % 8
    return _DIRECTION_LABELS[index]


def _describe_edge(
    walk_graph: WalkGraph, edge_u: str, edge_v: str
) -> Tuple[str, str]:
    """把一条边翻译成（动作描述, 附加说明）。"""
    edge = walk_graph.edges[edge_key(edge_u, edge_v)]
    start = walk_graph.point_of(edge_u)
    end = walk_graph.point_of(edge_v)
    direction = direction_label(start, end)
    distance = int(round(edge.length_m))

    prefix = f"{direction}向" if direction else "继续"
    hazards = edge_hazards(edge, walk_graph.edge_obstacles(edge_u, edge_v))
    descriptions: List[str] = []
    if hazards.stairs_segments:
        detail = f"阶梯，已知台阶 {hazards.stairs_total} 级" if hazards.stairs_total else "阶梯，级数未标注"
        if hazards.stairs_unknown_segments:
            detail += f"，{hazards.stairs_unknown_segments} 段级数未知"
        descriptions.append(detail)
    if hazards.slope_segments:
        detail = f"已知最大坡度 {hazards.max_slope_pct:.1f}%" if hazards.max_slope_pct else "有坡道"
        if hazards.slope_unknown_segments:
            detail += f"，{hazards.slope_unknown_segments} 段坡度未知"
        descriptions.append(detail)
    if hazards.crossings:
        descriptions.append(f"经人行横道过街 {hazards.crossings} 次" if is_crossing(edge.walk_type) else f"需过街 {hazards.crossings} 次，请确认人行横道")
    if edge.walk_type in (9, 10):
        descriptions.append(f"可乘{'扶梯' if edge.walk_type == 9 else '直梯'}")
    instruction = f"{prefix}步行约 {distance} 米"
    if descriptions:
        instruction += "（" + "；".join(descriptions) + "）"
    notes = []
    if hazards.stairs_segments or hazards.slope_segments:
        notes.append("台阶/坡度细节以现场为准")
    if hazards.crossings:
        notes.append("请确认并走人行横道，留意右转车辆")
    if edge.pedestrian_uncertain:
        notes.append("人行道未确认，不要直接沿车行道行走，以现场为准")
    if edge.walk_type not in (0, 3, 9, 10, 21, 22):
        notes.append(f"路型：{walk_type_label(edge.walk_type)}")
    return instruction, "；".join(notes)


def build_steps(
    walk_graph: WalkGraph, node_path: Sequence[str], config: AppConfig
) -> Tuple[RouteStep, ...]:
    """把节点序列转换为逐段文字步骤。"""
    steps: List[RouteStep] = []
    for index in range(len(node_path) - 1):
        u, v = node_path[index], node_path[index + 1]
        edge = walk_graph.edges[edge_key(u, v)]
        obstacles = walk_graph.edge_obstacles(u, v)
        score = score_edge(edge, obstacles, config.scoring)
        instruction, note = _describe_edge(walk_graph, u, v)
        steps.append(
            RouteStep(
                index=index + 1,
                instruction=instruction,
                distance_m=edge.length_m,
                walk_type=edge.walk_type,
                kind_label=walk_type_label(edge.walk_type),
                penalty=round(score.total, 2),
                note=note,
            )
        )
    return tuple(steps)


# --------------------------------------------------------------------------- #
# 规划
# --------------------------------------------------------------------------- #
def _edge_costs(walk_graph: WalkGraph, config: AppConfig) -> Dict[Tuple[str, str], float]:
    """预先算好每条边的权重，供 networkx 最短路使用。"""
    costs: Dict[Tuple[str, str], float] = {}
    for key, edge in walk_graph.edges.items():
        obstacles = walk_graph.edge_obstacles(edge.u, edge.v)
        costs[key] = score_edge(edge, obstacles, config.scoring).total
    return costs


def stairs_text(stats: RouteStats) -> str:
    """数量未知不能显示为没有台阶。"""
    if stats.stairs_unknown_segments:
        return f"已知 {stats.stairs_total} 级，另 {stats.stairs_unknown_segments} 段未知"
    return f"{stats.stairs_total} 级"


def slope_text(stats: RouteStats) -> str:
    """保留未知坡度，避免将缺失显示成 0%。"""
    if stats.slope_unknown_segments:
        if stats.slope_segments == stats.slope_unknown_segments:
            return f"未知（{stats.slope_unknown_segments} 段）"
        return f"已知最大 {stats.max_slope_pct:.1f}%，另 {stats.slope_unknown_segments} 段未知"
    return f"{stats.max_slope_pct:.1f}%"


def _connector_risk(distance: float) -> RiskItem:
    """吸附只是几何近邻，不证明两点之间存在可步行连接。"""
    return RiskItem(
        level="中", title="起终点接驳路段未验证",
        detail=(
            f"路网以外的接驳直线距离合计约 {distance:.0f} 米，实际距离可能更长；"
            "未验证围墙、机动车道、台阶或入口。下一步：现场确认可步行连接，"
            "不能把直线距离当作导航，以现场为准。"
        ),
        source="坐标估算（未验证接驳）", confidence="low",
    )


def _attach_connectors(
    route: Route, origin: Tuple[float, float], config: AppConfig
) -> Route:
    """把未验证接驳纳入估计距离/成本和提示，地图仍只画已知路网。"""
    start_gap = haversine_m(*origin, *route.polyline[0])
    end_gap = haversine_m(*route.polyline[-1], *route.destination.point)
    gap = start_gap + end_gap
    if gap <= 1e-6:
        return route
    steps = list(route.steps)
    if start_gap > 1e-6:
        steps.insert(0, RouteStep(
            index=0,
            instruction=f"起点距路网起点直线约 {start_gap:.0f} 米；接驳未验证，请先确认可步行连接",
            distance_m=start_gap, walk_type=-1, kind_label="未验证接驳", note="以现场为准",
        ))
    if end_gap > 1e-6:
        steps.append(RouteStep(
            index=0,
            instruction=f"路网终点距目的地直线约 {end_gap:.0f} 米；入口与接驳未验证，请现场确认",
            distance_m=end_gap, walk_type=-1, kind_label="未验证接驳", note="以现场为准",
        ))
    stats = replace(
        route.stats,
        distance_m=round(route.stats.distance_m + gap, 1),
        duration_min=round(route.stats.duration_min + time_cost(gap, config.scoring), 1),
        unverified_connector_m=round(gap, 1),
    )
    risks = sorted((*route.risks, _connector_risk(gap)), key=lambda risk: {"高": 0, "中": 1, "低": 2}[risk.level])
    return replace(
        route, steps=tuple(replace(step, index=i) for i, step in enumerate(steps, 1)),
        stats=stats, cost=round(max(0.0, route.cost) + time_cost(gap, config.scoring), 3),
        risks=tuple(risks),
    )


def _direct_route(
    walk_graph: WalkGraph,
    origin: Tuple[float, float],
    node_id: str,
    destination: Poi,
    config: AppConfig,
) -> Route:
    """起点与目的地吸附到同一节点时的「已到附近」路线。"""
    distance = haversine_m(*origin, *destination.point)
    stats = RouteStats(
        distance_m=round(distance, 1),
        duration_min=round(time_cost(distance, config.scoring), 1),
        unverified_connector_m=round(distance, 1),
    )
    # 同一节点不证明入口可达，仍保留目的地自身的坡道风险。
    risks = build_risks(
        stats, destination, config.risk, data_source="未验证接驳", data_confidence="low",
    )
    if distance > 1e-6:
        risks = [risk for risk in risks if risk.title != "未发现明显障碍"]
        risks.append(_connector_risk(distance))
    else:
        risks = [risk for risk in risks if risk.title != "未发现明显障碍"]
        risks.append(RiskItem(
            level="低", title="起点与目的地坐标重合",
            detail="两点坐标重合，请现场确认具体入口和设施，以现场为准。",
            source=destination.source or "未标注来源", confidence="low",
        ))
    risks.sort(key=lambda risk: {"高": 0, "中": 1, "低": 2}[risk.level])
    return Route(
        route_id="route_1", destination=destination, node_path=(node_id,),
        polyline=(walk_graph.point_of(node_id),),
        steps=(RouteStep(
            index=1,
            instruction=f"起点与目的地吸附同一节点；两点直线约 {distance:.0f} 米，尚无已验证接驳路线",
            distance_m=distance, walk_type=-1, kind_label="未验证接驳",
            note="请现场确认入口和可步行路径，不按直线导航",
        ),),
        stats=stats, cost=round(max(0.0, time_cost(distance, config.scoring)), 3),
        risks=tuple(risks), is_primary=True,
    )


def plan_routes(
    walk_graph: WalkGraph,
    origin: Tuple[float, float],
    destination: Poi,
    config: AppConfig,
) -> PlanOutcome:
    """为单个目的地规划主路线与备选路线。

    Args:
        walk_graph: 已构建的路网。
        origin: 起点 ``(lon, lat)``。
        destination: 目的地 POI。
        config: 应用配置（评分与风险阈值）。
    """
    outcome = PlanOutcome()

    if walk_graph.node_count == 0:
        outcome.notes.append(
            "路网为空，无法规划路线。下一步：检查 data/graph_nodes.csv 与 graph_edges.csv。"
        )
        return outcome

    origin_node = nearest_node(walk_graph, origin, max_distance_m=SNAP_MAX_M)
    dest_node = nearest_node(walk_graph, destination.point, max_distance_m=SNAP_MAX_M)

    if origin_node is None:
        outcome.notes.append(
            f"起点距离最近路网节点超过 {int(SNAP_MAX_M)} 米，无法规划。"
            "下一步：换一个更靠近路网的起点，或补充该区域路网数据。"
        )
        return outcome
    if dest_node is None:
        outcome.notes.append(
            f"目的地「{destination.name}」距离最近路网节点超过 {int(SNAP_MAX_M)} 米，无法规划。"
            "下一步：检查该 POI 坐标是否录错，或补充路网数据。"
        )
        return outcome

    if origin_node == dest_node:
        outcome.routes.append(
            _direct_route(walk_graph, origin, origin_node, destination, config)
        )
        return outcome

    costs = _edge_costs(walk_graph, config)

    def weight(u: str, v: str, _data: Dict[str, object]) -> float:
        return costs.get(edge_key(u, v), 0.0)

    try:
        primary_path = nx.shortest_path(
            walk_graph.graph, source=origin_node, target=dest_node, weight=weight
        )
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        outcome.notes.append(
            f"未找到从起点到「{destination.name}」的可行步行路线。"
            "下一步：换一个目的地，或检查路网是否连通（python -m src.selfcheck --data）。"
        )
        return outcome

    # 用 networkx 的简单路径生成器按成本升序取候选，避免自研 K 短路
    candidate_paths: List[Tuple[str, ...]] = [tuple(primary_path)]
    try:
        generator = nx.shortest_simple_paths(
            walk_graph.graph, origin_node, dest_node, weight=weight
        )
        for path in generator:
            candidate = tuple(path)
            if candidate not in candidate_paths:
                candidate_paths.append(candidate)
            if len(candidate_paths) >= MAX_CANDIDATES:
                break
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        pass

    evaluations = []
    for path in candidate_paths:
        evaluation = evaluate_route(walk_graph, path, destination, config.scoring)
        evaluations.append((path, evaluation))
    evaluations.sort(key=lambda item: item[1].cost)

    for index, (path, evaluation) in enumerate(evaluations[: 1 + MAX_ALTERNATIVES]):
        risks = build_risks(
            evaluation.stats,
            destination,
            config.risk,
            notes=evaluation.notes,
            data_source=evaluation.data_source,
            data_confidence=evaluation.data_confidence,
        )
        outcome.routes.append(
            Route(
                route_id=f"route_{index + 1}",
                destination=destination,
                node_path=path,
                polyline=walk_graph.polyline(path),
                steps=build_steps(walk_graph, path, config),
                stats=evaluation.stats,
                cost=evaluation.cost,
                risks=tuple(risks),
                is_primary=(index == 0),
            )
        )

    outcome.routes = [_attach_connectors(route, origin, config) for route in outcome.routes]

    if len(outcome.routes) == 1:
        outcome.notes.append("仅找到一条可行路线，没有备选方案，请谨慎出行。")

    outcome.notes.append(
        "起点已吸附到最近路网节点，实际步行距离可能略有差异。"
    )
    return outcome


def describe_route(route: Route) -> List[str]:
    """把路线渲染成多行文字（CLI 与 Streamlit 共用）。"""
    lines: List[str] = []
    stats = route.stats
    lines.append(
        f"目的地：{route.destination.display_name}"
        f"（{route.destination.address or '地址未标注'}）"
    )
    distance_label = "距离估计至少" if stats.unverified_connector_m else "总距离约"
    lines.append(
        f"{distance_label} {stats.distance_m:.0f} 米，预计步行 {stats.duration_min:.0f} 分钟，"
        f"评分 {max(route.cost, 0.0):.2f}（越低越平缓）"
    )
    if stats.unverified_connector_m:
        lines.append(
            f"其中未验证接驳直线约 {stats.unverified_connector_m:.0f} 米；"
            "实际步行距离/时间可能更长，本结果不是完整导航。"
        )
    lines.append(
        f"台阶 {stairs_text(stats)}（{stats.stairs_segments} 段）、"
        f"最大坡度 {slope_text(stats)}、过街 {stats.crossings} 次、"
        f"沿途休息点 {stats.rest_points_nearby} 个"
    )
    lines.append("路线步骤：")
    for step in route.steps:
        suffix = f"（{step.note}）" if step.note else ""
        lines.append(f"  {step.index}. {step.instruction}{suffix}")
    return lines
