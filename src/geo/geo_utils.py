"""地理几何工具：球面距离、局部米制投影、点到线段距离、bbox 操作。

约定：所有坐标一律 ``(lon, lat)`` 顺序，单位为十进制度；距离单位为米。
不引入额外依赖（pandas/networkx 之外不需要第三方库），保证离线可跑。
"""

from __future__ import annotations

import math
from typing import Iterable, List, Optional, Sequence, Tuple

#: 地球平均半径（米），用于 haversine
EARTH_RADIUS_M = 6371008.8
#: 1 度纬度对应的米数（近似常数）
M_PER_DEG_LAT = 111194.93
#: 赤道处 1 度经度对应的米数
M_PER_DEG_LON_EQUATOR = 111319.49

Point = Tuple[float, float]
BBox = Tuple[float, float, float, float]  # (lon_min, lat_min, lon_max, lat_max)


# --------------------------------------------------------------------------- #
# 距离
# --------------------------------------------------------------------------- #
def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """两点间球面距离（米），使用 haversine 公式。"""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(d_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, a)))


def m_per_deg_lon(lat: float) -> float:
    """某纬度处 1 度经度对应的米数。"""
    return M_PER_DEG_LON_EQUATOR * math.cos(math.radians(lat))


def local_xy_m(point: Point, origin: Point) -> Tuple[float, float]:
    """把经纬度近似投影为以 ``origin`` 为原点的局部平面坐标（米）。

    适合片区内（数公里尺度）的距离与角度计算，误差 < 0.1%。
    """
    lon, lat = point
    lon0, lat0 = origin
    x = (lon - lon0) * m_per_deg_lon(lat0)
    y = (lat - lat0) * M_PER_DEG_LAT
    return x, y


def local_distance_m(a: Point, b: Point, origin: Optional[Point] = None) -> float:
    """局部平面上的两点距离（米）；``origin`` 缺省用两点中点。"""
    base = origin if origin is not None else ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    ax, ay = local_xy_m(a, base)
    bx, by = local_xy_m(b, base)
    return math.hypot(bx - ax, by - ay)


def polyline_length_m(points: Sequence[Point]) -> float:
    """折线总长度（米）；少于两个点返回 0。"""
    total = 0.0
    for index in range(len(points) - 1):
        total += haversine_m(*points[index], *points[index + 1])
    return total


def point_segment_distance_m(point: Point, start: Point, end: Point) -> float:
    """点到线段的最短距离（米），用于「休息点是否在路线 200m 缓冲内」。"""
    origin = point
    px, py = local_xy_m(point, origin)
    ax, ay = local_xy_m(start, origin)
    bx, by = local_xy_m(end, origin)

    dx = bx - ax
    dy = by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-12:
        return math.hypot(px - ax, py - ay)

    # 投影参数 t 限制在 [0, 1]，得到线段上的最近点
    t = ((px - ax) * dx + (py - ay) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    cx = ax + t * dx
    cy = ay + t * dy
    return math.hypot(px - cx, py - cy)


def point_polyline_distance_m(point: Point, polyline: Sequence[Point]) -> float:
    """点到折线的最短距离（米）；折线只有 1 点时退化为点距。"""
    if not polyline:
        raise ValueError("折线为空，无法计算距离。下一步：确认路线至少包含一个节点。")
    if len(polyline) == 1:
        return haversine_m(*point, *polyline[0])
    return min(
        point_segment_distance_m(point, polyline[i], polyline[i + 1])
        for i in range(len(polyline) - 1)
    )


# --------------------------------------------------------------------------- #
# bbox
# --------------------------------------------------------------------------- #
def bbox_contains(bbox: BBox, point: Point, pad: float = 0.0) -> bool:
    """点是否落在 bbox 内（可放宽 ``pad`` 度）。"""
    lon_min, lat_min, lon_max, lat_max = bbox
    lon, lat = point
    return (
        lon_min - pad <= lon <= lon_max + pad
        and lat_min - pad <= lat <= lat_max + pad
    )


def bbox_center(bbox: BBox) -> Point:
    """bbox 中心点。"""
    lon_min, lat_min, lon_max, lat_max = bbox
    return ((lon_min + lon_max) / 2.0, (lat_min + lat_max) / 2.0)


def bbox_span_deg(bbox: BBox) -> Tuple[float, float]:
    """bbox 的经纬跨度（度）。"""
    lon_min, lat_min, lon_max, lat_max = bbox
    return (lon_max - lon_min, lat_max - lat_min)


def bbox_expand_m(bbox: BBox, meters: float) -> BBox:
    """把 bbox 向外扩展指定米数（用于构造查询范围）。"""
    lon_min, lat_min, lon_max, lat_max = bbox
    lat_mid = (lat_min + lat_max) / 2.0
    d_lat = meters / M_PER_DEG_LAT
    d_lon = meters / max(m_per_deg_lon(lat_mid), 1e-9)
    return (lon_min - d_lon, lat_min - d_lat, lon_max + d_lon, lat_max + d_lat)


def bbox_union(boxes: Iterable[BBox]) -> Optional[BBox]:
    """合并多个 bbox；输入为空返回 None。"""
    collected: List[BBox] = [b for b in boxes if b is not None]
    if not collected:
        return None
    lon_min = min(b[0] for b in collected)
    lat_min = min(b[1] for b in collected)
    lon_max = max(b[2] for b in collected)
    lat_max = max(b[3] for b in collected)
    return (lon_min, lat_min, lon_max, lat_max)


def interpolate(a: Point, b: Point, ratio: float) -> Point:
    """两点间线性插值，``ratio`` 会被裁剪到 [0, 1]。"""
    t = max(0.0, min(1.0, ratio))
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
