"""``src/geo`` 坐标与几何测试：转换降级链、待复核清单、米制投影。"""

from __future__ import annotations

from typing import Dict, Tuple

import pytest

from src.geo import coord_transform as ct
from src.geo import geo_utils as gu


@pytest.fixture(autouse=True)
def _clear_cache():
    """每个用例前后清空后端缓存，避免互相污染。"""
    ct.reset_backend_cache()
    yield
    ct.reset_backend_cache()


# --------------------------------------------------------------------------- #
# 系统标识
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("wgs84", "wgs84"),
        ("WGS-84", "wgs84"),
        ("EPSG:4326", "wgs84"),
        ("gcj02", "gcj02"),
        ("GCJ-02", "gcj02"),
        ("高德", "gcj02"),
        ("bd09", "bd09"),
        ("百度", "bd09"),
        ("", None),
        (None, None),
        ("火星坐标系X", None),
    ],
)
def test_normalize_system(raw, expected) -> None:
    """坐标系统别名规范化。"""
    assert ct.normalize_system(raw) == expected


def test_system_label_readable() -> None:
    """中文标签便于风险提示。"""
    assert "WGS84" in ct.system_label("wgs84")
    assert "GCJ02" in ct.system_label("gcj02")
    assert "未知" in ct.system_label(None)


# --------------------------------------------------------------------------- #
# WGS84 直通
# --------------------------------------------------------------------------- #
def test_wgs84_passthrough_is_ok() -> None:
    """已经是 WGS84 时原样通过，不调用任何第三方库。"""
    result = ct.to_wgs84(108.9455, 34.2611, "wgs84", converters={})
    assert result.status == ct.STATUS_OK
    assert result.method == "passthrough"
    assert (result.lon, result.lat) == (108.9455, 34.2611)
    assert result.ok and not result.needs_review


# --------------------------------------------------------------------------- #
# GCJ02 有转换器
# --------------------------------------------------------------------------- #
def test_gcj02_converted_when_converter_available() -> None:
    """注入转换器后应返回 geotransform 方法且坐标已纠偏。"""

    def fake_converter(lon: float, lat: float) -> Tuple[float, float]:
        return (lon - 0.005, lat - 0.002)

    result = ct.to_wgs84(108.9455, 34.2611, "gcj02", converters={"gcj02": fake_converter})

    assert result.status == ct.STATUS_OK
    assert result.method == "geotransform"
    assert result.lon == pytest.approx(108.9405)
    assert result.lat == pytest.approx(34.2591)


def test_gcj02_converter_exception_falls_to_pending() -> None:
    """第三方库抛错时降级为待复核，不抛出异常。"""

    def broken(lon: float, lat: float) -> Tuple[float, float]:
        raise RuntimeError("proj database missing")

    result = ct.to_wgs84(108.9455, 34.2611, "gcj02", converters={"gcj02": broken})

    assert result.status == ct.STATUS_PENDING
    assert (result.lon, result.lat) == (108.9455, 34.2611)
    assert "geotransform" in result.reason


# --------------------------------------------------------------------------- #
# GCJ02 转换后端缺失（模拟场景）
# --------------------------------------------------------------------------- #
def test_gcj02_without_converter_marks_pending_and_keeps_coords() -> None:
    """缺 geotransform 时不纠偏、不崩溃，坐标保持原值并标注待复核。"""
    result = ct.to_wgs84(108.9455, 34.2611, "gcj02", converters={})

    assert result.status == ct.STATUS_PENDING
    assert result.needs_review is True
    assert (result.lon, result.lat) == (108.9455, 34.2611)
    assert "geotransform" in result.reason
    assert "以现场为准" in result.reason
    assert "下一步" in result.reason


def test_bd09_without_converter_marks_pending() -> None:
    """BD09 同样走待复核路径。"""
    result = ct.to_wgs84(108.9, 34.2, "bd09", converters={})
    assert result.status == ct.STATUS_PENDING
    assert result.needs_review


