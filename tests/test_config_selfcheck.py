"""配置和离线自检回归，全部使用临时数据或内存替身。"""
from pathlib import Path
import shutil
import dataclasses

import pytest
from src import config, selfcheck


@pytest.mark.parametrize("raw", [
    {"runtime": [1]},
    {"area": {"margin_deg": "bad"}},
    {"runtime": {"http_timeout_s": float("nan")}},
    {"scoring": {"stairs_per_step": -1}},
    {"area": {"bbox": [200, 0, 201, 1]}},
])
def test_invalid_config_fields_raise_config_error(monkeypatch, raw) -> None:
    monkeypatch.setattr(config, "_read_json", lambda path: raw)
    with pytest.raises(config.ConfigError, match="下一步"):
        config.load_config(env={})


def test_zero_retries_and_injected_paths_are_preserved(monkeypatch) -> None:
    monkeypatch.setenv("DATA_DIR", "wrong")
    result = config.load_config(env={"AMAP_MAX_RETRIES": "0", "DATA_DIR": "data/osm_beilin"})
    assert result.runtime.max_retries == 0
    assert result.runtime.data_dir == config.PROJECT_ROOT / "data/osm_beilin"


@pytest.mark.parametrize("kind", ["missing", "empty", "bad_reference"])
def test_data_selfcheck_rejects_unusable_graph(tmp_path: Path, monkeypatch, kind: str) -> None:
    original = config.load_config(env={})
    for name in ("pois_greengrocer.csv", "pois_clinic.csv", "obstacles.csv", "rest_points.csv"):
        shutil.copyfile(original.runtime.data_dir / name, tmp_path / name)
    if kind != "missing":
        (tmp_path / "graph_nodes.csv").write_text("node_id,lon,lat,coord_system\n" + (
            "a,108.94,34.26,wgs84\n" if kind == "bad_reference" else ""))
        (tmp_path / "graph_edges.csv").write_text("u,v,length_m,walk_type\n" + (
            "a,missing,10,0\n" if kind == "bad_reference" else ""))
    selected = dataclasses.replace(original, runtime=dataclasses.replace(original.runtime, data_dir=tmp_path))
    monkeypatch.setattr(selfcheck, "load_config", lambda: selected)
    monkeypatch.setattr(selfcheck, "check_dependencies", lambda: ("ok", [], []))
    monkeypatch.setattr(selfcheck, "check_key", lambda config: ("ok", []))
    assert selfcheck.run_selfcheck(with_data=True) == 1
