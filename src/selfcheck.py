"""自检脚本：``python -m src.selfcheck [--data] [--strict]``。

用途（AGENTS.md「DSH 似乎没有加载 AGENTS.md」条）：提供一条可复制的验证命令，
一次性打印「指令文件是否加载、依赖是否齐全、配置是否可读、样例数据是否达标、
高德 Key 是否就绪」。默认只报告不失败，``--strict`` 时任何不达标即返回退出码 1。
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .config import PROJECT_ROOT, AppConfig, ConfigError, load_config

AGENTS_MARKER = "AGENTS.md 已加载"
SUPPORTED_PYTHON = ((3, 9), (3, 12))

#: 离线主链路必需依赖
CORE_DEPS: Tuple[str, ...] = ("networkx",)
#: 可选依赖：缺失即降级，不影响离线主链路
OPTIONAL_DEPS: Tuple[Tuple[str, str], ...] = (
    ("pyproj", "坐标精确转换（首选）"),
    ("geotransform", "可选坐标纠偏后端"),
    ("coordTransform", "GCJ02/BD09 纠偏（纯 Python，geotransform 替代）"),
    ("streamlit", "网页前端（缺失则用 CLI）"),
    ("folium", "地图渲染（缺失则文字步骤）"),
    ("dotenv", ".env 文件读取"),
    ("keyring", "系统钥匙串存储 AMAP_KEY"),
    ("pandas", "数据质量分析（当前主链路用标准库 csv）"),
)

#: 样例数据最低数量要求（AGENTS.md「离线演示是第一优先级」）
MIN_COUNTS: Dict[str, int] = {
    "pois_greengrocer.csv": 5,
    "pois_clinic.csv": 3,
}

ENCODINGS: Tuple[str, ...] = ("utf-8", "utf-8-sig", "gbk")


# --------------------------------------------------------------------------- #
# 检查项
# --------------------------------------------------------------------------- #
def check_agents_loaded() -> Tuple[bool, str]:
    """确认 AGENTS.md 位于当前目录或父级链中，并打印加载标记。"""
    current = Path.cwd().resolve()
    for directory in (current, *current.parents):
        candidate = directory / "AGENTS.md"
        if candidate.is_file():
            return True, f"{AGENTS_MARKER}：{candidate}（{candidate.stat().st_size} 字节）"
    for name in ("CLAUDE.md",):
        if (current / name).is_file():
            return True, f"{AGENTS_MARKER}：{current / name}"
    return False, "未在项目根/工作目录父级链找到 AGENTS.md，指令可能未生效"


def check_python_version() -> Tuple[str, str]:
    """报告解释器版本；不在 3.9—3.12 时给出说明。"""
    version = sys.version_info
    text = f"Python {version.major}.{version.minor}.{version.micro}"
    (low_major, low_minor), (high_major, high_minor) = SUPPORTED_PYTHON
    inside = (low_major, low_minor) <= (version.major, version.minor) <= (
        high_major,
        high_minor,
    )
    if inside:
        return "ok", f"{text}（在 3.9—3.12 支持范围内）"
    return "warn", f"{text}（目标范围 3.9—3.12，代码按 3.9 语法编写，未在目标版本实机回归）"


def _module_version(name: str) -> str:
    """尽量拿到模块版本号，拿不到返回 ``?``。"""
    try:
        module = importlib.import_module(name)
    except Exception:  # noqa: BLE001 - 任何导入失败都视为缺失
        return ""
    raw = getattr(module, "__version__", "")
    if not raw:
        try:
            from importlib.metadata import version as _pkg_version

            raw = _pkg_version(name.replace(".", "-"))
        except Exception:  # noqa: BLE001
            raw = "?"
    return str(raw)


def check_dependencies() -> Tuple[str, List[str], List[str]]:
    """检查核心/可选依赖，返回（总体状态, 输出行, 缺失的可选依赖名）。"""
    lines: List[str] = []
    missing_core: List[str] = []
    missing_optional: List[str] = []

    lines.append("核心依赖（离线主链路必需）：")
    for name in CORE_DEPS:
        version = _module_version(name)
        if version:
            lines.append(f"  [OK]   {name:<14} {version}")
        else:
            missing_core.append(name)
            lines.append(
                f"  [缺失] {name:<14} 下一步：pip install -r requirements-core.txt"
            )

    lines.append("可选依赖（缺失自动降级）：")
    for name, purpose in OPTIONAL_DEPS:
        version = _module_version(name)
        if version:
            lines.append(f"  [OK]   {name:<14} {version:<12} {purpose}")
        else:
            missing_optional.append(name)
            lines.append(f"  [降级] {name:<14} {'-':<12} 缺失 → {purpose}不可用")

    status = "fail" if missing_core else ("warn" if missing_optional else "ok")
    return status, lines, missing_optional


def check_config() -> Tuple[str, Optional[AppConfig], List[str]]:
    """加载配置并校验片区 bbox/center 合理性。"""
    lines: List[str] = []
    try:
        config = load_config()
    except ConfigError as exc:
        return "fail", None, [f"配置加载失败：{exc}"]

    area = config.area
    lines.append(f"配置文件：{config.source_path}")
    lines.append(f"片区：{area.name}")
    lon_min, lat_min, lon_max, lat_max = area.bbox
    lines.append(
        f"片区 bbox：lon[{lon_min}, {lon_max}] lat[{lat_min}, {lat_max}]"
        f"（约 {(lon_max - lon_min) * 92000 / 1000:.1f}km x "
        f"{(lat_max - lat_min) * 111000 / 1000:.1f}km）"
    )
    status = "ok"
    if area.bbox == (0.0, 0.0, 0.0, 0.0):
        status = "fail"
        lines.append("  [失败] bbox 为全 0，下一步：检查 config/default.json 的 area.bbox")
    if not area.contains(*area.center):
        status = "warn"
        lines.append("  [警告] center 不在 bbox 内，下一步：确认片区参数是否配错")
    lines.append(f"离线演示：{config.offline_demo}（AGENTS.md 默认应为 True）")
    if not config.offline_demo:
        lines.append("  [提示] 已关闭离线演示，将尝试在线高德；无 Key/无网络会自动降级")
    scoring = config.scoring
    lines.append(
        f"评分系数：速度 {scoring.senior_speed_mps} m/s，"
        f"台阶 {scoring.stairs_per_step}/级，斜坡 {scoring.slope_base}+"
        f"{scoring.slope_per_pct}×坡度%，过街 {scoring.crossing_cost}/次"
        "（MVP 默认值，假设/待验证）"
    )
    return status, config, lines


def _read_csv_any_encoding(path: Path) -> Tuple[Optional[str], Optional[str], int]:
    """按 utf-8 → utf-8-sig → gbk 依次尝试读取，返回（编码, 错误信息, 行数-表头）。"""
    last_error = ""
    for encoding in ENCODINGS:
        try:
            text = path.read_text(encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = f"{encoding} 解码失败：{exc.reason}"
            continue
        except OSError as exc:
            return None, f"读取失败：{exc}", 0
        rows = [line for line in text.splitlines() if line.strip()]
        return encoding, None, max(len(rows) - 1, 0)
    return None, last_error or "三种编码均失败", 0


def check_data(config: AppConfig) -> Tuple[str, List[str]]:
    """统计样例数据数量与编码是否达标。"""
    lines: List[str] = []
    data_dir = config.runtime.data_dir
    if not data_dir.is_dir():
        return "fail", [f"数据目录不存在：{data_dir}。下一步：创建 data/ 并放入样例 CSV"]

    hard_fail = False
    lines.append(f"数据目录：{data_dir}")

    # 1) POI 文件：AGENTS.md 要求菜店 >= 5、医院 >= 3
    for filename, minimum in MIN_COUNTS.items():
        path = data_dir / filename
        if not path.is_file():
            hard_fail = True
            lines.append(f"  [缺失] {filename}（要求 >= {minimum} 条）")
            continue
        encoding, error, count = _read_csv_any_encoding(path)
        if error:
            hard_fail = True
            lines.append(f"  [失败] {filename}：{error}")
            continue
        if count < minimum:
            hard_fail = True
        mark = "OK" if count >= minimum else "不足"
        lines.append(
            f"  [{mark}] {filename}：{count} 条（要求 >= {minimum}，编码 {encoding}）"
        )

    # 2) 障碍点 + 休息点：合计 >= 10
    total = 0
    for name, label in (("obstacles.csv", "障碍点"), ("rest_points.csv", "休息点")):
        path = data_dir / name
        if not path.is_file():
            hard_fail = True
            lines.append(f"  [缺失] {name}（{label}）")
            continue
        encoding, error, count = _read_csv_any_encoding(path)
        if error:
            hard_fail = True
            lines.append(f"  [失败] {name}：{error}")
            continue
        total += count
        lines.append(f"  [OK] {name}：{count} 条{label}（编码 {encoding}）")
    if total < 10:
        hard_fail = True
        lines.append(f"  [不足] 障碍+休息合计 {total} 条（要求 >= 10）")
    else:
        lines.append(f"  [OK] 障碍+休息合计 {total} 条（要求 >= 10）")

    # 路网是离线主链路必要数据，缺失、空表或无可用边均不能通过 --data。
    ready = True
    for filename in ("graph_nodes.csv", "graph_edges.csv"):
        path = data_dir / filename
        if not path.is_file():
            ready = False
            hard_fail = True
            lines.append(f"  [缺失] {filename}。下一步：补齐路网文件后重试。")
            continue
        encoding, error, count = _read_csv_any_encoding(path)
        if error or count == 0:
            ready = False
            hard_fail = True
            lines.append(f"  [失败] {filename}：{error or '没有数据行'}。下一步：补充有效路网数据。")
        else:
            lines.append(f"  [OK] {filename}：{count} 条（编码 {encoding}）")
    if ready:
        try:
            from .data_loader import Dataset, load_graph_nodes, load_graph_edges
            from .graph_builder import build_graph
            nodes = load_graph_nodes(data_dir / "graph_nodes.csv", config.area)
            edges = load_graph_edges(data_dir / "graph_edges.csv")
            graph = build_graph(Dataset(nodes=nodes.items, edges=edges.items), config)
            if not graph.node_count or not graph.edge_count:
                hard_fail = True
                lines.append("  [失败] 路网没有可用节点或连接边。下一步：检查坐标、节点引用与长度。")
            else:
                lines.append(f"  [OK] 可用路网：{graph.node_count} 节点/{graph.edge_count} 连接边")
            issues = nodes.issues + edges.issues + graph.issues
            if issues or nodes.pending_review:
                lines.append(f"  [提示] 路网数据问题 {len(issues)} 条，待复核节点 {len(nodes.pending_review)} 条")
        except Exception as exc:
            hard_fail = True
            lines.append(f"  [失败] 路网无法加载：{exc}。下一步：检查两个路网 CSV 与核心依赖。")
    return ("fail" if hard_fail else "ok"), lines


def check_key(config: AppConfig) -> Tuple[str, List[str]]:
    """报告高德 Key 状态（只显示来源与打码后 4 位）。"""
    try:
        from .keys import describe_key_status
    except Exception:  # noqa: BLE001 - keys 模块尚未就绪时降级
        return "warn", ["Key 模块未就绪：下一步完成 src/keys.py 后可执行 --set-key"]
    try:
        source, masked = describe_key_status(config)
    except Exception as exc:  # noqa: BLE001
        return "warn", [f"Key 状态读取失败：{exc}"]
    if source == "none":
        return "warn", [
            "AMAP_KEY：未配置（离线演示不受影响）",
            "  下一步：python -m src.cli --set-key  （写入系统钥匙串）"
            " 或写入 .env 中的 AMAP_KEY",
        ]
    return "ok", [f"AMAP_KEY：已配置，来源 = {source}，值 = {masked}"]


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def _print_block(title: str, lines: Sequence[str]) -> None:
    """打印带分隔标题的段落。"""
    print(f"\n== {title} " + "=" * max(60 - len(title), 3))
    for line in lines:
        print(line)


def run_selfcheck(with_data: bool = False, strict: bool = False) -> int:
    """执行全部自检，返回退出码（0 通过 / 1 有不达标项）。"""
    failures: List[str] = []
    warnings: List[str] = []

    ok, message = check_agents_loaded()
    _print_block("指令文件", [message])
    if not ok:
        failures.append(message)

    status, message = check_python_version()
    _print_block("Python 版本", [message])
    if status == "warn":
        warnings.append(message)

    status, dep_lines, missing_optional = check_dependencies()
    _print_block("依赖", dep_lines)
    if status == "fail":
        failures.append("核心依赖缺失")
    elif status == "warn":
        warnings.append("可选依赖缺失： " + "、".join(missing_optional))

    status, config, config_lines = check_config()
    _print_block("配置", config_lines)
    if status == "fail":
        failures.append("配置不达标")
    elif status == "warn":
        warnings.append("配置存在警告")

    if config is not None:
        status, data_lines = check_data(config)
        _print_block("样例数据", data_lines)
        if status == "fail":
            # 数据未就绪不阻断开发期自检；--data 时视为硬性失败
            if with_data:
                failures.append("样例数据不达标")
            else:
                warnings.append("样例数据未就绪（加 --data 可视为失败）")
        elif status == "warn":
            warnings.append("样例数据尚未完全就绪")

        status, key_lines = check_key(config)
        _print_block("高德 Key", key_lines)
        if status == "warn":
            warnings.append("高德 Key 未配置（离线演示可用）")
    else:
        _print_block("样例数据", ["已跳过：配置未加载"])

    print("\n== 结论 " + "=" * 56)
    if failures:
        print(f"失败项 {len(failures)} 个：")
        for item in failures:
            print(f"  - {item}")
    elif warnings:
        print(f"主链路可用，存在 {len(warnings)} 条提示：")
        for item in warnings:
            print(f"  - {item}")
    else:
        print("全部检查通过。")

    if failures:
        return 1
    if strict and warnings:
        return 1
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        prog="python -m src.selfcheck",
        description="邻里缓行自检：指令文件、依赖、配置、样例数据、高德 Key。",
    )
    parser.add_argument(
        "--data",
        action="store_true",
        help="把样例数据不达标也视为失败（用于数据就绪后的回归）",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="任何警告也返回退出码 1",
    )
    args = parser.parse_args(argv)
    return run_selfcheck(with_data=args.data, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