# --------------------------------------------------------------------------- #
# 缺失/非法 coord_system
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("system", [None, "", "   ", "utm-49n"])
def test_missing_or_unknown_system_marks_pending(system) -> None:
    """coord_system 缺失或非法时进待复核，不整体失败。"""
    result = ct.to_wgs84(108.9, 34.2, system, converters={})
    assert result.status == ct.STATUS_PENDING
    assert "coord_system" in result.reason
    assert (result.lon, result.lat) == (108.9, 34.2)


# --------------------------------------------------------------------------- #
# 批量转换
# --------------------------------------------------------------------------- #
def test_convert_records_splits_ok_and_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """批量转换应把 WGS84 放入已转换、GCJ02（无库）放入待复核。"""
    # 显式禁用转换后端，测试结果不依赖可选库的安装状态
    monkeypatch.setattr(ct, "_discover_geotransform", lambda: {})
    ct.reset_backend_cache()
    records = [
        {"id": "a", "lon": 108.94, "lat": 34.26, "coord_system": "wgs84"},
        {"id": "b", "lon": 108.95, "lat": 34.27, "coord_system": "gcj02"},
    ]

    converted, pending = ct.convert_records(records)

    assert [item["id"] for item in converted] == ["a"]
    assert [item["id"] for item in pending] == ["b"]
    assert converted[0]["coord_system"] == "wgs84"
    assert converted[0]["coord_method"] == "passthrough"
    assert pending[0]["coord_system"] == "gcj02"
    assert "review_reason" in pending[0]


def test_convert_records_handles_bad_numbers() -> None:
    """经纬度缺失或非数字时进待复核并给出下一步。"""
    records = [
        {"id": "bad-missing", "lat": 34.2, "coord_system": "wgs84"},
        {"id": "bad-type", "lon": "东经108.9", "lat": 34.2, "coord_system": "wgs84"},
    ]

    converted, pending = ct.convert_records(records)

    assert converted == []
    assert [item["id"] for item in pending] == ["bad-missing", "bad-type"]
    assert all("下一步" in item["review_reason"] for item in pending)


def test_convert_records_does_not_mutate_input() -> None:
    """批量转换不得修改调用方传入的原始记录。"""
    original = {"id": "a", "lon": 108.94, "lat": 34.26, "coord_system": "wgs84"}
    records = [original]

    ct.convert_records(records)

    assert original == {"id": "a", "lon": 108.94, "lat": 34.26, "coord_system": "wgs84"}


# --------------------------------------------------------------------------- #
# 返回值解析与后端探测
# --------------------------------------------------------------------------- #
def test_unwrap_pair_supports_common_shapes() -> None:
    """兼容 tuple/list、lon/lat 属性、x/y 属性三种返回形态。"""

    class WithLonLat:
        lon = 108.1
        lat = 34.1

    class WithXY:
        x = 108.2
        y = 34.2

    assert ct._unwrap_pair((108.0, 34.0)) == (108.0, 34.0)
    assert ct._unwrap_pair([108.0, 34.0]) == (108.0, 34.0)
    assert ct._unwrap_pair(WithLonLat()) == (108.1, 34.1)
    assert ct._unwrap_pair(WithXY()) == (108.2, 34.2)
    with pytest.raises(TypeError):
        ct._unwrap_pair(object())


def test_detect_backends_returns_known_keys() -> None:
    """后端探测返回固定键，缺失值为 None。"""
    backends = ct.detect_backends()
    assert set(backends) == {"pyproj", "geotransform", "coordtransform"}


def test_backend_summary_mentions_degradation_when_missing() -> None:
    """无任何后端时，说明文字必须点出降级与离线不受影响。"""
    summary = ct.backend_summary()
    assert isinstance(summary, str) and summary
    backends = ct.detect_backends()
    if not any(backends[name] for name in ("pyproj", "geotransform", "coordtransform")):
        assert "待复核" in summary or "降级" in summary or "无可用后端" in summary


