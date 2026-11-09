"""``src/data_loader.py`` 测试：编码兜底、坏数据跳过、坐标待复核、空数据。"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, List, Sequence

import pytest

from src.config import AppConfig, load_config
from src.data_loader import (
    FILE_CLINIC,
    FILE_GRAPH_EDGES,
    FILE_GRAPH_NODES,
    FILE_GREENGROCER,
    FILE_OBSTACLES,
    FILE_REST_POINTS,
    load_dataset,
    load_graph_edges,
    load_graph_nodes,
    load_obstacles,
    load_pois,
    load_rest_points,
    read_csv_rows,
)

POI_FIELDS = [
    "id",
    "name",
    "category",
    "lon",
    "lat",
    "coord_system",
    "address",
    "open_hours",
    "has_ramp",
    "source",
    "confidence",
    "updated_at",
    "notes",
]

OBSTACLE_FIELDS = [
    "id",
    "name",
    "type",
    "lon",
    "lat",
    "coord_system",
    "severity",
    "step_count",
    "slope_pct",
    "length_m",
    "source",
    "confidence",
    "notes",
]

REST_FIELDS = [
    "id",
    "name",
    "type",
    "lon",
    "lat",
    "coord_system",
    "seats",
    "source",
    "confidence",
    "notes",
]

NODE_FIELDS = ["node_id", "lon", "lat", "coord_system", "source"]
EDGE_FIELDS = [
    "u",
    "v",
    "length_m",
    "walk_type",
    "step_count",
    "slope_pct",
    "surface",
    "source",
]


@pytest.fixture()
def config() -> AppConfig:
    """真实配置，仅读取。"""
    return load_config()


def _write_csv(
    path: Path,
    rows: Sequence[Dict[str, str]],
    fieldnames: Sequence[str],
    encoding: str = "utf-8",
) -> Path:
    """按指定编码写 CSV。"""
    with path.open("w", encoding=encoding, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def _poi_row(**overrides: str) -> Dict[str, str]:
    """构造一条合法菜店记录。"""
    base = {
        "id": "shop_001",
        "name": "样例蔬菜店",
        "category": "greengrocer",
        "lon": "108.9450",
        "lat": "34.2600",
        "coord_system": "wgs84",
        "address": "样例街 1 号",
        "open_hours": "07:00-19:00",
        "has_ramp": "1",
        "source": "现场",
        "confidence": "high",
        "updated_at": "2026-01-01",
        "notes": "",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# 编码
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "gbk"])
def test_read_csv_supports_three_encodings(tmp_path: Path, encoding: str) -> None:
    """utf-8 / utf-8-sig / gbk 三种编码都能读取。"""
    path = _write_csv(
        tmp_path / "pois.csv", [_poi_row()], POI_FIELDS, encoding=encoding
    )

    rows, detected, error = read_csv_rows(path)

    assert error is None
    assert detected == encoding
    assert len(rows) == 1
    assert rows[0]["name"] == "样例蔬菜店"


def test_load_pois_from_gbk_file(tmp_path: Path, config: AppConfig) -> None:
    """GBK 文件的 POI 能正确加载。"""
    path = _write_csv(tmp_path / "pois.csv", [_poi_row()], POI_FIELDS, encoding="gbk")

    result = load_pois(path, "greengrocer", config.area)

    assert result.encoding == "gbk"
    assert len(result.items) == 1
    assert result.items[0].name == "样例蔬菜店"


def test_missing_file_reports_next_step(tmp_path: Path) -> None:
    """文件不存在时给出中文错误与下一步。"""
    rows, encoding, error = read_csv_rows(tmp_path / "nope.csv")

    assert rows == [] and encoding == ""
    assert error is not None
    assert "下一步" in error.reason


# --------------------------------------------------------------------------- #
# 字段校验
# --------------------------------------------------------------------------- #
def test_missing_required_field_is_skipped(tmp_path: Path, config: AppConfig) -> None:
    """缺 id 的记录被跳过并记录原因。"""
    path = _write_csv(
        tmp_path / "pois.csv",
        [_poi_row(id=""), _poi_row(id="shop_002")],
        POI_FIELDS,
    )

    result = load_pois(path, "greengrocer", config.area)

    assert len(result.items) == 1
    assert result.items[0].id == "shop_002"
    assert len(result.errors) == 1
    assert "id" in result.errors[0].reason


def test_missing_coordinate_is_skipped(tmp_path: Path, config: AppConfig) -> None:
    """坐标为空时跳过该条，不影响其它记录。"""
    path = _write_csv(
        tmp_path / "pois.csv",
        [
            _poi_row(id="shop_001", lon=""),
            _poi_row(id="shop_002", lat="北纬34度"),
            _poi_row(id="shop_003"),
        ],
        POI_FIELDS,
    )

    result = load_pois(path, "greengrocer", config.area)

    assert [item.id for item in result.items] == ["shop_003"]
    assert len(result.errors) == 2
    assert all("下一步" in issue.reason for issue in result.errors)


def test_duplicate_id_is_skipped(tmp_path: Path, config: AppConfig) -> None:
    """重复 id 只保留第一条。"""
    path = _write_csv(
        tmp_path / "pois.csv",
        [_poi_row(id="shop_001"), _poi_row(id="shop_001", name="重复店")],
        POI_FIELDS,
    )

    result = load_pois(path, "greengrocer", config.area)

    assert len(result.items) == 1
    assert result.items[0].name == "样例蔬菜店"
    assert any("重复" in issue.reason for issue in result.errors)


def test_has_ramp_parsing(tmp_path: Path, config: AppConfig) -> None:
    """has_ramp 支持 1/0、是/否、空值。"""
    path = _write_csv(
        tmp_path / "pois.csv",
        [
            _poi_row(id="a", has_ramp="1"),
            _poi_row(id="b", has_ramp="否"),
            _poi_row(id="c", has_ramp=""),
        ],
        POI_FIELDS,
    )

    result = load_pois(path, "greengrocer", config.area)
    values = {item.id: item.has_ramp for item in result.items}

    assert values == {"a": True, "b": False, "c": None}


def test_missing_source_gives_warning_not_skip(
    tmp_path: Path, config: AppConfig
) -> None:
    """缺来源标注只警告，记录仍加载（AGENTS.md 要求标注来源）。"""
    path = _write_csv(
        tmp_path / "pois.csv", [_poi_row(source="", confidence="")], POI_FIELDS
    )

    result = load_pois(path, "greengrocer", config.area)

    assert len(result.items) == 1
    assert result.errors == []
    assert any("source" in issue.reason for issue in result.warnings)


def test_invalid_source_value_warns(tmp_path: Path, config: AppConfig) -> None:
    """来源不在约定集合内时提示改正。"""
    path = _write_csv(
        tmp_path / "pois.csv", [_poi_row(source="网上抄的")], POI_FIELDS
    )

    result = load_pois(path, "greengrocer", config.area)

    assert len(result.items) == 1
    assert any("约定集合" in issue.reason for issue in result.warnings)


# --------------------------------------------------------------------------- #
# 坐标
# --------------------------------------------------------------------------- #
def test_gcj02_without_converter_goes_pending(
    tmp_path: Path, config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GCJ02 且无转换库时进待复核，不加载为可用 POI。"""
    from src.geo import coord_transform

    # 显式禁用后端，保证测试不依赖可选库的安装状态
    monkeypatch.setattr(coord_transform, "_discover_geotransform", lambda: {})
    coord_transform.reset_backend_cache()
    path = _write_csv(
        tmp_path / "pois.csv", [_poi_row(coord_system="gcj02")], POI_FIELDS
    )

    result = load_pois(path, "greengrocer", config.area)

    assert result.items == []
    assert len(result.pending_review) == 1
    assert result.pending_review[0]["coord_system"] == "gcj02"
    assert "下一步" in result.pending_review[0]["reason"]


