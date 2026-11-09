"""风险提示与分级：把路线统计翻译成老人和家属看得懂的中文提示。

分级阈值集中在 ``config/default.json`` 的 ``risk`` 段，可按片区调研结果调整。
每条提示都带来源与置信度，避免把「样例数据」当成「已核实事实」。
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from .config import RiskConfig
from .models import Poi, RiskItem, RouteStats, obstacle_type_label

#: 风险等级中文
LEVEL_LOW = "低"
LEVEL_MEDIUM = "中"
LEVEL_HIGH = "高"

#: 一条风险都没触发时的兜底提示
NO_RISK_HINT = "本路线未触发台阶、陡坡、过街等风险项，仍建议以现场情况为准"


def classify_risk(stats: RouteStats, risk: RiskConfig) -> str:
    """按统计量给出总体风险等级（高/中/低）。"""
    if (
        stats.stairs_total >= risk.high_min_stairs
        or stats.max_slope_pct >= risk.steep_slope_pct
        or stats.crossings > risk.medium_max_crossings
        or stats.max_obstacle_severity >= 3
    ):
        return LEVEL_HIGH

    if (
        stats.stairs_total > risk.low_max_stairs
        or stats.max_slope_pct > risk.low_max_slope_pct
        or stats.crossings > risk.low_max_crossings
        or stats.fatigue_segments > 0
        or stats.no_railing_m > 0
        or stats.obstacle_count > 0
        or stats.stairs_unknown_segments > 0
        or stats.slope_unknown_segments > 0
        or stats.pedestrian_uncertain_segments > 0
    ):
        return LEVEL_MEDIUM

    return LEVEL_LOW


def build_risks(
    stats: RouteStats,
    destination: Optional[Poi],
    risk: RiskConfig,
    notes: Sequence[str] = (),
    data_source: str = "样例合成",
    data_confidence: str = "low",
) -> List[RiskItem]:
    """生成风险提示列表（按严重度排序：高 → 中 → 低）。"""
    items: List[RiskItem] = []
    overall = classify_risk(stats, risk)

    if stats.stairs_segments > 0:
        level = LEVEL_HIGH if stats.stairs_total >= risk.high_min_stairs else LEVEL_MEDIUM
        detail = f"路线含 {stats.stairs_segments} 段台阶"
        if stats.stairs_total > 0:
            detail += f"，已标注合计约 {stats.stairs_total} 级"
        if stats.stairs_unknown_segments:
            detail += f"，另有 {stats.stairs_unknown_segments} 段级数未标注，以现场为准"
        items.append(
            RiskItem(
                level=level,
                title="有台阶",
                detail=detail + "。轮椅或拄拐出行请谨慎，必要时改走坡道。",
                source=data_source,
                confidence=data_confidence,
            )
        )

    if stats.slope_segments > 0 or stats.max_slope_pct > 0:
        detail = (
            f"已标注路段最大坡度约 {stats.max_slope_pct:.1f}%"
            if stats.slope_segments > stats.slope_unknown_segments or stats.max_slope_pct > 0
            else "坡度未标注"
        )
        if stats.slope_unknown_segments:
            detail += f"，另有 {stats.slope_unknown_segments} 段坡度未知，以现场为准"
        level = LEVEL_HIGH if stats.max_slope_pct >= risk.steep_slope_pct else LEVEL_MEDIUM
        items.append(
            RiskItem(
                level=level,
                title="有坡道/斜坡",
                detail=detail + "。下坡时注意控制速度，雨雪天易滑。",
                source=data_source,
                confidence=data_confidence,
            )
        )

    if stats.pedestrian_uncertain_segments:
        items.append(RiskItem(
            level=LEVEL_MEDIUM, title="部分路段人行道未确认",
            detail=(
                f"{stats.pedestrian_uncertain_segments} 段道路没有可信的人行道信息，"
                "不要直接沿车行道行走；下一步：现场确认或更换路线，以现场为准。"
            ),
            source=data_source, confidence="low",
        ))

    if stats.crossings > 0:
        level = (
            LEVEL_HIGH
            if stats.crossings > risk.medium_max_crossings
            else LEVEL_MEDIUM
        )
        items.append(
            RiskItem(
                level=level,
                title="需要过街",
                detail=(
                    f"路线需过街 {stats.crossings} 次，请走人行横道并留意右转车辆。"
                ),
                source=data_source,
                confidence=data_confidence,
            )
        )

    if stats.no_railing_m > 0:
        items.append(
            RiskItem(
                level=LEVEL_MEDIUM,
                title="部分路段无护栏",
                detail=(
                    f"约 {stats.no_railing_m:.0f} 米路段紧邻车行道且无护栏，"
                    "建议靠内侧行走。"
                ),
                source=data_source,
                confidence=data_confidence,
            )
        )

    if stats.obstacle_count > 0:
        items.append(
            RiskItem(
                level=LEVEL_MEDIUM if stats.max_obstacle_severity < 3 else LEVEL_HIGH,
                title="途经障碍点",
                detail=(
                    f"路线经过 {stats.obstacle_count} 处已记录障碍"
                    f"（最高严重度 {stats.max_obstacle_severity}/3），"
                    "现场情况可能变化，请以实际为准。"
                ),
                source=data_source,
                confidence=data_confidence,
            )
        )

    if stats.fatigue_segments > 0:
        items.append(
            RiskItem(
                level=LEVEL_MEDIUM,
                title="连续路段缺少休息点",
                detail=(
                    f"有 {stats.fatigue_segments} 段连续超过 200 米没有休息点，"
                    "建议中途停歇。"
                ),
                source="评分规则（MVP 假设）",
                confidence="low",
            )
        )
    elif stats.rest_points_nearby == 0 and stats.distance_m >= 300:
        items.append(
            RiskItem(
                level=LEVEL_LOW,
                title="沿途未发现休息点",
                detail="沿线 200 米范围内未记录休息点，建议出门前先休息。",
                source="评分规则（MVP 假设）",
                confidence="low",
            )
        )

    if stats.unknown_segments > 0:
        items.append(
            RiskItem(
                level=LEVEL_LOW,
                title="路型未标注",
                detail=(
                    f"{stats.unknown_segments} 段道路类型未识别，"
                    "台阶/坡度细节未标注，以现场为准。"
                ),
                source=data_source,
                confidence="low",
            )
        )

    if destination is not None and destination.has_ramp is False:
        items.append(
            RiskItem(
                level=LEVEL_MEDIUM,
                title="目的地入口无坡道",
                detail=(
                    f"「{destination.name}」入口未记录坡道"
                    + ("，且本路线含台阶。" if stats.stairs_segments else "。")
                    + "建议家属陪同或先电话确认。"
                ),
                source=destination.source or data_source,
                confidence=destination.confidence or "low",
            )
        )

    for note in notes:
        items.append(
            RiskItem(
                level=LEVEL_LOW,
                title="数据说明",
                detail=note,
                source="评分规则",
                confidence="low",
            )
        )

    if not items:
        items.append(
            RiskItem(
                level=LEVEL_LOW,
                title="未发现明显障碍",
                detail=NO_RISK_HINT,
                source=data_source,
                confidence=data_confidence,
            )
        )

    order = {LEVEL_HIGH: 0, LEVEL_MEDIUM: 1, LEVEL_LOW: 2}
    items.sort(key=lambda item: order.get(item.level, 3))
    return items


def risk_summary(risks: Sequence[RiskItem]) -> str:
    """一行摘要，例如「高风险 1 条、中风险 2 条」。"""
    counts = {LEVEL_HIGH: 0, LEVEL_MEDIUM: 0, LEVEL_LOW: 0}
    for item in risks:
        counts[item.level] = counts.get(item.level, 0) + 1
    parts = [
        f"{level}风险 {counts[level]} 条"
        for level in (LEVEL_HIGH, LEVEL_MEDIUM, LEVEL_LOW)
        if counts.get(level)
    ]
    return "、".join(parts) if parts else "无风险提示"


def obstacle_risk_hint(obstacle_type: str, severity: int) -> str:
    """单条障碍的简短说明，供列表输出使用。"""
    return f"{obstacle_type_label(obstacle_type)}（严重度 {severity}/3）"
