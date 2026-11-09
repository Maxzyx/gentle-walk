"""图构建：把离线 CSV 数据组装成 ``networkx`` 无向图，并把障碍点挂到最近路段。

遵循 AGENTS.md「图结构与加权最短路用 networkx，不自研算法」：
本模块只负责**建图**与**空间关联**，最短路交给 ``networkx.shortest_path``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import networkx as nx

from .config import AppConfig
from .data_loader import Dataset
from .geo.geo_utils import haversine_m, point_segment_distance_m
from .models import (
    DataIssue,
    GraphEdge,
    GraphNode,
    Obstacle,
    RestPoint,
    WALK_TYPE_LABELS,
)

EdgeKey = Tuple[str, str]

#: 障碍点与路段的最大关联距离（米）；超出则不参与该路段评分
DEFAULT_OBSTACLE_ATTACH_M = 30.0


def edge_key(u: str, v: str) -> EdgeKey:
    """无向边的规范键（排序后），保证 (u,v) 与 (v,u) 视为同一条。"""
    return (u, v) if u <= v else (v, u)


@dataclass
class WalkGraph:
    """路网容器：networkx 图 + 原始对象索引 + 空间关联结果。"""

    graph: nx.Graph
    nodes: Dict[str, GraphNode] = field(default_factory=dict)
    edges: Dict[EdgeKey, GraphEdge] = field(default_factory=dict)
    obstacles_by_edge: Dict[EdgeKey, List[Obstacle]] = field(default_factory=dict)
    rest_points: List[RestPoint] = field(default_factory=list)
    issues: List[DataIssue] = field(default_factory=list)
    unattached_obstacles: List[Obstacle] = field(default_factory=list)

    @property
    def node_count(self) -> int:
        """节点数。"""
        return self.graph.number_of_nodes()

    @property
    def edge_count(self) -> int:
        """边数。"""
        return self.graph.number_of_edges()

    def point_of(self, node_id: str) -> Tuple[float, float]:
        """节点坐标。"""
        node = self.nodes[node_id]
        return (node.lon, node.lat)

    def connected(self) -> bool:
        """路网是否单连通（用于自检提示）。"""
        if self.graph.number_of_nodes() == 0:
            return False
        return nx.is_connected(self.graph)

    def component_count(self) -> int:
        """连通分量数量。"""
        if self.graph.number_of_nodes() == 0:
            return 0
        return nx.number_connected_components(self.graph)

    def polyline(self, node_path: Sequence[str]) -> Tuple[Tuple[float, float], ...]:
        """把节点序列转换为折线坐标序列。"""
        return tuple(self.point_of(node_id) for node_id in node_path)

    def edge_obstacles(self, u: str, v: str) -> List[Obstacle]:
        """取某条边关联的障碍点。"""
        return self.obstacles_by_edge.get(edge_key(u, v), [])


def build_graph(
    dataset: Dataset,
    config: Optional[AppConfig] = None,
    obstacle_attach_m: float = DEFAULT_OBSTACLE_ATTACH_M,
) -> WalkGraph:
    """根据数据集构建路网图。

    行为：
    - 节点：来自 ``graph_nodes.csv``；
    - 边：来自 ``graph_edges.csv``，端点缺失的边跳过并记入 ``issues``；
    - 障碍点：按「点到线段最短距离」挂到最近路段（≤ ``obstacle_attach_m`` 米），
      超出关联距离的记入 ``unattached_obstacles``，不参与评分也不静默丢弃。
    """
    graph = nx.Graph()
    nodes: Dict[str, GraphNode] = {}
    edges: Dict[EdgeKey, GraphEdge] = {}
    issues: List[DataIssue] = []

    for node in dataset.nodes:
        nodes[node.node_id] = node
        graph.add_node(node.node_id, lon=node.lon, lat=node.lat)

    for edge in dataset.edges:
        if edge.u not in nodes or edge.v not in nodes:
            issues.append(
                DataIssue(
                    source_file="graph_edges.csv",
                    record_id=f"{edge.u}-{edge.v}",
                    reason=(
                        "边引用了不存在的节点，已跳过。"
                        "下一步：检查 graph_nodes.csv 是否包含该节点编号。"
                    ),
                )
            )
            continue
        key = edge_key(edge.u, edge.v)
        if key in edges:
            issues.append(
                DataIssue(
                    source_file="graph_edges.csv",
                    record_id=f"{edge.u}-{edge.v}",
                    reason="重复边（同一对端点出现多次），已保留第一条。",
                    severity="warning",
                )
            )
            continue
        edges[key] = edge
        graph.add_edge(
            edge.u,
            edge.v,
            length_m=edge.length_m,
            walk_type=edge.walk_type,
            step_count=edge.step_count,
            slope_pct=edge.slope_pct,
            surface=edge.surface,
            key=key,
        )

    obstacles_by_edge: Dict[EdgeKey, List[Obstacle]] = {}
    unattached: List[Obstacle] = []
    for obstacle in dataset.obstacles:
        best_key: Optional[EdgeKey] = None
        best_distance = float("inf")
        for key, edge in edges.items():
            start = (nodes[edge.u].lon, nodes[edge.u].lat)
            end = (nodes[edge.v].lon, nodes[edge.v].lat)
            distance = point_segment_distance_m(obstacle.point, start, end)
            if distance < best_distance:
                best_distance = distance
                best_key = key
        if best_key is None or best_distance > obstacle_attach_m:
            unattached.append(obstacle)
            continue
        obstacles_by_edge.setdefault(best_key, []).append(obstacle)

    if graph.number_of_nodes() == 0:
        issues.append(
            DataIssue(
                source_file="graph_nodes.csv",
                record_id="-",
                reason=(
                    "路网没有可用节点，无法规划路线。"
                    "下一步：检查 data/graph_nodes.csv 是否存在且坐标合法。"
                ),
            )
        )
    elif nx.number_connected_components(graph) > 1:
        issues.append(
            DataIssue(
                source_file="graph_nodes.csv",
                record_id="-",
                reason=(
                    f"路网存在 {nx.number_connected_components(graph)} 个连通分量，"
                    "部分目的地可能不可达。下一步：补充连接边使路网连通。"
                ),
                severity="warning",
            )
        )

    return WalkGraph(
        graph=graph,
        nodes=nodes,
        edges=edges,
        obstacles_by_edge=obstacles_by_edge,
        rest_points=list(dataset.rest_points),
        issues=issues,
        unattached_obstacles=unattached,
    )


def nearest_node(
    walk_graph: WalkGraph, point: Tuple[float, float], max_distance_m: Optional[float] = None
) -> Optional[str]:
    """找离给定坐标最近的节点；超过 ``max_distance_m`` 返回 None。"""
    best_node: Optional[str] = None
    best_distance = float("inf")
    lon, lat = point
    for node_id, node in walk_graph.nodes.items():
        distance = haversine_m(lon, lat, node.lon, node.lat)
        if distance < best_distance:
            best_distance = distance
            best_node = node_id
    if best_node is None:
        return None
    if max_distance_m is not None and best_distance > max_distance_m:
        return None
    return best_node


def unknown_walk_type_edges(walk_graph: WalkGraph) -> List[GraphEdge]:
    """返回路型号无法识别的边，供风险提示。"""
    return [
        edge
        for edge in walk_graph.edges.values()
        if edge.walk_type not in WALK_TYPE_LABELS
    ]
