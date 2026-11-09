"""数据模型：POI、障碍点、休息点、路网、步行段、路线与风险。

设计约定：
- 全部用 ``@dataclass(frozen=True)``，加载后不再修改，避免下游意外改写；
- 坐标一律 ``(lon, lat)`` 且入库后统一 WGS84（转换在 ``data_loader`` 完成）；
- ``walk_type`` 取值与高德步行规划 2.0 保持一致（AGENTS.md：阶梯 21、斜坡 22、
  人行横道 3、扶梯 9、直梯 10），线下样例路网复用同一套类型号，
  保证离线与在线数据同构、评分逻辑只有一份。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

Point = Tuple[float, float]

# --------------------------------------------------------------------------- #
# 目的地类目
# --------------------------------------------------------------------------- #
CATEGORY_GREENGROCER = "greengrocer"
CATEGORY_CLINIC = "clinic"
CATEGORY_LABELS: Dict[str, str] = {
    CATEGORY_GREENGROCER: "社区菜店",
    CATEGORY_CLINIC: "社区医院",
}
SUPPORTED_CATEGORIES: Tuple[str, ...] = (CATEGORY_GREENGROCER, CATEGORY_CLINIC)


def category_label(category: str) -> str:
    """类目中文名。"""
    return CATEGORY_LABELS.get(category, category)


# --------------------------------------------------------------------------- #
# 道路类型（与高德 WalkStep.walk_type 对齐）
# --------------------------------------------------------------------------- #
WALK_TYPE_NORMAL = 0
WALK_TYPE_CROSSING = 3
WALK_TYPE_ESCALATOR = 9
WALK_TYPE_ELEVATOR = 10
WALK_TYPE_STAIRS = 21
WALK_TYPE_SLOPE = 22

#: 类型号 → 中文名称（AGENTS.md 第 42 行明确 21=阶梯、22=斜坡、3=人行横道、9=扶梯、10=直梯）
WALK_TYPE_LABELS: Dict[int, str] = {
    WALK_TYPE_NORMAL: "普通步道",
    WALK_TYPE_CROSSING: "人行横道",
    WALK_TYPE_ESCALATOR: "扶梯",
    WALK_TYPE_ELEVATOR: "直梯",
    WALK_TYPE_STAIRS: "阶梯",
    WALK_TYPE_SLOPE: "斜坡",
}

#: 未识别类型号的处理说明（风险提示会引用）
UNKNOWN_WALK_TYPE_LABEL = "未识别路型"


def walk_type_label(code: Optional[int]) -> str:
    """路型中文名；未知类型返回「未识别路型」。"""
    if code is None:
        return UNKNOWN_WALK_TYPE_LABEL
    return WALK_TYPE_LABELS.get(int(code), UNKNOWN_WALK_TYPE_LABEL)


def is_stairs(code: Optional[int]) -> bool:
    """是否为阶梯（高德 walk_type=21）。"""
    return code is not None and int(code) == WALK_TYPE_STAIRS


def is_slope(code: Optional[int]) -> bool:
    """是否为斜坡（高德 walk_type=22）。"""
    return code is not None and int(code) == WALK_TYPE_SLOPE


def is_crossing(code: Optional[int]) -> bool:
    """是否为人行横道（高德 walk_type=3）。"""
    return code is not None and int(code) == WALK_TYPE_CROSSING


def is_vertical_convenience(code: Optional[int]) -> bool:
    """是否为扶梯/直梯（9/10，属便利设施，不施加惩罚）。"""
    return code is not None and int(code) in {WALK_TYPE_ESCALATOR, WALK_TYPE_ELEVATOR}


# --------------------------------------------------------------------------- #
# 来源与置信度（AGENTS.md：每条数据标注来源）
# --------------------------------------------------------------------------- #
VALID_SOURCES: Tuple[str, ...] = (
    "现场",
    "街景",
    "访谈",
    "高德样例",
    # AGENTS.md 要求「不编造已抓取大量真实数据」：本项目 MVP 自带的是人工编写的
    # 演示数据，必须如实标注为合成数据，不能冒充现场采集结果。
    "样例合成",
    # OpenStreetMap（ODbL 1.0）：公开路网数据的真实来源标注，不是现场采集
    "OSM",
    # 高德开放平台正式接口数据（POI），按平台条款仅用于本项目演示
    "高德",
)
VALID_CONFIDENCE: Tuple[str, ...] = ("high", "medium", "low")

SOURCE_LABELS: Dict[str, str] = {
    "现场": "现场调研",
    "街景": "街景判读",
    "访谈": "居民访谈",
    "高德样例": "高德开放平台样例",
    "样例合成": "合成演示数据（非真实采集）",
}


def source_label(source: str) -> str:
    """来源中文说明。"""
    return SOURCE_LABELS.get(source, source or "未标注来源")


# --------------------------------------------------------------------------- #
# 点位数据
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Poi:
    """目的地：社区菜店或社区医院。"""

    id: str
    name: str
    category: str
    lon: float
    lat: float
    coord_system: str = "wgs84"
    address: str = ""
    open_hours: str = ""
    has_ramp: Optional[bool] = None
    source: str = ""
    confidence: str = ""
    updated_at: str = ""
    notes: str = ""

    @property
    def point(self) -> Point:
        """``(lon, lat)``。"""
        return (self.lon, self.lat)

    @property
    def display_name(self) -> str:
        """带类目的显示名，如「社区菜店-某某蔬菜店」。"""
        return f"{category_label(self.category)}-{self.name}"


@dataclass(frozen=True)
class Obstacle:
    """障碍点：台阶、陡坡、过街、施工、湿滑、无护栏、狭窄、路缘。"""

    id: str
    name: str
    type: str
    lon: float
    lat: float
    coord_system: str = "wgs84"
    severity: int = 2
    step_count: Optional[int] = None
    slope_pct: Optional[float] = None
    length_m: Optional[float] = None
    source: str = ""
    confidence: str = ""
    notes: str = ""

    @property
    def point(self) -> Point:
        """``(lon, lat)``。"""
        return (self.lon, self.lat)


@dataclass(frozen=True)
class RestPoint:
    """休息点：长椅、遮阴处、店铺座位、公共座椅。"""

    id: str
    name: str
    type: str
    lon: float
    lat: float
    coord_system: str = "wgs84"
    seats: Optional[int] = None
    source: str = ""
    confidence: str = ""
    notes: str = ""

    @property
    def point(self) -> Point:
        """``(lon, lat)``。"""
        return (self.lon, self.lat)


#: 障碍类型中文名
OBSTACLE_TYPE_LABELS: Dict[str, str] = {
    "stairs": "台阶",
    "slope": "陡坡",
    "crossing": "过街",
    "construction": "施工",
    "slippery": "湿滑",
    "no_railing": "无护栏",
    "narrow": "狭窄",
    "curb": "路缘石",
}

#: 休息点类型中文名
REST_TYPE_LABELS: Dict[str, str] = {
    "bench": "长椅",
    "shade": "遮阴处",
    "shop_seat": "店铺座位",
    "public_seat": "公共座椅",
}


def obstacle_type_label(obstacle_type: str) -> str:
    """障碍类型中文名。"""
    return OBSTACLE_TYPE_LABELS.get(obstacle_type, obstacle_type or "未知障碍")


def rest_type_label(rest_type: str) -> str:
    """休息点类型中文名。"""
    return REST_TYPE_LABELS.get(rest_type, rest_type or "休息点")


# --------------------------------------------------------------------------- #
# 路网
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GraphNode:
    """路网节点。"""

    node_id: str
    lon: float
    lat: float
    coord_system: str = "wgs84"
    source: str = ""

    @property
    def point(self) -> Point:
        """``(lon, lat)``。"""
        return (self.lon, self.lat)


@dataclass(frozen=True)
class GraphEdge:
    """路网边：连接两个节点，携带路型与坡度信息。"""

    u: str
    v: str
    length_m: float
    walk_type: int = WALK_TYPE_NORMAL
    step_count: Optional[int] = None
    slope_pct: Optional[float] = None
    surface: str = ""
    source: str = ""
    pedestrian_uncertain: bool = False

    @property
    def endpoints(self) -> Tuple[str, str]:
        """端点元组。"""
        return (self.u, self.v)


# --------------------------------------------------------------------------- #
# 步行段（在线高德 / 离线路网共用结构）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WalkStep:
    """一段步行路径。``walk_type`` 直接使用高德类型号。"""

    instruction: str
    distance_m: float
    walk_type: int = WALK_TYPE_NORMAL
    step_count: Optional[int] = None
    slope_pct: Optional[float] = None
    road_name: str = ""
    polyline: Tuple[Point, ...] = ()
    source: str = ""

    @property
    def kind_label(self) -> str:
        """路型中文名。"""
        return walk_type_label(self.walk_type)


# --------------------------------------------------------------------------- #
# 路线与风险
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RouteStats:
    """路线的量化统计，供评分与风险分级共用。"""

    distance_m: float = 0.0
    duration_min: float = 0.0
    stairs_segments: int = 0
    stairs_total: int = 0
    max_slope_pct: float = 0.0
    crossings: int = 0
    obstacle_count: int = 0
    max_obstacle_severity: int = 0
    fatigue_segments: int = 0
    rest_points_nearby: int = 0
    no_railing_m: float = 0.0
    unknown_segments: int = 0
    stairs_unknown_segments: int = 0
    slope_segments: int = 0
    slope_unknown_segments: int = 0
    pedestrian_uncertain_segments: int = 0
    unverified_connector_m: float = 0.0



@dataclass(frozen=True)
class RouteStep:
    """输出用的单步描述（已带中文路型与说明）。"""

    index: int
    instruction: str
    distance_m: float
    walk_type: int
    kind_label: str
    penalty: float = 0.0
    note: str = ""


@dataclass(frozen=True)
class RiskItem:
    """一条风险提示。"""

    level: str
    title: str
    detail: str
    source: str = ""
    confidence: str = ""


@dataclass(frozen=True)
class Route:
    """一条候选路线。"""

    route_id: str
    destination: Poi
    node_path: Tuple[str, ...]
    polyline: Tuple[Point, ...]
    steps: Tuple[RouteStep, ...]
    stats: RouteStats
    cost: float
    risks: Tuple[RiskItem, ...] = ()
    is_primary: bool = False

    @property
    def distance_m(self) -> float:
        """路线总长度（米）。"""
        return self.stats.distance_m

    @property
    def duration_min(self) -> float:
        """预计步行时长（分钟）。"""
        return self.stats.duration_min


@dataclass(frozen=True)
class DataIssue:
    """一条数据问题记录（坏数据跳过但不整体失败）。"""

    source_file: str
    record_id: str
    reason: str
    severity: str = "error"  # error=已跳过；warning=已用默认值

    @property
    def is_error(self) -> bool:
        """是否为导致记录被跳过的错误。"""
        return self.severity == "error"


@dataclass
class RouteResult:
    """一次规划请求的完整结果。"""

    origin: Point
    category: str
    mode: str = "offline"
    routes: List[Route] = field(default_factory=list)
    risks: List[RiskItem] = field(default_factory=list)
    data_issues: List[DataIssue] = field(default_factory=list)
    pending_review: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def has_routes(self) -> bool:
        """是否至少有一条可行路线。"""
        return bool(self.routes)

    @property
    def primary(self) -> Optional[Route]:
        """主路线（第一条）。"""
        return self.routes[0] if self.routes else None
