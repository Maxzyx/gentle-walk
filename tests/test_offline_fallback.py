"""离线降级编排测试：无 Key、无网络、在线异常、空数据、无路线。"""

from __future__ import annotations

import dataclasses
import importlib.util
from typing import Any, Dict, List, Optional

import pytest

from src import keys, service
from src.adapters.amap_client import AmapUnavailable
from src.config import AppConfig, load_config
from src.data_loader import Dataset
from src.models import Poi
from src.service import (
    OFFLINE_BANNER,
    PlanRequest,
    ServiceState,
    describe_result,
    list_destinations,
    normalize_category,
    parse_origin,
    plan,
    prepare_state,
)


@pytest.fixture()
def config() -> AppConfig:
    """真实配置（offline_demo=true）。"""
    return load_config()


@pytest.fixture()
def online_config(config: AppConfig) -> AppConfig:
    """把 offline_demo 关掉的配置，用于测试在线分支。"""
    return dataclasses.replace(
        config, runtime=dataclasses.replace(config.runtime, offline_demo=False)
    )


def _fake_resolution(configured: bool = True) -> keys.KeyResolution:
    """构造 Key 解析结果替身。"""
    if configured:
        return keys.KeyResolution(key="DUMMY_AMAP_KEY_NOT_REAL_0000cdef", source="env")
    return keys.KeyResolution(key=None, source="none", detail="四层来源均未命中")


