"""Streamlit 页面：可选前端。

AGENTS.md：Streamlit 不可用时 CLI 必须输出完整文字路线；页面只是「锦上添花」。
因此本模块：

- 顶层不 import streamlit，未安装时 ``python -m src.ui_streamlit`` 只打印提示，不报错；
- 地图（Folium）同样可选，瓦片不可用或未安装时自动降级为文字步骤 + 风险清单；
- 所有业务逻辑调用 ``src/service.py``，与 CLI 共用同一套数据与评分。
"""

from __future__ import annotations

import sys
from html import escape
from pathlib import Path
from typing import Optional, Tuple

# 兼容两种运行方式：streamlit run src/ui_streamlit.py（脚本）与 python -m src.ui_streamlit（模块）
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import keys, service  # noqa: E402
from src.router import stairs_text, slope_text  # noqa: E402
from src.config import AppConfig, ConfigError, load_config  # noqa: E402
from src.models import CATEGORY_LABELS, SUPPORTED_CATEGORIES, RouteResult  # noqa: E402
from src.service import (  # noqa: E402
    DATA_DISCLAIMER,
    OFFLINE_BANNER,
    PlanRequest,
    describe_result,
    list_destinations,
    prepare_state,
)

RISK_ICON = {"高": "🔴", "中": "🟠", "低": "🟢"}


def _render_map(st: object, result: RouteResult) -> None:
    """渲染地图；Folium/瓦片不可用时降级为文字说明。"""
    try:
        import folium  # noqa: PLC0415 - 可选依赖
        from streamlit_folium import st_folium  # noqa: PLC0415
    except ImportError:
        st.caption("地图组件未安装，已降级为文字步骤（路线信息不受影响）。")
        return

    primary = result.routes[0]
    center = primary.polyline[0]
    map_object = folium.Map(location=[center[1], center[0]], zoom_start=16)
    for index, route in enumerate(result.routes):
        points = [(lat, lon) for lon, lat in route.polyline]
        color = "#1f77b4" if index == 0 else "#999999"
        folium.PolyLine(points, color=color, weight=5 if index == 0 else 3, opacity=0.9).add_to(
            map_object
        )
    destination = primary.destination
    folium.Marker(
        [destination.lat, destination.lon], tooltip=escape(destination.display_name)
    ).add_to(map_object)
    st_folium(map_object, width=None, height=420)


def _render_risks(st: object, result: RouteResult) -> None:
    """渲染风险清单。"""
    if not result.risks:
        st.info("未发现明显风险。")
        return
    for risk in result.risks:
        icon = RISK_ICON.get(risk.level, "⚪")
        st.markdown(f"**{icon} [{risk.level}] {risk.title}**  \n{risk.detail}")
        st.caption(f"来源：{risk.source}；置信度：{risk.confidence}")


def _render_route(st: object, result: RouteResult) -> None:
    """渲染主路线指标、步骤与备选。"""
    primary = result.routes[0]
    stats = primary.stats

    columns = st.columns(5)
    columns[0].metric("估计至少" if stats.unverified_connector_m else "距离", f"{stats.distance_m:.0f} 米")
    columns[1].metric("预计步行", f"{stats.duration_min:.0f} 分钟")
    columns[2].metric("台阶", stairs_text(stats))
    columns[3].metric("最大坡度", slope_text(stats))
    columns[4].metric("过街", f"{stats.crossings} 次")

    st.subheader(f"目的地：{primary.destination.display_name}")
    st.caption(f"地址：{primary.destination.address or '未标注'}｜评分 {max(primary.cost, 0.0):.2f}（越低越平缓）")

    st.markdown("**路线步骤**")
    for step in primary.steps:
        suffix = f"（{step.note}）" if step.note else ""
        st.markdown(f"{step.index}. {step.instruction}{suffix}")

    st.markdown("**风险提示**")
    _render_risks(st, result)

    alternatives = result.routes[1:]
    if alternatives:
        with st.expander(f"备选路线（{len(alternatives)} 条）"):
            for index, route in enumerate(alternatives, start=1):
                st.markdown(
                    f"**备选 {index}**：距离约 {route.stats.distance_m:.0f} 米、"
                    f"台阶 {stairs_text(route.stats)}、"
                    f"最大坡度 {slope_text(route.stats)}、"
                    f"过街 {route.stats.crossings} 次、评分 {max(route.cost, 0.0):.2f}"
                )
                for step in route.steps:
                    st.caption(f"· {step.instruction}")


