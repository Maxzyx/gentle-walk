"""步行规划 WalkStep 解析测试：阶梯 21 / 斜坡 22 / 人行横道 3 / 扶梯 9 / 直梯 10。"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from src.adapters.amap_client import AmapResponse, AmapUnavailable
from src.adapters.amap_walking import (
    UNKNOWN_KIND,
    WALK_TYPE_MAP,
    WALKING_PATH,
    fetch_walking_steps,
    map_to_label,
    map_walk_type,
    parse_polyline,
    parse_walk_steps,
    summarize_steps,
)
from src.models import (
    WALK_TYPE_CROSSING,
    WALK_TYPE_ELEVATOR,
    WALK_TYPE_ESCALATOR,
    WALK_TYPE_NORMAL,
    WALK_TYPE_SLOPE,
    WALK_TYPE_STAIRS,
)

SAMPLE_PAYLOAD: Dict[str, Any] = {
    "status": "1",
    "route": {
        "paths": [
            {
                "distance": "420",
                "duration": "600",
                "steps": [
                    {
                        "instruction": "向西步行 120 米",
                        "distance": "120",
                        "walk_type": "0",
                        "polyline": "108.9450,34.2610;108.9440,34.2610",
                    },
                    {
                        "instruction": "经过台阶 8 级",
                        "distance": "30",
                        "walk_type": "21",
                        "step_count": "8",
                        "polyline": "108.9440,34.2610;108.9437,34.2610",
                    },
                    {
                        "instruction": "经斜坡通行",
                        "distance": "40",
                        "walk_type": "22",
                        "polyline": "108.9437,34.2610;108.9433,34.2610",
                    },
                    {
                        "instruction": "走人行横道过街",
                        "distance": "25",
                        "walk_type": "3",
                        "polyline": "108.9433,34.2610;108.9430,34.2610",
                    },
                    {
                        "instruction": "乘扶梯上到地面",
                        "distance": "15",
                        "walk_type": "9",
                        "polyline": "108.9430,34.2610;108.9428,34.2610",
                    },
                ],
            }
        ]
    },
}


class FakeClient:
    """替身客户端：返回固定响应并记录参数。"""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list = []

    def get(self, path: str, params: Dict[str, str]) -> AmapResponse:
        self.calls.append((path, dict(params)))
        return AmapResponse(ok=True, data=self.payload, status=200)


# --------------------------------------------------------------------------- #
# 类型映射（AGENTS.md 硬性数字）
# --------------------------------------------------------------------------- #
def test_walk_type_map_matches_agents_md() -> None:
    """阶梯=21、斜坡=22、人行横道=3、扶梯=9、直梯=10。"""
    assert WALK_TYPE_STAIRS == 21
    assert WALK_TYPE_SLOPE == 22
    assert WALK_TYPE_MAP[21] == "stairs"
    assert WALK_TYPE_MAP[22] == "slope"
    assert WALK_TYPE_MAP[3] == "crossing"
    assert WALK_TYPE_MAP[9] == "escalator"
    assert WALK_TYPE_MAP[10] == "elevator"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (21, "stairs"),
        (22, "slope"),
        (3, "crossing"),
        (9, "escalator"),
        (10, "elevator"),
        (0, "unknown"),
        (1, "unknown"),
        (99, "unknown"),
        ("21", "stairs"),
        (None, UNKNOWN_KIND),
    ],
)
def test_map_walk_type(code, expected: str) -> None:
    """数字/字符串/空值都能安全映射。"""
    assert map_walk_type(code) == expected


def test_map_to_label_chinese() -> None:
    """中文名与模型层一致。"""
    assert map_to_label(21) == "阶梯"
    assert map_to_label(22) == "斜坡"
    assert map_to_label(3) == "人行横道"
    assert map_to_label(10) == "直梯"
    assert "未识别" in map_to_label(77)


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def test_parse_polyline() -> None:
    """折线字符串被拆成坐标点元组。"""
    points = parse_polyline("108.9,34.2;108.91,34.21")

    assert points == ((108.9, 34.2), (108.91, 34.21))


@pytest.mark.parametrize("raw", ["", None, "abc", "108.9;34.2"])
def test_parse_polyline_invalid(raw) -> None:
    """非法折线返回空元组，不抛异常。"""
    assert parse_polyline(raw) == ()


def test_parse_polyline_keeps_valid_segments() -> None:
    """部分片段非法时保留合法坐标点。"""
    assert parse_polyline("1,2;bad") == ((1.0, 2.0),)


def test_parse_walk_steps_full() -> None:
    """完整响应解析出步骤、距离、台阶数与路型。"""
    steps = parse_walk_steps(SAMPLE_PAYLOAD)

    assert len(steps) == 5
    assert steps[0].walk_type == WALK_TYPE_NORMAL
    assert steps[1].walk_type == WALK_TYPE_STAIRS
    assert steps[1].step_count == 8
    assert steps[1].distance_m == pytest.approx(30.0)
    assert steps[2].walk_type == WALK_TYPE_SLOPE
    assert steps[3].walk_type == WALK_TYPE_CROSSING
    assert steps[4].walk_type == WALK_TYPE_ESCALATOR
    assert len(steps[0].polyline) == 2
    assert steps[0].source == "高德样例"


def test_parse_walk_steps_missing_walk_type_defaults_normal() -> None:
    """walk_type 缺失时按普通步道处理，不丢步骤。"""
    payload = {"route": {"paths": [{"steps": [{"instruction": "直行", "distance": "10"}]}]}}

    steps = parse_walk_steps(payload)

    assert len(steps) == 1
    assert steps[0].walk_type == WALK_TYPE_NORMAL
    assert steps[0].step_count is None


def test_parse_walk_steps_empty_payload() -> None:
    """没有路径时返回空列表，由调用方决定是否报错。"""
    assert parse_walk_steps({}) == []
    assert parse_walk_steps({"route": {"paths": []}}) == []
    assert parse_walk_steps({"route": {"paths": [{"steps": []}]}}) == []


def test_parse_walk_steps_skips_non_mapping() -> None:
    """steps 里混入非法元素时跳过该元素。"""
    payload = {"route": {"paths": [{"steps": ["bad", {"instruction": "直行", "distance": "5"}]}]}}

    steps = parse_walk_steps(payload)

    assert len(steps) == 1


def test_fetch_walking_steps_calls_v5_endpoint() -> None:
    """请求 v5 步行规划接口并带上 origin/destination。"""
    client = FakeClient(SAMPLE_PAYLOAD)

    steps = fetch_walking_steps(client, (108.9455, 34.2611), (108.9400, 34.2600))

    path, params = client.calls[0]
    assert path == WALKING_PATH
    assert params["origin"] == "108.945500,34.261100"
    assert params["destination"] == "108.940000,34.260000"
    assert len(steps) == 5


def test_fetch_walking_steps_without_path_raises() -> None:
    """无可用路径时抛出带下一步的错误。"""
    client = FakeClient({"status": "1", "route": {"paths": []}})

    with pytest.raises(AmapUnavailable) as excinfo:
        fetch_walking_steps(client, (108.9455, 34.2611), (108.9400, 34.2600))
    assert "下一步" in str(excinfo.value)


def test_summarize_steps_counts() -> None:
    """统计台阶段数、台阶总数、斜坡段数、过街次数与总距离。"""
    steps = parse_walk_steps(SAMPLE_PAYLOAD)

    summary = summarize_steps(steps)

    assert summary["steps"] == 5
    assert summary["stairs_segments"] == 1
    assert summary["stairs_total"] == 8
    assert summary["slope_segments"] == 1
    assert summary["crossings"] == 1
    assert summary["distance_m"] == pytest.approx(230.0)