def test_missing_coord_system_goes_pending(tmp_path: Path, config: AppConfig) -> None:
    """coord_system 缺失时进待复核（AGENTS.md 硬性要求）。"""
    path = _write_csv(
        tmp_path / "pois.csv", [_poi_row(coord_system="")], POI_FIELDS
    )

    result = load_pois(path, "greengrocer", config.area)

    assert result.items == []
    assert len(result.pending_review) == 1
    assert "coord_system" in result.pending_review[0]["reason"]


def test_out_of_area_coordinate_kept_with_warning(
    tmp_path: Path, config: AppConfig
) -> None:
    """坐标超出片区时保留记录但给警告（不丢真实 POI）。"""
    path = _write_csv(
        tmp_path / "pois.csv", [_poi_row(lon="109.5000", lat="34.2600")], POI_FIELDS
    )

    result = load_pois(path, "greengrocer", config.area)

    assert len(result.items) == 1
    assert any("超出片区" in issue.reason for issue in result.warnings)


# --------------------------------------------------------------------------- #
# 障碍点 / 休息点
# --------------------------------------------------------------------------- #
def _obstacle_row(**overrides: str) -> Dict[str, str]:
    """构造一条合法障碍点记录。"""
    base = {
        "id": "obs_001",
        "name": "样例台阶",
        "type": "stairs",
        "lon": "108.9452",
        "lat": "34.2602",
        "coord_system": "wgs84",
        "severity": "2",
        "step_count": "8",
        "slope_pct": "",
        "length_m": "6",
        "source": "现场",
        "confidence": "high",
        "notes": "",
    }
    base.update(overrides)
    return base