def _render_sidebar(st: object, config: AppConfig) -> Tuple[Optional[PlanRequest], service.ServiceState]:
    """侧边栏参数区，返回待执行的请求；未点击按钮时返回 None。"""
    st.sidebar.header("出行参数")

    category_label = st.sidebar.radio(
        "目的地类型",
        options=[CATEGORY_LABELS[category] for category in SUPPORTED_CATEGORIES],
        index=0,
    )
    category = SUPPORTED_CATEGORIES[
        [CATEGORY_LABELS[c] for c in SUPPORTED_CATEGORIES].index(category_label)
    ]

    center_lon, center_lat = config.area.center
    col_lon, col_lat = st.sidebar.columns(2)
    origin_lon = col_lon.number_input("起点经度", value=center_lon, format="%.6f")
    origin_lat = col_lat.number_input("起点纬度", value=center_lat, format="%.6f")

    state = prepare_state(config=config)
    pois = list_destinations(category, state=state)
    options = ["自动推荐（比较全部）"] + [f"{poi.id}｜{poi.name}" for poi in pois]
    choice = st.sidebar.selectbox("目的地", options=options, index=0)
    destination_id = None if choice.startswith("自动推荐") else choice.split("｜")[0]

    st.sidebar.markdown("---")
    st.sidebar.caption(f"AMAP_KEY：{keys.resolve_key(config=config).masked}")
    if state.mode == "offline":
        st.sidebar.info(OFFLINE_BANNER)
    else:
        st.sidebar.success("在线高德模式")

    if st.sidebar.button("规划路线", type="primary"):
        return PlanRequest(
            category=category,
            origin=(float(origin_lon), float(origin_lat)),
            destination_id=destination_id,
        ), state
    return None, state


def _render_result(st: object, result: RouteResult) -> None:
    """渲染一次规划结果。"""
    mode_text = "在线高德" if result.mode == "online" else "离线演示"
    if result.mode == "offline":
        st.info(f"当前为{mode_text}模式：数据来自 data/ 下的样例 CSV。")
    else:
        st.success(f"当前为{mode_text}模式。")

    for note in result.notes:
        st.caption(f"· {note}")

    if not result.routes:
        st.warning("未能生成路线，请查看上面的提示。")
        with st.expander("文字输出（CLI 同等内容）"):
            st.code("\n".join(describe_result(result)))
        return

    _render_route(st, result)
    _render_map(st, result)

    with st.expander("数据说明与问题"):
        st.markdown(f"数据来源声明：{DATA_DISCLAIMER}")
        if result.pending_review:
            st.markdown(f"- 待复核条目：{len(result.pending_review)} 条（未参与规划）")
        if result.data_issues:
            for issue in result.data_issues:
                st.markdown(
                    f"- [{issue.severity}] {issue.source_file}/{issue.record_id}：{issue.reason}"
                )
        if not result.pending_review and not result.data_issues:
            st.markdown("- 未发现数据问题")

    with st.expander("文字输出（与 CLI 一致，可复制）"):
        st.code("\n".join(describe_result(result)))


def main() -> int:
    """页面入口。"""
    try:
        import streamlit as st  # noqa: PLC0415 - 可选依赖
    except ImportError:
        print("未安装 Streamlit，无法启动网页模式。")
        print("下一步：pip install -r requirements-optional.txt；")
        print("        或直接使用 CLI：python -m src.cli（文字输出完整，无需前端依赖）。")
        return 1

    st.set_page_config(page_title="邻里缓行 · 适老步行路线助手", page_icon="🧭", layout="wide")
    st.title("🧭 邻里缓行 · 适老步行路线助手")
    st.caption("为老人规划「平缓优先」的步行路线：少台阶、少陡坡、少过街，并给出风险提示。")

    try:
        config = load_config()
    except ConfigError as exc:
        st.error(f"配置错误：{exc}")
        return 2

    request, state = _render_sidebar(st, config)
    if request is not None:
        with st.spinner("正在规划路线…"):
            st.session_state["result"] = service.plan(request, config=config, state=state)

    result = st.session_state.get("result")
    if result is None:
        st.info("请在左侧选择目的地类型与起点，然后点击「规划路线」。")
        st.markdown("离线模式下无需网络与 AMAP_KEY，数据来自 data/ 下的样例 CSV。")
        return 0

    _render_result(st, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