# --------------------------------------------------------------------------- #
# 米制投影
# --------------------------------------------------------------------------- #
def test_project_to_meters_returns_method() -> None:
    """投影结果带方法名，便于输出说明。"""
    projected = ct.project_to_meters(108.9455, 34.2611)
    assert projected.method in {"pyproj-utm", "local-equirectangular"}
    assert isinstance(projected.x, float) and isinstance(projected.y, float)


def test_project_to_meters_relative_to_origin() -> None:
    """给定原点时返回相对偏移，100 米量级的点应在 90—110 米内。"""
    origin = (108.9455, 34.2611)
    target = (108.9455, 34.2620)  # 纬度 +0.0009 度 ≈ 100 米

    projected = ct.project_to_meters(target[0], target[1], origin)
    distance = (projected.x**2 + projected.y**2) ** 0.5

    assert 90.0 <= distance <= 110.0


def test_distance_m_reasonable() -> None:
    """米制距离与 haversine 同量级（1 度纬度约 111 公里）。"""
    value = ct.distance_m((108.0, 34.0), (108.0, 35.0))
    assert 110000.0 <= value <= 112500.0


# --------------------------------------------------------------------------- #
# 几何工具
# --------------------------------------------------------------------------- #
def test_haversine_one_degree_latitude() -> None:
    """1 度纬度约 111.2 公里。"""
    assert gu.haversine_m(0.0, 0.0, 0.0, 1.0) == pytest.approx(111195.0, abs=100.0)


def test_haversine_zero_distance() -> None:
    """同一点距离为 0。"""
    assert gu.haversine_m(108.9455, 34.2611, 108.9455, 34.2611) == pytest.approx(0.0)


def test_local_xy_orientation() -> None:
    """局部投影：东向为 x 正、北向为 y 正。"""
    x, y = gu.local_xy_m((108.9465, 34.2621), (108.9455, 34.2611))
    assert x > 0 and y > 0
    assert 90.0 <= x <= 95.0  # 0.001 度经度在该纬度约 92 米
    assert 110.0 <= y <= 112.5  # 0.001 度纬度约 111 米


def test_point_segment_distance_perpendicular() -> None:
    """点到线段的垂距约为 0.0004 度纬度 ≈ 44 米。"""
    value = gu.point_segment_distance_m(
        (108.9455, 34.2615), (108.9445, 34.2611), (108.9465, 34.2611)
    )
    assert 40.0 <= value <= 50.0


def test_point_segment_distance_endpoint_clamp() -> None:
    """投影落在端点外时按端点距离计算。"""
    value = gu.point_segment_distance_m(
        (108.9480, 34.2611), (108.9445, 34.2611), (108.9465, 34.2611)
    )
    expected = gu.haversine_m(108.9480, 34.2611, 108.9465, 34.2611)
    assert value == pytest.approx(expected, rel=0.02)


def test_point_polyline_distance_uses_nearest_segment() -> None:
    """折线距离取各段最小值。"""
    polyline = [(108.9445, 34.2611), (108.9465, 34.2611), (108.9465, 34.2631)]
    value = gu.point_polyline_distance_m((108.9465, 34.2620), polyline)
    assert value < 1.0


def test_point_polyline_distance_empty_raises() -> None:
    """空折线给出中文错误与下一步。"""
    with pytest.raises(ValueError) as excinfo:
        gu.point_polyline_distance_m((108.9, 34.2), [])
    assert "下一步" in str(excinfo.value)


def test_bbox_helpers() -> None:
    """bbox 包含、扩展、中心、跨度、合并。"""
    bbox = (108.928, 34.247, 108.963, 34.276)
    assert gu.bbox_contains(bbox, (108.9455, 34.2611))
    assert not gu.bbox_contains(bbox, (109.5, 34.2611))
    assert gu.bbox_contains(bbox, (109.5, 34.2611), pad=1.0)

    expanded = gu.bbox_expand_m(bbox, 200.0)
    lon_span, lat_span = gu.bbox_span_deg(expanded)
    assert lon_span > gu.bbox_span_deg(bbox)[0]
    assert lat_span > gu.bbox_span_deg(bbox)[1]

    center = gu.bbox_center(bbox)
    assert center[0] == pytest.approx((108.928 + 108.963) / 2)
    assert gu.bbox_union([bbox, (108.9, 34.2, 108.93, 34.25)]) == (
        108.9,
        34.2,
        108.963,
        34.276,
    )
    assert gu.bbox_union([]) is None