def test_obstacle_severity_defaults_to_medium(
    tmp_path: Path, config: AppConfig
) -> None:
    """severity 缺失按中度 2 处理并给警告。"""
    path = _write_csv(
        tmp_path / "obs.csv", [_obstacle_row(severity="")], OBSTACLE_FIELDS
    )

    result = load_obstacles(path, config.area)

    assert result.items[0].severity == 2
    assert any("severity" in issue.reason for issue in result.warnings)


def test_obstacle_severity_is_clamped(tmp_path: Path, config: AppConfig) -> None:
    """severity 超出 1—3 时被裁剪。"""
    path = _write_csv(
        tmp_path / "obs.csv",
        [_obstacle_row(id="o1", severity="9"), _obstacle_row(id="o2", severity="0")],
        OBSTACLE_FIELDS,
    )

    result = load_obstacles(path, config.area)
    values = {item.id: item.severity for item in result.items}

    assert values == {"o1": 3, "o2": 1}


def test_obstacle_step_count_and_slope_parsed(
    tmp_path: Path, config: AppConfig
) -> None:
    """台阶级数与坡度百分比被解析为数字。"""
    path = _write_csv(
        tmp_path / "obs.csv",
        [
            _obstacle_row(id="o1", type="stairs", step_count="12"),
            _obstacle_row(id="o2", type="slope", step_count="", slope_pct="9.5"),
        ],
        OBSTACLE_FIELDS,
    )

    result = load_obstacles(path, config.area)
    by_id = {item.id: item for item in result.items}

    assert by_id["o1"].step_count == 12
    assert by_id["o1"].slope_pct is None
    assert by_id["o2"].slope_pct == pytest.approx(9.5)
    assert by_id["o2"].step_count is None


def test_rest_point_loading(tmp_path: Path, config: AppConfig) -> None:
    """休息点正常加载，seats 可空。"""
    rows = [
        {
            "id": "rest_001",
            "name": "样例长椅",
            "type": "bench",
            "lon": "108.9451",
            "lat": "34.2601",
            "coord_system": "wgs84",
            "seats": "3",
            "source": "现场",
            "confidence": "high",
            "notes": "",
        },
        {
            "id": "rest_002",
            "name": "样例树荫",
            "type": "shade",
            "lon": "108.9453",
            "lat": "34.2603",
            "coord_system": "wgs84",
            "seats": "",
            "source": "街景",
            "confidence": "medium",
            "notes": "",
        },
    ]
    path = _write_csv(tmp_path / "rest.csv", rows, REST_FIELDS)

    result = load_rest_points(path, config.area)
    by_id = {item.id: item for item in result.items}

    assert len(result.items) == 2
    assert by_id["rest_001"].seats == 3
    assert by_id["rest_002"].seats is None


