"""步行路径规划 2.0：解析 ``WalkStep`` 并映射 ``walk_type``。

AGENTS.md「步行路径规划与障碍字段」：
- 接口 ``GET https://restapi.amap.com/v5/direction/walking``；
- ``WalkStep.walk_type`` 中 **阶梯为 21、斜坡为 22**，人行横道为 3，扶梯为 9，直梯为 10；
- 这些类型直接映射为评分中的台阶/陡坡惩罚项；
- ``step_count`` 等细粒度字段缺失时用默认中等惩罚，并提示「以现场为准」。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..config import AppConfig
from ..models import (
    WALK_TYPE_CROSSING,
    WALK_TYPE_ELEVATOR,
    WALK_TYPE_ESCALATOR,
    WALK_TYPE_NORMAL,
    WALK_TYPE_SLOPE,
    WALK_TYPE_STAIRS,
    WalkStep,
    walk_type_label,
)
from .amap_client import AmapClient, AmapResponse, AmapUnavailable

#: 步行规划路径
WALKING_PATH = "/v5/direction/walking"

#: walk_type → 内部路型标识（评分直接使用）
WALK_TYPE_MAP: Dict[int, str] = {
    WALK_TYPE_CROSSING: "crossing",
    WALK_TYPE_ESCALATOR: "escalator",
    WALK_TYPE_ELEVATOR: "elevator",
    WALK_TYPE_STAIRS: "stairs",
    WALK_TYPE_SLOPE: "slope",
}

#: 未识别类型
UNKNOWN_KIND = "unknown"


def map_walk_type(code: Optional[int]) -> str:
    """把高德 ``walk_type`` 映射为内部路型标识。

    Returns:
        ``stairs`` / ``slope`` / ``crossing`` / ``escalator`` / ``elevator`` / ``unknown``。
    """
    if code is None:
        return UNKNOWN_KIND
    try:
        normalized = int(code)
    except (TypeError, ValueError):
        return UNKNOWN_KIND
    return WALK_TYPE_MAP.get(normalized, UNKNOWN_KIND)


def map_to_label(code: Optional[int]) -> str:
    """把 ``walk_type`` 映射为中文名（未识别返回「未识别路型」）。"""
    if code is None:
        return walk_type_label(None)
    try:
        return walk_type_label(int(code))
    except (TypeError, ValueError):
        return walk_type_label(None)


def _as_float(value: Any, default: float = 0.0) -> float:
    """把高德返回的字符串数字转 float。"""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_int(value: Any) -> Optional[int]:
    """把高德返回的字符串数字转 int；空值返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(round(float(text)))
    except ValueError:
        return None


def parse_polyline(raw: Any) -> Tuple[Tuple[float, float], ...]:
    """解析 ``"lon,lat;lon,lat"`` 形式的折线；非法片段跳过。"""
    if not isinstance(raw, str) or not raw.strip():
        return ()
    points: List[Tuple[float, float]] = []
    for chunk in raw.split(";"):
        parts = chunk.split(",")
        if len(parts) != 2:
            continue
        try:
            points.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return tuple(points)


def parse_walk_steps(payload: Mapping[str, Any]) -> List[WalkStep]:
    """解析步行规划响应中的第一段 path 的 steps。

    Returns:
        WalkStep 列表；无有效步骤时返回空列表（由调用方决定是否报错）。
    """
    route = payload.get("route") or {}
    paths = route.get("paths") or []
    if not isinstance(paths, list) or not paths:
        return []

    raw_steps = paths[0].get("steps") or []
    if not isinstance(raw_steps, list):
        return []

    steps: List[WalkStep] = []
    for raw in raw_steps:
        if not isinstance(raw, Mapping):
            continue
        walk_type_raw = raw.get("walk_type")
        walk_type = _as_int(walk_type_raw)
        if walk_type is None:
            # 字段缺失：按普通步道处理，但保留「未标注」信息由上层提示
            walk_type = WALK_TYPE_NORMAL
        steps.append(
            WalkStep(
                instruction=str(raw.get("instruction") or "").strip(),
                distance_m=_as_float(raw.get("distance")),
                walk_type=walk_type,
                step_count=_as_int(raw.get("step_count")),
                slope_pct=None,  # 高德步行 2.0 不直接返回坡度百分比
                road_name=str(raw.get("road") or "").strip(),
                polyline=parse_polyline(raw.get("polyline")),
                source="高德样例",
            )
        )
    return steps


def fetch_walking_steps(
    client: AmapClient,
    origin: Tuple[float, float],
    destination: Tuple[float, float],
) -> List[WalkStep]:
    """调用高德步行规划，返回解析后的步骤列表。

    Raises:
        AmapUnavailable: 无 Key、请求失败或返回没有可用路径。
    """
    params = {
        "origin": f"{origin[0]:.6f},{origin[1]:.6f}",
        "destination": f"{destination[0]:.6f},{destination[1]:.6f}",
    }
    response: AmapResponse = client.get(WALKING_PATH, params)
    if not response.ok:
        raise AmapUnavailable(response.error or "高德步行规划请求失败")

    steps = parse_walk_steps(response.data)
    if not steps:
        raise AmapUnavailable(
            "高德步行规划未返回可用路径。下一步：换个起点或目的地重试，或改用离线演示模式。"
        )
    return steps


def summarize_steps(steps: List[WalkStep]) -> Dict[str, Any]:
    """统计步骤中的台阶/坡度/过街信息，供日志与自检使用。"""
    stairs = sum(1 for step in steps if map_walk_type(step.walk_type) == "stairs")
    slopes = sum(1 for step in steps if map_walk_type(step.walk_type) == "slope")
    crossings = sum(1 for step in steps if map_walk_type(step.walk_type) == "crossing")
    unknown = sum(1 for step in steps if map_walk_type(step.walk_type) == UNKNOWN_KIND)
    total_steps = sum(step.step_count or 0 for step in steps)
    distance = sum(step.distance_m for step in steps)
    return {
        "steps": len(steps),
        "stairs_segments": stairs,
        "stairs_total": total_steps,
        "slope_segments": slopes,
        "crossings": crossings,
        "unknown_segments": unknown,
        "distance_m": round(distance, 1),
    }