def test_polyline_length_and_interpolate() -> None:
    """折线长度与线性插值。"""
    assert gu.polyline_length_m([(108.0, 34.0)]) == 0.0
    length = gu.polyline_length_m([(108.9455, 34.2611), (108.9465, 34.2611)])
    assert 90.0 <= length <= 95.0
    assert gu.interpolate((0.0, 0.0), (10.0, 10.0), 0.5) == (5.0, 5.0)
    assert gu.interpolate((0.0, 0.0), (10.0, 10.0), 2.0) == (10.0, 10.0)


# --------------------------------------------------------------------------- #
# coordTransform 可选转换后端
# --------------------------------------------------------------------------- #
def test_coordtransform_backend_is_discovered(monkeypatch: pytest.MonkeyPatch) -> None:
    """导入名为 coordTransform 的纯 Python 库应被识别为可用后端。"""
    import types

    fake_module = types.ModuleType("coordTransform")
    fake_module.gcj02_to_wgs84 = lambda lon, lat: [lon - 0.005, lat - 0.002]
    fake_module.bd09_to_wgs84 = lambda lon, lat: [lon - 0.01, lat - 0.004]

    real_import = ct.importlib.import_module

    def fake_import(name: str):
        if name == "coordTransform":
            return fake_module
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(ct.importlib, "import_module", fake_import)
    ct.reset_backend_cache()
    try:
        result = ct.to_wgs84(108.9455, 34.2611, "gcj02")
        assert result.status == ct.STATUS_OK
        assert result.method == "coordTransform"
        assert result.lon == pytest.approx(108.9405)
        assert result.lat == pytest.approx(34.2591)
        assert ct.active_converter_backends().get("gcj02") == "coordTransform"
    finally:
        monkeypatch.setattr(ct.importlib, "import_module", real_import)
        ct.reset_backend_cache()


def test_missing_backend_reason_suggests_coordtransform() -> None:
    """缺少后端时，提供通用安装与数据复核指引。"""
    result = ct.to_wgs84(108.9455, 34.2611, "gcj02", converters={})

    assert "coordtransform" in result.reason
    assert "安装" in result.reason



@pytest.mark.parametrize("lon,lat", [(float("nan"), 34), (float("inf"), 34), (181, 34), (108, 91)])
def test_invalid_coordinates_are_pending(lon, lat) -> None:
    assert ct.to_wgs84(lon, lat, "wgs84", converters={}).needs_review


def test_invalid_converter_output_is_pending() -> None:
    result = ct.to_wgs84(108, 34, "gcj02", converters={"gcj02": lambda *_: (float("inf"), 34)})
    assert result.needs_review


def test_projection_pair_uses_one_crs(monkeypatch) -> None:
    import sys
    import types
    used = []
    class FakeTransformer:
        @staticmethod
        def from_crs(source, target, **kwargs):
            used.append(target)
            zone = int(target.split(":")[1])
            return types.SimpleNamespace(transform=lambda lon, lat: (lon * 1000 - zone * 1_000_000, lat * 1000))
    monkeypatch.setitem(sys.modules, "pyproj", types.SimpleNamespace(Transformer=FakeTransformer))
    ct.reset_backend_cache()
    assert ct.distance_m((5.999, 34), (6.001, 34)) == pytest.approx(2.0, abs=.01)
    assert len(used) == 1
    relative = ct.project_to_meters(6.001, 34, origin=(5.999, 34))
    assert relative.x == pytest.approx(2.0, abs=.01)
