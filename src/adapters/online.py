"""在线数据源：用高德 POI 替换样例目的地，路网仍使用离线路网。

取舍说明（MVP 边界）：
- 在线高德只用来「发现目的地」，步行路线仍由本地路网 + networkx 计算，
  这样离线与在线共用同一套评分/风险逻辑，结果可比、可解释；
- 在线 POI 坐标为 GCJ02，入库前统一转换一次；转换库缺失时整条进入「待复核」，
  不会把未纠偏坐标混进路线结果；
- 任何网络/Key 问题都抛 ``AmapUnavailable``，由 ``src/service.py`` 降级回离线。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, List, Mapping, Optional

from ..config import AppConfig
from ..data_loader import Dataset, load_dataset
from ..geo.coord_transform import convert_records
from ..models import SUPPORTED_CATEGORIES, DataIssue, Poi
from .amap_client import AmapClient, AmapUnavailable
from .amap_poi import QuadtreeStats, search_category


def _poi_from_record(record: Mapping[str, Any]) -> Poi:
    """把已转换坐标的记录转成 ``Poi``（只取需要的字段）。"""
    return Poi(
        id=str(record.get("id") or ""),
        name=str(record.get("name") or ""),
        category=str(record.get("category") or ""),
        lon=float(record["lon"]),
        lat=float(record["lat"]),
        coord_system=str(record.get("coord_system") or "wgs84"),
        address=str(record.get("address") or ""),
        open_hours=str(record.get("open_hours") or ""),
        has_ramp=None,  # 高德不提供坡道信息，保持未知，风险提示里如实标注
        source=str(record.get("source") or "高德样例"),
        confidence=str(record.get("confidence") or "medium"),
        updated_at=str(record.get("updated_at") or ""),
        notes=str(record.get("notes") or ""),
    )


def load_online_dataset(
    config: AppConfig, client: Optional[AmapClient] = None
) -> Dataset:
    """用高德 POI 搜索构建数据集。

    Args:
        config: 应用配置（片区 bbox、四叉树参数、缓存目录）。
        client: 可选客户端替身（测试用）。

    Raises:
        AmapUnavailable: 无 Key、网络失败或高德返回业务错误。
    """
    current_client = client or AmapClient(config)
    if not current_client.has_key:
        raise AmapUnavailable(
            "未配置 AMAP_KEY，无法使用在线数据。"
            "下一步：python -m src.cli --set-key 配置后重试，或继续使用离线演示模式。"
        )

    offline = load_dataset(config)
    pois: List[Poi] = []
    pending: List[Dict[str, Any]] = list(offline.pending_review)
    issues: List[DataIssue] = list(offline.issues)
    stats_total = QuadtreeStats()

    for category in SUPPORTED_CATEGORIES:
        records, stats = search_category(
            current_client, category, config.area.bbox, config.quadtree
        )
        stats_total.requests += stats.requests
        stats_total.leaves += stats.leaves
        stats_total.splits += stats.splits
        stats_total.max_depth = max(stats_total.max_depth, stats.max_depth)
        stats_total.truncated_leaves += stats.truncated_leaves

        converted, review = convert_records(records)
        for record in converted:
            try:
                pois.append(_poi_from_record(record))
            except (KeyError, TypeError, ValueError) as exc:
                issues.append(
                    DataIssue(
                        source_file="高德 POI",
                        record_id=str(record.get("id") or record.get("name") or "?"),
                        reason=f"在线 POI 字段异常（{exc}），已跳过。下一步：重试或检查接口返回。",
                        severity="warning",
                    )
                )
        for record in review:
            pending.append(
                {
                    "id": record.get("id"),
                    "name": record.get("name"),
                    "lon": record.get("lon"),
                    "lat": record.get("lat"),
                    "reason": record.get("review_reason", ""),
                }
            )

    if not pois and pending:
        raise AmapUnavailable(
            f"在线 POI 共 {len(pending)} 条坐标无法转换（缺少 geotransform 或 pyproj），"
            "已全部进入待复核，未参与规划。"
            "下一步：安装 geotransform 后重试，或继续使用离线演示模式。"
        )

    if stats_total.truncated_leaves:
        issues.append(
            DataIssue(
                source_file="高德 POI",
                record_id="quadtree",
                reason=(
                    f"有 {stats_total.truncated_leaves} 个网格在达到深度/跨度上限后仍超过阈值，"
                    "结果可能不完整。下一步：缩小片区范围或提高 max_depth。"
                ),
                severity="warning",
            )
        )

    return replace(offline, pois=pois, pending_review=pending, issues=issues)