# --------------------------------------------------------------------------- #
# 路网
# --------------------------------------------------------------------------- #
def test_graph_nodes_missing_coord_system_goes_pending(
    tmp_path: Path, config: AppConfig
) -> None:
    """缺少坐标系的路网节点不得伪装成 WGS84。"""
    path = _write_csv(
        tmp_path / "nodes.csv",
        [{"node_id": "n1", "lon": "108.945", "lat": "34.261", "source": "样例合成"}],
        ("node_id", "lon", "lat", "source"),
    )
    result = load_graph_nodes(path, config.area)
    assert result.items == []
    assert len(result.pending_review) == 1


def test_graph_edges_validation(tmp_path: Path) -> None:
    """边：缺端点/自环/长度非法被跳过，walk_type 缺失按 0 并警告。"""
    rows = [
        {"u": "n1", "v": "n2", "length_m": "100", "walk_type": "0"},
        {"u": "n1", "v": "", "length_m": "100", "walk_type": "0"},
        {"u": "n3", "v": "n3", "length_m": "100", "walk_type": "0"},
        {"u": "n4", "v": "n5", "length_m": "0", "walk_type": "0"},
        {"u": "n6", "v": "n7", "length_m": "50", "walk_type": ""},
    ]
    path = _write_csv(tmp_path / "edges.csv", rows, EDGE_FIELDS)

    result = load_graph_edges(path)

    assert len(result.items) == 2
    assert result.items[0].length_m == 100
    assert result.items[1].walk_type == 0
    assert len(result.errors) == 3
    assert any("walk_type" in issue.reason for issue in result.warnings)


def test_graph_edges_preserve_walk_type_numbers(tmp_path: Path) -> None:
    """路网边保留高德类型号 21/22/3，供评分直接复用。"""
    rows = [
        {"u": "n1", "v": "n2", "length_m": "20", "walk_type": "21", "step_count": "9"},
        {"u": "n2", "v": "n3", "length_m": "30", "walk_type": "22", "slope_pct": "10"},
        {"u": "n3", "v": "n4", "length_m": "15", "walk_type": "3"},
    ]
    path = _write_csv(tmp_path / "edges.csv", rows, EDGE_FIELDS)

    result = load_graph_edges(path)
    kinds = [edge.walk_type for edge in result.items]

    assert kinds == [21, 22, 3]
    assert result.items[0].step_count == 9
    assert result.items[1].slope_pct == pytest.approx(10.0)


# --------------------------------------------------------------------------- #
# 整体加载
# --------------------------------------------------------------------------- #
def _build_sample_dir(root: Path, encoding: str = "utf-8") -> Path:
    """构造一个最小可用数据目录（5 菜店 / 3 医院 / 6 障碍 / 4 休息 / 路网）。"""
    _write_csv(
        root / FILE_GREENGROCER,
        [_poi_row(id=f"shop_{i:03d}", lon=str(108.945 + i * 0.001)) for i in range(5)],
        POI_FIELDS,
        encoding,
    )
    _write_csv(
        root / FILE_CLINIC,
        [
            _poi_row(
                id=f"clinic_{i:03d}",
                category="clinic",
                name=f"样例卫生服务站{i}",
                lon=str(108.946 + i * 0.001),
            )
            for i in range(3)
        ],
        POI_FIELDS,
        encoding,
    )
    _write_csv(
        root / FILE_OBSTACLES,
        [_obstacle_row(id=f"obs_{i:03d}", lon=str(108.945 + i * 0.0005)) for i in range(6)],
        OBSTACLE_FIELDS,
        encoding,
    )
    _write_csv(
        root / FILE_REST_POINTS,
        [
            {
                "id": f"rest_{i:03d}",
                "name": f"样例休息点{i}",
                "type": "bench",
                "lon": str(108.945 + i * 0.0005),
                "lat": "34.2601",
                "coord_system": "wgs84",
                "seats": "2",
                "source": "现场",
                "confidence": "high",
                "notes": "",
            }
            for i in range(4)
        ],
        REST_FIELDS,
        encoding,
    )
    _write_csv(
        root / FILE_GRAPH_NODES,
        [
            {"node_id": "n1", "lon": "108.9450", "lat": "34.2600", "coord_system": "wgs84", "source": "现场"},
            {"node_id": "n2", "lon": "108.9460", "lat": "34.2600", "coord_system": "wgs84", "source": "现场"},
        ],
        NODE_FIELDS,
        encoding,
    )
    _write_csv(
        root / FILE_GRAPH_EDGES,
        [{"u": "n1", "v": "n2", "length_m": "92", "walk_type": "0"}],
        EDGE_FIELDS,
        encoding,
    )
    return root


