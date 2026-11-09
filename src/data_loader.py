"""CSV 加载与校验：编码兜底、坏数据入 ``data_issues``、坐标统一转 WGS84。

遵循 AGENTS.md「兼容性硬性要求」：
- 读取依次尝试 ``utf-8`` → ``utf-8-sig`` → ``gbk``；
- 单条坏数据（缺字段、类型错误、坐标为空）跳过并记录，不让整体加载失败；
- ``coord_system`` 必须存在，缺失或无法转换的条目进「待复核」清单，不崩溃。
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Generic, Iterable, List, Mapping, Optional, Sequence, Tuple, TypeVar

from .config import AppConfig
from .geo.coord_transform import (
    STATUS_OK,
    CoordResult,
    normalize_system,
    to_wgs84,
)
from .models import (
    DataIssue,
    GraphEdge,
    GraphNode,
    Obstacle,
    Poi,
    RestPoint,
    VALID_CONFIDENCE,
    VALID_SOURCES,
    WALK_TYPE_NORMAL,
)

T = TypeVar("T")

#: 读取编码顺序（AGENTS.md 明确要求）
ENCODINGS: Tuple[str, ...] = ("utf-8", "utf-8-sig", "gbk")

#: 数据文件名
FILE_GREENGROCER = "pois_greengrocer.csv"
FILE_CLINIC = "pois_clinic.csv"
FILE_OBSTACLES = "obstacles.csv"
FILE_REST_POINTS = "rest_points.csv"
FILE_GRAPH_NODES = "graph_nodes.csv"
FILE_GRAPH_EDGES = "graph_edges.csv"


# --------------------------------------------------------------------------- #
# 结果容器
# --------------------------------------------------------------------------- #
@dataclass
class LoadResult(Generic[T]):
    """单文件加载结果。"""

    items: List[T] = field(default_factory=list)
    issues: List[DataIssue] = field(default_factory=list)
    pending_review: List[Dict[str, Any]] = field(default_factory=list)
    encoding: str = ""
    path: Optional[Path] = None

    @property
    def errors(self) -> List[DataIssue]:
        """被跳过的坏数据。"""
        return [issue for issue in self.issues if issue.is_error]

    @property
    def warnings(self) -> List[DataIssue]:
        """已用默认值但保留的记录。"""
        return [issue for issue in self.issues if not issue.is_error]


@dataclass
class Dataset:
    """整个离线数据集。"""

    pois: List[Poi] = field(default_factory=list)
    obstacles: List[Obstacle] = field(default_factory=list)
    rest_points: List[RestPoint] = field(default_factory=list)
    nodes: List[GraphNode] = field(default_factory=list)
    edges: List[GraphEdge] = field(default_factory=list)
    issues: List[DataIssue] = field(default_factory=list)
    pending_review: List[Dict[str, Any]] = field(default_factory=list)
    encodings: Dict[str, str] = field(default_factory=dict)

    def pois_by_category(self, category: str) -> List[Poi]:
        """按类目筛选目的地。"""
        return [poi for poi in self.pois if poi.category == category]

    @property
    def is_empty(self) -> bool:
        """数据集是否完全没有可用内容。"""
        return not (self.pois or self.nodes or self.edges)

    def summary(self) -> str:
        """一行中文摘要，供 CLI/自检输出。"""
        return (
            f"菜店 {len(self.pois_by_category('greengrocer'))} 个、"
            f"医院 {len(self.pois_by_category('clinic'))} 个、"
            f"障碍点 {len(self.obstacles)} 个、休息点 {len(self.rest_points)} 个、"
            f"路网 {len(self.nodes)} 节点/{len(self.edges)} 边"
        )


# --------------------------------------------------------------------------- #
# 基础读取
# --------------------------------------------------------------------------- #
def read_csv_rows(
    path: Path,
    issues: Optional[List[DataIssue]] = None,
) -> Tuple[List[Dict[str, str]], str, Optional[DataIssue]]:
    """读取 CSV 为字典列表，自动尝试三种编码。

    Returns:
        (行列表, 实际编码, 错误记录)；文件不存在或三种编码均失败时行列表为空。
    """
    if not path.is_file():
        return (
            [],
            "",
            DataIssue(
                source_file=path.name,
                record_id="-",
                reason=f"文件不存在：{path}。下一步：确认 data/ 下已放入该文件。",
            ),
        )

    last_error = ""
    for encoding in ENCODINGS:
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                rows = []
                row_issues: List[DataIssue] = []
                while True:
                    try:
                        row = next(reader)
                    except StopIteration:
                        break
                    except csv.Error as exc:
                        row_issues.append(DataIssue(
                            source_file=path.name, record_id=f"line#{reader.line_num}",
                            reason=f"CSV 行无法解析：{exc}。下一步：检查该行引号和字段长度。",
                        ))
                        continue
                    if None in row:
                        row_issues.append(DataIssue(
                            source_file=path.name, record_id=f"line#{reader.line_num}",
                            reason="CSV 行字段多于表头，已跳过。下一步：检查多余逗号或为含逗号的字段加引号。",
                        ))
                        continue
                    rows.append(dict(row))
        except UnicodeDecodeError as exc:
            last_error = f"{encoding} 解码失败：{exc.reason}"
            continue
        except OSError as exc:
            return (
                [],
                "",
                DataIssue(
                    source_file=path.name,
                    record_id="-",
                    reason=f"读取失败：{exc}。下一步：检查文件权限。",
                ),
            )
        # 去掉列名与值两侧空白，避免手写 CSV 的尾随空格；
        # 若首列名带 UTF-8 BOM（utf-8-sig 文件被 utf-8 解码时的典型现象），
        # 这里一并剥掉，并按 utf-8-sig 记录实际编码。
        bom_detected = False
        cleaned: List[Dict[str, str]] = []
        for row in rows:
            item: Dict[str, str] = {}
            for position, (key, value) in enumerate(row.items()):
                name = (key or "").strip()
                if position == 0 and name.startswith("\ufeff"):
                    name = name.lstrip("\ufeff")
                    bom_detected = True
                item[name] = (value or "").strip()
            cleaned.append(item)
        reported = "utf-8-sig" if bom_detected else encoding
        if issues is not None:
            issues.extend(row_issues)
            return cleaned, reported, None
        # 兼容原三元组接口；调用者仍可取得有效行和坏行说明。
        return cleaned, reported, row_issues[0] if row_issues else None

    return (
        [],
        "",
        DataIssue(
            source_file=path.name,
            record_id="-",
            reason=(
                f"三种编码均无法读取（{last_error}）。下一步：用 UTF-8 重新保存该 CSV。"
            ),
        ),
    )


def _text(row: Mapping[str, str], key: str) -> str:
    """取字符串字段（缺失为空串）。"""
    return (row.get(key) or "").strip()


def _parse_float(raw: str) -> Optional[float]:
    """解析浮点数；空串或非法返回 None。"""
    if raw is None:
        return None
    text = str(raw).strip().replace("，", "").replace(",", "")
    if not text:
        return None
    try:
        value = float(text)
        return value if math.isfinite(value) else None
    except (ValueError, OverflowError):
        return None


def _invalid_numeric_row(
    row: Mapping[str, str], fields: Sequence[str], result: LoadResult[Any], record_id: str
) -> bool:
    """非空但非法的数值使该条记录进入 data_issues，不影响其他行。"""
    invalid = [name for name in fields if _text(row, name) and _parse_float(_text(row, name)) is None]
    if not invalid:
        return False
    result.issues.append(DataIssue(
        source_file=result.path.name if result.path else "CSV", record_id=record_id,
        reason=f"数值字段 {invalid} 不是有限数字，已跳过。下一步：修正该行数字后重新导入。",
    ))
    return True


def _parse_int(raw: str) -> Optional[int]:
    """解析整数；空串或非法返回 None。"""
    value = _parse_float(raw)
    if value is None:
        return None
    return int(round(value))


def _parse_bool(raw: str) -> Optional[bool]:
    """解析布尔字段，支持 1/0、true/false、是/否、有/无。"""
    text = (raw or "").strip().lower()
    if text in {"1", "true", "yes", "y", "是", "有", "有坡道"}:
        return True
    if text in {"0", "false", "no", "n", "否", "无", "没有"}:
        return False
    return None


def _check_metadata(
    row: Mapping[str, str], source_file: str, record_id: str
) -> List[DataIssue]:
    """校验来源与置信度标注（AGENTS.md 要求每条数据标注来源）。"""
    issues: List[DataIssue] = []
    source = _text(row, "source")
    confidence = _text(row, "confidence")
    if not source:
        issues.append(
            DataIssue(
                source_file=source_file,
                record_id=record_id,
                reason="缺少 source 来源标注。下一步：补写 现场/街景/访谈/高德样例/OSM/高德。",
                severity="warning",
            )
        )
    elif source not in VALID_SOURCES:
        issues.append(
            DataIssue(
                source_file=source_file,
                record_id=record_id,
                reason=(
                    f"来源「{source}」不在约定集合 {VALID_SOURCES} 内。"
                    "下一步：改用 现场/街景/访谈/高德样例 之一。"
                ),
                severity="warning",
            )
        )
    if confidence and confidence not in VALID_CONFIDENCE:
        issues.append(
            DataIssue(
                source_file=source_file,
                record_id=record_id,
                reason=(
                    f"confidence「{confidence}」不在 high/medium/low 内。"
                    "下一步：改为三者之一。"
                ),
                severity="warning",
            )
        )
    return issues


def _convert_point(
    row: Mapping[str, str],
    source_file: str,
    record_id: str,
    lon_key: str = "lon",
    lat_key: str = "lat",
) -> Tuple[Optional[CoordResult], Optional[DataIssue], Optional[Dict[str, Any]]]:
    """读取并转换坐标。

    Returns:
        (转换结果, 错误记录, 待复核记录)，三者互斥：成功时后两者为 None。
    """
    lon = _parse_float(_text(row, lon_key))
    lat = _parse_float(_text(row, lat_key))
    if lon is None or lat is None:
        return (
            None,
            DataIssue(
                source_file=source_file,
                record_id=record_id,
                reason=(
                    f"坐标缺失或不是数字（{lon_key}={_text(row, lon_key)!r}，"
                    f"{lat_key}={_text(row, lat_key)!r}）。下一步：补齐坐标后重新导入。"
                ),
            ),
            None,
        )

    coord_system_raw = _text(row, "coord_system")
    result = to_wgs84(lon, lat, coord_system_raw)
    if result.status == STATUS_OK:
        return result, None, None

    pending = {
        "id": record_id,
        "source_file": source_file,
        "coord_system": coord_system_raw,
        "lon": lon,
        "lat": lat,
        "reason": result.reason,
    }
    return None, None, pending


def _bbox_warning(
    area: Optional[Any], lon: float, lat: float, source_file: str, record_id: str
) -> Optional[DataIssue]:
    """坐标超出片区范围时给出警告（保留记录，不丢弃）。"""
    if area is None:
        return None
    if area.contains(lon, lat):
        return None
    return DataIssue(
        source_file=source_file,
        record_id=record_id,
        reason=(
            f"坐标 ({lon}, {lat}) 超出片区「{area.name}」范围，可能是坐标偏差或录错。"
            "下一步：核对坐标或确认片区配置。"
        ),
        severity="warning",
    )


# --------------------------------------------------------------------------- #
# 各类型加载
# --------------------------------------------------------------------------- #
def load_pois(
    path: Path, category: str, area: Optional[Any] = None
) -> LoadResult[Poi]:
    """加载目的地 POI（菜店或医院）。"""
    result: LoadResult[Poi] = LoadResult(path=path)
    rows, encoding, error = read_csv_rows(path, issues=result.issues)
    result.encoding = encoding
    if error is not None:
        result.issues.append(error)
        return result

    seen_ids = set()
    for index, row in enumerate(rows, start=2):  # 第 1 行为表头
        record_id = _text(row, "id") or f"{path.stem}#{index}"
        missing = [
            key for key in ("id", "name") if not _text(row, key)
        ]
        if missing:
            result.issues.append(
                DataIssue(
                    source_file=path.name,
                    record_id=record_id,
                    reason=(
                        f"缺少必填字段 {missing}。下一步：补齐 id 与 name 后重新导入。"
                    ),
                )
            )
            continue
        if record_id in seen_ids:
            result.issues.append(
                DataIssue(
                    source_file=path.name,
                    record_id=record_id,
                    reason="id 重复，已跳过该条。下一步：改为唯一 id。",
                )
            )
            continue

        coord, coord_error, pending = _convert_point(row, path.name, record_id)
        if coord_error is not None:
            result.issues.append(coord_error)
            continue
        if pending is not None:
            result.pending_review.append(pending)
            continue
        assert coord is not None

        result.issues.extend(_check_metadata(row, path.name, record_id))
        warning = _bbox_warning(area, coord.lon, coord.lat, path.name, record_id)
        if warning is not None:
            result.issues.append(warning)

        seen_ids.add(record_id)
        result.items.append(
            Poi(
                id=record_id,
                name=_text(row, "name"),
                category=_text(row, "category") or category,
                lon=coord.lon,
                lat=coord.lat,
                coord_system="wgs84",
                address=_text(row, "address"),
                open_hours=_text(row, "open_hours"),
                has_ramp=_parse_bool(_text(row, "has_ramp")),
                source=_text(row, "source"),
                confidence=_text(row, "confidence"),
                updated_at=_text(row, "updated_at"),
                notes=_text(row, "notes"),
            )
        )
    return result


def load_obstacles(path: Path, area: Optional[Any] = None) -> LoadResult[Obstacle]:
    """加载障碍点。"""
    result: LoadResult[Obstacle] = LoadResult(path=path)
    rows, encoding, error = read_csv_rows(path, issues=result.issues)
    result.encoding = encoding
    if error is not None:
        result.issues.append(error)
        return result

    for index, row in enumerate(rows, start=2):
        record_id = _text(row, "id") or f"{path.stem}#{index}"
        missing = [key for key in ("id", "name", "type") if not _text(row, key)]
        if missing:
            result.issues.append(
                DataIssue(
                    source_file=path.name,
                    record_id=record_id,
                    reason=f"缺少必填字段 {missing}。下一步：补齐 id/name/type。",
                )
            )
            continue

        coord, coord_error, pending = _convert_point(row, path.name, record_id)
        if coord_error is not None:
            result.issues.append(coord_error)
            continue
        if pending is not None:
            result.pending_review.append(pending)
            continue
        assert coord is not None

        if _invalid_numeric_row(row, ("severity", "step_count", "slope_pct", "length_m"), result, record_id):
            continue
        severity = _parse_int(_text(row, "severity"))
        if severity is None:
            severity = 2
            result.issues.append(
                DataIssue(
                    source_file=path.name,
                    record_id=record_id,
                    reason="severity 缺失，已按中度（2）处理。下一步：补写 1/2/3。",
                    severity="warning",
                )
            )
        severity = max(1, min(3, severity))

        result.issues.extend(_check_metadata(row, path.name, record_id))
        warning = _bbox_warning(area, coord.lon, coord.lat, path.name, record_id)
        if warning is not None:
            result.issues.append(warning)

        result.items.append(
            Obstacle(
                id=record_id,
                name=_text(row, "name"),
                type=_text(row, "type"),
                lon=coord.lon,
                lat=coord.lat,
                coord_system="wgs84",
                severity=severity,
                step_count=_parse_int(_text(row, "step_count")),
                slope_pct=_parse_float(_text(row, "slope_pct")),
                length_m=_parse_float(_text(row, "length_m")),
                source=_text(row, "source"),
                confidence=_text(row, "confidence"),
                notes=_text(row, "notes"),
            )
        )
    return result


def load_rest_points(path: Path, area: Optional[Any] = None) -> LoadResult[RestPoint]:
    """加载休息点。"""
    result: LoadResult[RestPoint] = LoadResult(path=path)
    rows, encoding, error = read_csv_rows(path, issues=result.issues)
    result.encoding = encoding
    if error is not None:
        result.issues.append(error)
        return result

    for index, row in enumerate(rows, start=2):
        record_id = _text(row, "id") or f"{path.stem}#{index}"
        missing = [key for key in ("id", "name", "type") if not _text(row, key)]
        if missing:
            result.issues.append(
                DataIssue(
                    source_file=path.name,
                    record_id=record_id,
                    reason=f"缺少必填字段 {missing}。下一步：补齐 id/name/type。",
                )
            )
            continue

        coord, coord_error, pending = _convert_point(row, path.name, record_id)
        if coord_error is not None:
            result.issues.append(coord_error)
            continue
        if pending is not None:
            result.pending_review.append(pending)
            continue
        assert coord is not None

        result.issues.extend(_check_metadata(row, path.name, record_id))
        warning = _bbox_warning(area, coord.lon, coord.lat, path.name, record_id)
        if warning is not None:
            result.issues.append(warning)

        if _invalid_numeric_row(row, ("seats",), result, record_id):
            continue
        result.items.append(
            RestPoint(
                id=record_id,
                name=_text(row, "name"),
                type=_text(row, "type"),
                lon=coord.lon,
                lat=coord.lat,
                coord_system="wgs84",
                seats=_parse_int(_text(row, "seats")),
                source=_text(row, "source"),
                confidence=_text(row, "confidence"),
                notes=_text(row, "notes"),
            )
        )
    return result


def load_graph_nodes(path: Path, area: Optional[Any] = None) -> LoadResult[GraphNode]:
    """加载路网节点。"""
    result: LoadResult[GraphNode] = LoadResult(path=path)
    rows, encoding, error = read_csv_rows(path, issues=result.issues)
    result.encoding = encoding
    if error is not None:
        result.issues.append(error)
        return result

    for index, row in enumerate(rows, start=2):
        node_id = _text(row, "node_id") or _text(row, "id") or f"{path.stem}#{index}"
        coord, coord_error, pending = _convert_point(row, path.name, node_id)
        if coord_error is not None:
            result.issues.append(coord_error)
            continue
        if pending is not None:
            result.pending_review.append(pending)
            continue
        assert coord is not None
        result.items.append(
            GraphNode(
                node_id=node_id,
                lon=coord.lon,
                lat=coord.lat,
                coord_system="wgs84",
                source=_text(row, "source"),
            )
        )
    return result


def load_graph_edges(path: Path) -> LoadResult[GraphEdge]:
    """加载路网边。"""
    result: LoadResult[GraphEdge] = LoadResult(path=path)
    rows, encoding, error = read_csv_rows(path, issues=result.issues)
    result.encoding = encoding
    if error is not None:
        result.issues.append(error)
        return result

    for index, row in enumerate(rows, start=2):
        u = _text(row, "u")
        v = _text(row, "v")
        record_id = f"{u}-{v}" if u and v else f"{path.stem}#{index}"
        if not u or not v:
            result.issues.append(
                DataIssue(
                    source_file=path.name,
                    record_id=record_id,
                    reason="边缺少端点 u 或 v。下一步：补齐两端节点编号。",
                )
            )
            continue
        if u == v:
            result.issues.append(
                DataIssue(
                    source_file=path.name,
                    record_id=record_id,
                    reason="边两端为同一节点（自环），已跳过。",
                )
            )
            continue

        if _invalid_numeric_row(row, ("length_m", "walk_type", "step_count", "slope_pct"), result, record_id):
            continue
        length_m = _parse_float(_text(row, "length_m"))
        if length_m is None or length_m <= 0:
            result.issues.append(
                DataIssue(
                    source_file=path.name,
                    record_id=record_id,
                    reason="length_m 缺失或不大于 0。下一步：补写正的米制长度。",
                )
            )
            continue

        walk_type = _parse_int(_text(row, "walk_type"))
        if walk_type is None:
            walk_type = WALK_TYPE_NORMAL
            result.issues.append(
                DataIssue(
                    source_file=path.name,
                    record_id=record_id,
                    reason="walk_type 缺失，已按普通步道（0）处理。",
                    severity="warning",
                )
            )

        result.items.append(
            GraphEdge(
                u=u,
                v=v,
                length_m=length_m,
                walk_type=walk_type,
                step_count=_parse_int(_text(row, "step_count")),
                slope_pct=_parse_float(_text(row, "slope_pct")),
                surface=_text(row, "surface"),
                source=_text(row, "source"),
                pedestrian_uncertain=_parse_bool(_text(row, "pedestrian_uncertain")) is True,
            )
        )
    return result


# --------------------------------------------------------------------------- #
# 汇总加载
# --------------------------------------------------------------------------- #
def load_dataset(config: AppConfig, data_dir: Optional[Path] = None) -> Dataset:
    """加载整个离线数据集；任何单文件失败都不影响其它文件。"""
    directory = Path(data_dir) if data_dir is not None else config.runtime.data_dir
    dataset = Dataset()

    loaders = [
        (FILE_GREENGROCER, lambda path: load_pois(path, "greengrocer", config.area)),
        (FILE_CLINIC, lambda path: load_pois(path, "clinic", config.area)),
        (FILE_OBSTACLES, lambda path: load_obstacles(path, config.area)),
        (FILE_REST_POINTS, lambda path: load_rest_points(path, config.area)),
        (FILE_GRAPH_NODES, lambda path: load_graph_nodes(path, config.area)),
        (FILE_GRAPH_EDGES, load_graph_edges),
    ]

    for filename, loader in loaders:
        result = loader(directory / filename)
        if result.encoding:
            dataset.encodings[filename] = result.encoding
        dataset.issues.extend(result.issues)
        dataset.pending_review.extend(result.pending_review)

        if filename in (FILE_GREENGROCER, FILE_CLINIC):
            dataset.pois.extend(result.items)  # type: ignore[arg-type]
        elif filename == FILE_OBSTACLES:
            dataset.obstacles.extend(result.items)  # type: ignore[arg-type]
        elif filename == FILE_REST_POINTS:
            dataset.rest_points.extend(result.items)  # type: ignore[arg-type]
        elif filename == FILE_GRAPH_NODES:
            dataset.nodes.extend(result.items)  # type: ignore[arg-type]
        else:
            dataset.edges.extend(result.items)  # type: ignore[arg-type]

    if dataset.is_empty:
        dataset.issues.append(
            DataIssue(
                source_file=str(directory),
                record_id="-",
                reason=(
                    "样例数据为空或全部不可用。下一步：确认 data/ 下 6 个 CSV 存在且格式正确，"
                    "或运行 python -m src.selfcheck --data 查看具体问题。"
                ),
            )
        )
    return dataset
