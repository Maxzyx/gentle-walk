"""命令行入口：``python -m src.cli``。

设计目标（AGENTS.md「Streamlit 不可用时 CLI 必须输出完整文字路线」）：
CLI 是**永远可用**的主前端，不依赖 Streamlit / Folium / 网络 / Key。
同时提供 Key 的三条便捷命令：``--set-key``、``--key-status``、``--clear-key``。
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from . import keys
from .config import ConfigError, load_config
from .models import CATEGORY_LABELS, SUPPORTED_CATEGORIES, category_label
from .service import (
    PlanRequest,
    ServiceState,
    describe_result,
    list_destinations,
    normalize_category,
    parse_origin,
    plan,
    prepare_state,
)

#: 示例命令，放在 --help 末尾
EPILOG = """\
常用示例：
  python -m src.cli                                  # 默认起点去最近的社区菜店
  python -m src.cli --category clinic                # 去社区医院
  python -m src.cli --from 108.9455,34.2611          # 指定起点经纬度
  python -m src.cli --list --category clinic         # 列出所有社区医院
  python -m src.cli --destination clinic_002         # 指定目的地编号
  python -m src.cli --set-key                        # 配置高德 Key（写入系统钥匙串）
  python -m src.cli --key-status                     # 查看 Key 来源（打码显示）
  python -m src.cli --clear-key                      # 删除已保存的 Key
  python -m src.cli --online                         # 尝试在线高德（需 Key 与网络）
  python -m src.cli --web                            # 启动 Streamlit 页面（若已安装）
"""


def build_parser() -> argparse.ArgumentParser:
    """构造参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m src.cli",
        description="邻里缓行 · 适老步行路线助手：为老人规划平缓优先的步行路线。",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--from",
        dest="origin",
        metavar="经度,纬度",
        help="起点坐标，例如 108.9455,34.2611；缺省用片区中心",
    )
    parser.add_argument(
        "--category",
        default="greengrocer",
        metavar="类型",
        help=f"目的地类型：{' / '.join(SUPPORTED_CATEGORIES)}（也可写 菜店 / 医院）",
    )
    parser.add_argument("--destination", metavar="编号", help="指定目的地编号（见 --list）")
    parser.add_argument("--list", action="store_true", help="列出该类目的所有目的地")
    parser.add_argument("--config", metavar="路径", help="指定配置文件（默认 config/default.json）")
    parser.add_argument("--online", action="store_true", help="尝试在线高德数据（需 Key 与网络）")

    key_group = parser.add_argument_group("高德 Key")
    key_group.add_argument("--set-key", action="store_true", help="交互式设置 AMAP_KEY")
    key_group.add_argument("--key-status", action="store_true", help="查看当前 Key 来源（打码）")
    key_group.add_argument("--clear-key", action="store_true", help="删除已保存的 AMAP_KEY")

    parser.add_argument("--web", action="store_true", help="启动 Streamlit 页面（需已安装）")
    return parser


def _print_lines(lines: Sequence[str]) -> None:
    """逐行打印。"""
    for line in lines:
        print(line)


def _handle_key_commands(args: argparse.Namespace, config) -> Optional[int]:
    """处理 Key 相关命令；返回退出码，未触发则返回 None。"""
    if args.set_key:
        ok, message = keys.prompt_and_store_key(config)
        print(message)
        return 0 if ok else 1

    if args.clear_key:
        ok, message = keys.clear_stored_key(config)
        print(message)
        return 0 if ok else 1

    if args.key_status:
        resolution = keys.resolve_key(config=config)
        print(f"AMAP_KEY 来源：{resolution.source_label}")
        print(f"当前值：{resolution.masked}")
        if resolution.detail:
            print(f"说明：{resolution.detail}")
        print()
        print(keys.key_setup_guide(config))
        return 0

    return None


def _handle_list(args: argparse.Namespace, state: ServiceState) -> int:
    """列出目的地。"""
    category = normalize_category(args.category)
    if category is None:
        print(
            f"不支持的目的地类型：{args.category!r}。"
            f"下一步：改用 {' 或 '.join(SUPPORTED_CATEGORIES)}。"
        )
        return 2

    pois = list_destinations(category, state=state)
    print(f"{CATEGORY_LABELS.get(category, category)}（共 {len(pois)} 个）：")
    if not pois:
        print("  （无可用数据，请运行 python -m src.selfcheck --data 检查）")
        return 0
    for poi in pois:
        ramp = {True: "有坡道", False: "无坡道", None: "坡道未知"}[poi.has_ramp]
        print(
            f"  {poi.id:<12} {poi.name}（{poi.address or '地址未标注'}）"
            f"  {ramp}  来源：{poi.source or '未标注'}"
        )
    return 0


def _handle_web(args: argparse.Namespace, config) -> Optional[int]:
    """启动 Streamlit 页面；未安装时给出 CLI 兜底指引。"""
    if not args.web:
        return None

    if importlib.util.find_spec("streamlit") is None:
        print("未安装 Streamlit，已跳过网页模式。")
        print("下一步：pip install -r requirements-optional.txt 后重试 --web；")
        print("        或直接用 CLI 文字模式（无需任何前端依赖）。")
        return None

    ui_path = Path(__file__).resolve().parent / "ui_streamlit.py"
    if not ui_path.is_file():
        print("未找到页面文件 src/ui_streamlit.py，已跳过网页模式。")
        print("下一步：确认文件存在，或直接用 CLI 文字模式。")
        return None

    import subprocess  # noqa: PLC0415 - 仅网页模式需要

    print(f"启动 Streamlit 页面：{ui_path}")
    try:
        child_env = os.environ.copy()
        child_env["APP_CONFIG"] = str(config.source_path.resolve())
        child_env["OFFLINE_DEMO"] = "true" if config.offline_demo else "false"
        child_env["DATA_DIR"] = str(config.runtime.data_dir.resolve())
        child_env["CACHE_DIR"] = str(config.runtime.cache_dir.resolve())
        return subprocess.call(
            [sys.executable, "-m", "streamlit", "run", str(ui_path)], env=child_env
        )
    except OSError as exc:
        print(f"启动失败：{exc}。下一步：改用 CLI 文字模式。")
        return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行主入口。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(Path(args.config)) if args.config else load_config()
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 2

    if args.online and config.offline_demo:
        config = dataclasses.replace(
            config, runtime=dataclasses.replace(config.runtime, offline_demo=False)
        )

    key_exit = _handle_key_commands(args, config)
    if key_exit is not None:
        return key_exit

    try:
        origin = parse_origin(args.origin, config)
    except ValueError as exc:
        print(f"起点参数错误：{exc}")
        return 2

    if args.list:
        return _handle_list(args, prepare_state(config=config))

    web_exit = _handle_web(args, config)
    if web_exit is not None:
        return web_exit

    category = normalize_category(args.category)
    if category is None:
        print(
            f"不支持的目的地类型：{args.category!r}。"
            f"下一步：改用 {' 或 '.join(SUPPORTED_CATEGORIES)}。"
        )
        return 2

    state = prepare_state(config=config)
    result = plan(
        PlanRequest(category=category, origin=origin, destination_id=args.destination),
        config=config,
        state=state,
    )
    _print_lines(describe_result(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