def test_load_dataset_counts(tmp_path: Path, config: AppConfig) -> None:
    """整体加载后各类数量正确，摘要可读。"""
    data_dir = _build_sample_dir(tmp_path)

    dataset = load_dataset(config, data_dir)

    assert len(dataset.pois_by_category("greengrocer")) == 5
    assert len(dataset.pois_by_category("clinic")) == 3
    assert len(dataset.obstacles) == 6
    assert len(dataset.rest_points) == 4
    assert len(dataset.nodes) == 2
    assert len(dataset.edges) == 1
    assert dataset.is_empty is False
    assert "菜店 5" in dataset.summary()


def test_load_dataset_with_gbk_files(tmp_path: Path, config: AppConfig) -> None:
    """整目录 GBK 编码也能加载。"""
    data_dir = _build_sample_dir(tmp_path, encoding="gbk")

    dataset = load_dataset(config, data_dir)

    assert len(dataset.pois) == 8
    assert dataset.encodings[FILE_GREENGROCER] == "gbk"


def test_load_dataset_empty_dir_is_graceful(tmp_path: Path, config: AppConfig) -> None:
    """空目录不抛异常，返回空数据集并给出下一步提示。"""
    dataset = load_dataset(config, tmp_path)

    assert dataset.is_empty is True
    assert dataset.pois == []
    assert any("下一步" in issue.reason for issue in dataset.issues)


def test_load_dataset_partial_files(tmp_path: Path, config: AppConfig) -> None:
    """只放部分文件时，其它文件缺失不影响已存在文件加载。"""
    _write_csv(tmp_path / FILE_GREENGROCER, [_poi_row()], POI_FIELDS)

    dataset = load_dataset(config, tmp_path)

    assert len(dataset.pois) == 1
    assert len(dataset.nodes) == 0
    assert any("不存在" in issue.reason for issue in dataset.issues)


def test_extra_csv_column_skips_only_bad_row(tmp_path: Path, config: AppConfig) -> None:
    path = tmp_path / "pois.csv"
    path.write_text(
        "id,name,lon,lat,coord_system\n"
        "bad,坏行,108.94,34.26,wgs84,多余列\n"
        "good,有效店,108.94,34.26,wgs84\n", encoding="utf-8",
    )
    result = load_pois(path, "greengrocer", config.area)
    assert [poi.id for poi in result.items] == ["good"]
    assert any("字段多于表头" in issue.reason for issue in result.issues)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_edge_numbers_skip_bad_row(tmp_path: Path, value: str) -> None:
    path = tmp_path / "edges.csv"
    path.write_text(
        "u,v,length_m,walk_type,step_count\n"
        f"a,b,20,21,{value}\n"
        "b,c,20,0,\n", encoding="utf-8",
    )
    result = load_graph_edges(path)
    assert [(edge.u, edge.v) for edge in result.items] == [("b", "c")]
    assert result.errors


def test_graph_node_failed_conversion_stays_pending(tmp_path: Path, monkeypatch) -> None:
    from src.geo import coord_transform
    monkeypatch.setattr(coord_transform, "_discover_geotransform", lambda: {})
    path = tmp_path / "nodes.csv"
    path.write_text(
        "node_id,lon,lat,coord_system\nn1,108.945,34.261,gcj02\n",
        encoding="utf-8",
    )
    result = load_graph_nodes(path)
    assert not result.items
    assert len(result.pending_review) == 1
