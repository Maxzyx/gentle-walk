"""编排层：数据源选择 → 建图 → 规划 → 风险汇总。

对外只暴露少数几个函数，CLI 与 Streamlit 共用同一套逻辑，保证两条前端行为一致。
离线优先（AGENTS.md）：默认只读 ``data/``，在线高德仅在显式开启且具备 Key 时尝试，
任何在线异常都降级回离线并留下中文提示，不阻塞主链路。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import keys
from .config import AppConfig, load_config
from .data_loader import Dataset, load_dataset
from .geo.geo_utils import haversine_m
from .graph_builder import WalkGraph, build_graph
from .models import (
    CATEGORY_LABELS,
    SUPPORTED_CATEGORIES,
    DataIssue,
    Poi,
    RiskItem,
    Route,
    RouteResult,
)
from .router import plan_routes

Point = Tuple[float, float]

#: 离线模式提示语（CLI/页面统一口径）
OFFLINE_BANNER = "离线演示模式：使用本地 data/ 目录数据（来源见 source 字段），无需网络与 AMAP_KEY"
#: 数据来源声明（AGENTS.md 安全边界要求）。各记录 source 字段是唯一事实来源，
#: 样例合成数据为人工编写，必须如实标注。
DATA_DISCLAIMER = (
    "来源见各条记录的 source 字段：「样例合成」为人工编写的虚构演示数据；"
    "「高德」来自高德开放平台（坐标已转 WGS84），「OSM」路网来自 © OpenStreetMap "
    "contributors（ODbL 1.0）；开放时间、坡道与路况细节可能缺失，以现场为准。"
)


@dataclass
class PlanRequest:
    """一次规划请求。"""

    category: str = "greengrocer"
    origin: Optional[Point] = None
    destination_id: Optional[str] = None


@dataclass
class ServiceState:
    """一次规划过程中加载到的数据与路网，供上层复用。"""

    config: AppConfig
    dataset: Dataset
    walk_graph: WalkGraph
    mode: str = "offline"
    key_source: str = "none"
    notes: List[str] = field(default_factory=list)


def normalize_category(raw: str) -> Optional[str]:
    """把用户输入规范化为受支持类目；无法识别返回 None。"""
    text = (raw or "").strip().lower()
    aliases: Dict[str, str] = {
        "greengrocer": "greengrocer",
        "菜店": "greengrocer",
        "菜市场": "greengrocer",
        "蔬菜": "greengrocer",
        "clinic": "clinic",
        "医院": "clinic",
        "社区医院": "clinic",
        "卫生服务站": "clinic",
    }
    mapped = aliases.get(text)
    if mapped is not None:
        return mapped
    return text if text in SUPPORTED_CATEGORIES else None


def parse_origin(raw: Optional[str], config: AppConfig) -> Point:
    """解析 ``lon,lat`` 形式的起点；为空时用片区中心。"""
    if raw is None or not str(raw).strip():
        return config.area.center
    text = str(raw).strip()
    parts = text.replace("，", ",").split(",")
    if len(parts) != 2:
        raise ValueError(
            f"起点格式应为「经度,纬度」，当前为 {text!r}。"
            "下一步：例如 --from 108.9455,34.2611。"
        )
    try:
        lon = float(parts[0])
        lat = float(parts[1])
    except ValueError as exc:
        raise ValueError(
            f"起点经纬度不是数字：{text!r}。下一步：检查是否把中文逗号或文字写进了坐标。"
        ) from exc
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        raise ValueError(
            f"起点经纬度超出合法范围：{lon},{lat}。下一步：确认经纬度顺序为「经度,纬度」。"
        )
    return (lon, lat)


def _try_load_online(config: AppConfig) -> Tuple[Optional[Dataset], List[str]]:
    """尝试加载在线数据；不可用时返回 (None, 提示)。"""
    try:
        from .adapters.online import load_online_dataset  # noqa: PLC0415
    except ImportError:
        return None, ["在线高德适配器尚未启用，已使用离线演示数据。"]
    try:
        dataset = load_online_dataset(config)
    except Exception as exc:  # noqa: BLE001 - 在线失败一律降级
        return None, [
            f"在线高德不可用（{exc}），已切换离线演示数据。",
            "下一步：确认网络与 AMAP_KEY 是否有效，或先继续用离线模式。",
        ]
    return dataset, ["已启用在线高德数据（坐标已按配置尝试转换为 WGS84）。"]


def prepare_state(
    config: Optional[AppConfig] = None,
    dataset: Optional[Dataset] = None,
    walk_graph: Optional[WalkGraph] = None,
) -> ServiceState:
    """准备数据与路网，并决定离线/在线模式。"""
    app_config = config or load_config()
    notes: List[str] = []
    mode = "offline"
    key_source = "none"

    if dataset is None:
        key_resolution = keys.resolve_key(config=app_config)
        key_source = key_resolution.source
        if not app_config.offline_demo and key_resolution.configured:
            online_dataset, online_notes = _try_load_online(app_config)
            notes.extend(online_notes)
            if online_dataset is not None:
                dataset = online_dataset
                mode = "online"
            else:
                dataset = load_dataset(app_config)
        else:
            if not app_config.offline_demo and not key_resolution.configured:
                notes.append(
                    "未配置 AMAP_KEY，已使用离线演示数据。"
                    "下一步：python -m src.cli --set-key 配置后可用在线数据。"
                )
            dataset = load_dataset(app_config)

    if walk_graph is None:
        walk_graph = build_graph(dataset, app_config)
        if any(edge.source == "OSM" for edge in walk_graph.edges.values()):
            notes.append(
                "路网数据来自 OpenStreetMap（© OpenStreetMap contributors，ODbL 1.0），"
                "细节标签可能缺失，以现场为准。"
            )
        poi_sources = {poi.source for poi in dataset.pois}
        if "高德" in poi_sources:
            notes.append(
                "POI 数据来自高德开放平台（按行政区检索，坐标已转 WGS84），"
                "仅用于本项目演示，设施信息以现场为准。"
            )

    if mode == "offline":
        notes.insert(0, OFFLINE_BANNER)
    return ServiceState(
        config=app_config,
        dataset=dataset,
        walk_graph=walk_graph,
        mode=mode,
        key_source=key_source,
        notes=notes,
    )


def list_destinations(
    category: str,
    state: Optional[ServiceState] = None,
    config: Optional[AppConfig] = None,
) -> List[Poi]:
    """列出某类目的所有可用目的地，按名称排序。"""
    resolved = normalize_category(category)
    if resolved is None:
        return []
    current = state or prepare_state(config=config)
    pois = current.dataset.pois_by_category(resolved)
    return sorted(pois, key=lambda poi: poi.id)


def plan(
    request: PlanRequest,
    config: Optional[AppConfig] = None,
    state: Optional[ServiceState] = None,
) -> RouteResult:
    """执行一次完整规划。

    未指定 ``destination_id`` 时会比较该类目下所有目的地，推荐成本最低的一条。
    """
    current = state or prepare_state(config=config)
    category = normalize_category(request.category)
    origin = request.origin or current.config.area.center

    result = RouteResult(origin=origin, category=category or request.category)
    result.mode = current.mode
    result.notes.extend(current.notes)

    if category is None:
        result.notes.append(
            f"不支持的目的地类型：{request.category!r}。"
            f"下一步：改用 {' 或 '.join(SUPPORTED_CATEGORIES)}。"
        )
        return result

    pois = current.dataset.pois_by_category(category)
    if not pois:
        result.notes.append(
            f"数据中没有可用的{CATEGORY_LABELS.get(category, category)}。"
            "下一步：运行 python -m src.selfcheck --data 检查样例数据。"
        )
        _attach_issues(result, current)
        return result

    if request.destination_id:
        matched = [poi for poi in pois if poi.id == request.destination_id]
        if not matched:
            result.notes.append(
                f"未找到编号为 {request.destination_id} 的目的地。"
                "下一步：用 --list 查看可用编号。"
            )
            _attach_issues(result, current)
            return result
        pois = matched

    best: Optional[Tuple[float, Poi, object]] = None
    compared = 0
    unreachable: List[str] = []
    unreachable_count = 0
    for poi in pois:
        outcome = plan_routes(current.walk_graph, origin, poi, current.config)
        if not outcome.ok:
            # 在线模式下目的地可能成百上千，逐条输出会淹没结果，这里只留少量样例
            unreachable_count += 1
            if len(unreachable) < 3:
                unreachable.append(poi.name)
            continue
        compared += 1
        primary_cost = outcome.routes[0].cost
        if best is None or primary_cost < best[0]:
            best = (primary_cost, poi, outcome)

    unreachable_note = ""
    if unreachable_count:
        examples = "、".join(unreachable)
        unreachable_note = (
            f"{unreachable_count} 个目的地超出路网吸附范围（示例：{examples}），已跳过。"
            "下一步：补充该区域路网数据，或换一个更靠近路网的起点。"
        )

    if best is None:
        result.notes.append(
            "所有候选目的地都没有可行路线。"
            "下一步：换一个起点（--from），或检查路网是否连通。"
        )
        if unreachable_note:
            result.notes.append(unreachable_note)
        _attach_issues(result, current)
        return result

    _, destination, outcome = best
    result.routes = list(outcome.routes)  # type: ignore[union-attr]
    result.risks = list(outcome.routes[0].risks)  # type: ignore[union-attr]
    # 吸附说明随后会用具体米数补充，这里先去重，避免两条相似提示同时出现
    result.notes.extend(
        note
        for note in outcome.notes  # type: ignore[union-attr]
        if "起点已吸附到最近路网节点" not in note
    )

    if len(pois) > 1:
        result.notes.insert(
            0,
            f"已在 {compared} 个{CATEGORY_LABELS.get(category, category)}中比较，"
            f"推荐「{destination.name}」（评分 {result.routes[0].cost:.2f}）。",
        )
    if unreachable_note:
        result.notes.append(unreachable_note)
    if result.routes:
        origin_node_distance = haversine_m(
            origin[0], origin[1], result.routes[0].polyline[0][0], result.routes[0].polyline[0][1]
        )
        if origin_node_distance > 5.0:
            result.notes.append(
                f"起点距最近路网节点约 {origin_node_distance:.0f} 米，"
                "该接驳尚未验证；已计入直线距离估计，请现场确认可步行路径。"
            )

    _attach_issues(result, current)
    return result


def _attach_issues(result: RouteResult, state: ServiceState) -> None:
    """把数据问题与待复核清单挂到结果上。"""
    result.data_issues = list(state.dataset.issues) + list(state.walk_graph.issues)
    result.pending_review = list(state.dataset.pending_review)
    if result.pending_review:
        result.notes.append(
            f"有 {len(result.pending_review)} 条数据坐标未纠偏（待复核），"
            "已排除在规划之外。下一步：安装 coordtransform（或 geotransform）后重新导入。"
        )


def describe_result(result: RouteResult) -> List[str]:
    """把规划结果渲染成文字（CLI 与页面共用）。"""
    from .router import describe_route, stairs_text, slope_text  # 局部导入避免循环依赖

    lines: List[str] = []
    mode_text = "在线高德" if result.mode == "online" else "离线演示"
    lines.append(f"【{mode_text}】起点：{result.origin[0]:.4f},{result.origin[1]:.4f}")

    for note in result.notes:
        lines.append(f"· {note}")

    if not result.routes:
        lines.append("未能生成路线。")
        lines.append(f"数据来源声明：{DATA_DISCLAIMER}")
        return lines

    primary = result.routes[0]
    lines.append("")
    lines.append("== 推荐路线 ==")
    lines.extend(describe_route(primary))

    lines.append("")
    lines.append("== 风险提示 ==")
    if result.risks:
        for risk in result.risks:
            lines.append(
                f"[{risk.level}] {risk.title}：{risk.detail}"
                f"（来源：{risk.source}，置信度：{risk.confidence}）"
            )
    else:
        lines.append("未发现明显风险。")

    alternatives = result.routes[1:]
    if alternatives:
        lines.append("")
        lines.append("== 备选路线 ==")
        for index, route in enumerate(alternatives, start=1):
            lines.append(
                f"备选 {index}：距离约 {route.stats.distance_m:.0f} 米、"
                f"台阶 {stairs_text(route.stats)}、"
                f"最大坡度 {slope_text(route.stats)}、"
                f"过街 {route.stats.crossings} 次、评分 {max(route.cost, 0.0):.2f}"
            )
            if route.stats.unverified_connector_m:
                lines.append(
                    f"    · 含未验证接驳直线约 {route.stats.unverified_connector_m:.0f} 米，"
                    "不是完整导航，以现场为准。"
                )
            for step in route.steps[:3]:
                lines.append(f"    · {step.instruction}")

    if result.data_issues:
        errors = [issue for issue in result.data_issues if issue.is_error]
        if errors:
            lines.append("")
            lines.append(f"== 数据问题（已跳过 {len(errors)} 条）==")
            for issue in errors[:5]:
                lines.append(f"  - {issue.source_file}/{issue.record_id}：{issue.reason}")

    lines.append("")
    lines.append(f"数据来源声明：{DATA_DISCLAIMER}")
    return lines