# --------------------------------------------------------------------------- #
# 模式判定
# --------------------------------------------------------------------------- #
def test_default_config_is_offline(config: AppConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    """默认 offline_demo=true：完全离线，且根本不会尝试联网。"""

    def _boom(*args: Any, **kwargs: Any) -> Dataset:
        raise AssertionError("离线模式下不应调用在线适配器")

    monkeypatch.setattr("src.adapters.online.load_online_dataset", _boom)

    state = prepare_state(config=config)

    assert state.mode == "offline"
    assert OFFLINE_BANNER in state.notes
    assert state.walk_graph.node_count > 0
    assert len(state.dataset.pois_by_category("greengrocer")) == 5
    assert len(state.dataset.pois_by_category("clinic")) == 3


def test_online_without_key_falls_back(
    online_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关闭离线演示但没 Key：降级离线并提示如何配置。"""
    monkeypatch.setattr(keys, "resolve_key", lambda **kwargs: _fake_resolution(False))

    state = prepare_state(config=online_config)

    assert state.mode == "offline"
    assert any("--set-key" in note for note in state.notes)
    assert OFFLINE_BANNER in state.notes


def test_online_error_falls_back_with_next_step(
    online_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """在线请求抛异常时降级离线，并保留「下一步」。"""
    monkeypatch.setattr(keys, "resolve_key", lambda **kwargs: _fake_resolution(True))

    def _raise(*args: Any, **kwargs: Any) -> Dataset:
        raise AmapUnavailable("网络请求失败：超时")

    monkeypatch.setattr("src.adapters.online.load_online_dataset", _raise)

    state = prepare_state(config=online_config)

    assert state.mode == "offline"
    assert any("在线高德不可用" in note for note in state.notes)
    assert any("下一步" in note for note in state.notes)
    # 仍然有可用的离线数据，主链路不被阻塞
    assert state.dataset.pois


def test_online_success_uses_online_dataset(
    online_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """在线可用时 mode=online，并使用在线数据。"""
    monkeypatch.setattr(keys, "resolve_key", lambda **kwargs: _fake_resolution(True))
    online_poi = Poi(
        id="amap_B001",
        name="在线菜店",
        category="greengrocer",
        lon=108.9450,
        lat=34.2610,
        source="高德样例",
        confidence="medium",
    )

    def _online(*args: Any, **kwargs: Any) -> Dataset:
        return Dataset(pois=[online_poi])

    monkeypatch.setattr("src.adapters.online.load_online_dataset", _online)

    state = prepare_state(config=online_config)

    assert state.mode == "online"
    assert state.dataset.pois[0].name == "在线菜店"
    assert any("在线高德数据" in note for note in state.notes)


def test_prepare_state_accepts_injected_dataset(config: AppConfig) -> None:
    """传入 dataset 时不再加载磁盘数据，便于测试与复用。"""
    poi = Poi(id="p1", name="店", category="greengrocer", lon=108.945, lat=34.261)

    state = prepare_state(config=config, dataset=Dataset(pois=[poi]))

    assert state.dataset.pois == [poi]
    assert state.walk_graph.node_count == 0  # 未提供路网则从该 dataset 构建


# --------------------------------------------------------------------------- #
# 规划主链路与降级
# --------------------------------------------------------------------------- #
def test_plan_offline_full_chain(config: AppConfig) -> None:
    """离线主链路：选菜店 → 出路线 → 风险提示。"""
    result = plan(PlanRequest(category="greengrocer"), config=config)

    assert result.mode == "offline"
    assert result.routes
    assert result.risks
    text = "\n".join(describe_result(result))
    assert "推荐路线" in text
    assert "风险提示" in text
    assert "样例合成" in text


def test_plan_empty_dataset_gives_next_step(config: AppConfig) -> None:
    """空数据不崩溃，提示检查数据。"""
    state = prepare_state(config=config, dataset=Dataset())

    result = plan(PlanRequest(category="greengrocer"), config=config, state=state)

    assert result.routes == []
    assert any("没有可用" in note for note in result.notes)
    assert any("下一步" in note for note in result.notes)


def test_plan_unknown_destination_id(config: AppConfig) -> None:
    """目的地编号不存在时提示用 --list 查看。"""
    result = plan(PlanRequest(destination_id="nope"), config=config)

    assert result.routes == []
    assert any("--list" in note for note in result.notes)


def test_plan_no_route_from_far_origin(config: AppConfig) -> None:
    """起点过远时给出无路线说明。"""
    result = plan(
        PlanRequest(category="clinic", origin=(109.5, 34.9)), config=config
    )

    assert result.routes == []
    assert any("下一步" in note or "换一个起点" in note for note in result.notes)


def test_plan_reports_pending_review(config: AppConfig) -> None:
    """待复核数据会提示被排除。"""
    state = prepare_state(config=config, dataset=Dataset())
    state.dataset.pending_review.append({"id": "x", "reason": "坐标无法转换"})

    result = plan(PlanRequest(category="greengrocer"), config=config, state=state)

    assert any("待复核" in note for note in result.notes)


def test_list_destinations_sorted_by_id(config: AppConfig) -> None:
    """列表按编号排序，便于对照 --list 输出。"""
    pois = list_destinations("clinic", config=config)

    assert [poi.id for poi in pois] == sorted(poi.id for poi in pois)


def test_normalize_category_aliases() -> None:
    """中文与英文别名都能识别，未知返回 None。"""
    assert normalize_category("菜店") == "greengrocer"
    assert normalize_category("社区医院") == "clinic"
    assert normalize_category("GREENGROCER") == "greengrocer"
    assert normalize_category("museum") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, (108.9455, 34.2611)),
        ("108.94,34.26", (108.94, 34.26)),
        ("108.94，34.26", (108.94, 34.26)),
    ],
)
def test_parse_origin_variants(config: AppConfig, raw: Optional[str], expected) -> None:
    """起点解析支持缺省与中文逗号。"""
    assert parse_origin(raw, config) == expected


def test_describe_result_without_routes(config: AppConfig) -> None:
    """无路线时文字输出仍然完整（含数据来源声明）。"""
    state = prepare_state(config=config, dataset=Dataset())

    result = plan(PlanRequest(), config=config, state=state)
    text = "\n".join(describe_result(result))

    assert "未能生成路线" in text
    assert "数据来源声明" in text


# --------------------------------------------------------------------------- #
# Streamlit 可选前端
# --------------------------------------------------------------------------- #
def test_ui_module_import_does_not_require_streamlit() -> None:
    """页面模块在未安装 Streamlit 时也能安全导入（CLI 主链路不受影响）。"""
    import src.ui_streamlit as ui

    assert callable(ui.main)
    assert callable(ui._render_route)


@pytest.mark.skipif(
    importlib.util.find_spec("streamlit") is not None, reason="Streamlit 已安装，跳过缺失降级测试"
)
def test_ui_main_prints_guidance_without_streamlit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """未安装 Streamlit 时给出安装指引并返回 1，而不是抛异常。"""
    import src.ui_streamlit as ui

    code = ui.main()
    out = capsys.readouterr().out

    assert code == 1
    assert "未安装 Streamlit" in out
    assert "requirements-optional.txt" in out
    assert "python -m src.cli" in out
