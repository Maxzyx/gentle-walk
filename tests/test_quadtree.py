"""四叉树 POI 搜索与高德客户端测试（全部离线、注入替身，不触网）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from src.adapters.amap_client import (
    AmapClient,
    AmapResponse,
    AmapUnavailable,
    cache_key,
)
from src.adapters.amap_poi import (
    MAX_REQUESTS,
    PAGE_SIZE,
    QuadtreeStats,
    _should_split,
    parse_poi,
    search_category,
    search_pois,
)
from src.config import AppConfig, QuadtreeConfig, load_config

BBOX = (108.928, 34.247, 108.963, 34.276)


@pytest.fixture()
def config() -> AppConfig:
    """真实配置（只读取）。"""
    return load_config()


@pytest.fixture()
def quadtree(config: AppConfig) -> QuadtreeConfig:
    """真实四叉树配置（阈值 180 / 0.005 度 / 深度 6）。"""
    return config.quadtree


class FakeClient:
    """替身客户端：按 bbox 大小返回不同密度的 POI，并记录调用。"""

    def __init__(self, dense: int = 200, sparse: int = 50) -> None:
        self.dense = dense
        self.sparse = sparse
        self.calls: List[Dict[str, str]] = []

    def get(self, path: str, params: Dict[str, str]) -> AmapResponse:
        """返回伪造 POI 列表；bbox 越大返回越多，用于触发拆分。"""
        self.calls.append(dict(params))
        polygon = params.get("polygon", "")
        span = _polygon_span(polygon)
        # 整片区（经度跨度 0.035）返回 dense，四等分后的子区域返回 sparse
        count = self.dense if span >= 0.03 else self.sparse
        page = int(params.get("page", "1"))
        start = (page - 1) * PAGE_SIZE
        remaining = max(0, count - start)
        size = min(PAGE_SIZE, remaining)
        pois = [
            {
                "id": f"{span:.4f}_{page}_{index}",
                "name": f"店{span:.4f}_{page}_{index}",
                "location": f"{108.9 + index * 0.0001:.6f},{34.2 + index * 0.0001:.6f}",
            }
            for index in range(size)
        ]
        return AmapResponse(ok=True, data={"pois": pois, "count": str(count)}, status=200)


def _polygon_span(polygon: str) -> float:
    """从 polygon 参数解析经度跨度，用于决定返回密度。"""
    try:
        left, right = polygon.split("|")
        lon_left = float(left.split(",")[0])
        lon_right = float(right.split(",")[0])
        return abs(lon_right - lon_left)
    except (ValueError, IndexError):
        return 0.0


# --------------------------------------------------------------------------- #
# 终止条件
# --------------------------------------------------------------------------- #
def test_should_split_threshold(quadtree: QuadtreeConfig) -> None:
    """低于阈值不拆，达到阈值才拆。"""
    assert _should_split(179, BBOX, 0, quadtree) is False
    assert _should_split(180, BBOX, 0, quadtree) is True


def test_should_split_respects_min_span(quadtree: QuadtreeConfig) -> None:
    """网格跨度小于 0.005 度时停止拆分。"""
    tiny = (108.9450, 34.2610, 108.9451, 34.2611)  # 跨度 0.0001 度
    assert _should_split(500, tiny, 0, quadtree) is False


def test_should_split_respects_max_depth(quadtree: QuadtreeConfig) -> None:
    """达到深度上限后停止拆分。"""
    assert _should_split(500, BBOX, quadtree.max_depth, quadtree) is False


def test_search_does_not_split_when_sparse(quadtree: QuadtreeConfig) -> None:
    """稀疏区域只查一个叶子。"""
    client = FakeClient(dense=50, sparse=50)
    stats = QuadtreeStats()

    rows = search_pois(client, ["菜市场"], BBOX, quadtree, stats=stats)

    assert stats.splits == 0
    assert stats.leaves == 1
    assert len(rows) == 50


def test_search_splits_when_dense(quadtree: QuadtreeConfig) -> None:
    """密集区域触发四分：4 个子区域各查一次。"""
    client = FakeClient(dense=200, sparse=50)
    stats = QuadtreeStats()

    rows = search_pois(client, ["菜市场"], BBOX, quadtree, stats=stats)

    assert stats.splits == 1
    assert stats.leaves == 5  # 1 个父 + 4 个子
    assert stats.max_depth == 1
    assert len(rows) > 0
    # 至少有一次请求带上了子区域 polygon（经度跨度变小）
    assert any(_polygon_span(call["polygon"]) < 0.02 for call in client.calls)


def test_search_records_truncated_leaf(quadtree: QuadtreeConfig) -> None:
    """密集但已触达深度上限时，如实记录可能被截断的叶子。"""
    shallow = QuadtreeConfig(
        poi_threshold=quadtree.poi_threshold,
        min_span_deg=quadtree.min_span_deg,
        max_depth=0,
        page_size=quadtree.page_size,
        keywords=quadtree.keywords,
    )
    client = FakeClient(dense=200, sparse=50)
    stats = QuadtreeStats()

    search_pois(client, ["菜市场"], BBOX, shallow, stats=stats)

    assert stats.splits == 0
    assert stats.truncated_leaves == 1


def test_search_paginates_until_short_page(quadtree: QuadtreeConfig) -> None:
    """翻页直到不足一页为止，且不超过接口上限。"""
    client = FakeClient(dense=60, sparse=60)
    stats = QuadtreeStats()

    search_pois(client, ["菜市场"], BBOX, quadtree, stats=stats)

    pages = [call["page"] for call in client.calls]
    assert pages == ["1", "2", "3"]  # 60 条 = 25 + 25 + 10


def test_search_dedupes_same_name_and_location(quadtree: QuadtreeConfig) -> None:
    """同名同坐标结果去重。"""

    class DuplicateClient:
        """每次都返回同一条 POI 的替身。"""

        def get(self, path: str, params: Dict[str, str]) -> AmapResponse:
            return AmapResponse(
                ok=True,
                data={"pois": [{"id": "x", "name": "同一家店", "location": "108.9,34.2"}]},
                status=200,
            )

    rows = search_pois(DuplicateClient(), ["菜市场", "蔬菜店"], BBOX, quadtree)

    assert len(rows) == 1


def test_search_multiple_keywords_merged(quadtree: QuadtreeConfig) -> None:
    """多关键词结果合并，请求次数按关键词累加。"""
    client = FakeClient(dense=10, sparse=10)
    stats = QuadtreeStats()

    rows = search_pois(client, ["菜市场", "蔬菜店"], BBOX, quadtree, stats=stats)

    assert stats.requests == 2
    assert rows


# --------------------------------------------------------------------------- #
# POI 解析
# --------------------------------------------------------------------------- #
def test_parse_poi_converts_location() -> None:
    """location 字符串被拆成经纬度，坐标系统标记为 GCJ02。"""
    record = parse_poi(
        {"id": "B001", "name": "样例菜店", "location": "108.945000,34.261000", "address": "样例街"},
        "greengrocer",
    )

    assert record is not None
    assert record["id"] == "amap_B001"
    assert record["lon"] == pytest.approx(108.945)
    assert record["lat"] == pytest.approx(34.261)
    assert record["coord_system"] == "gcj02"
    assert record["source"] == "高德样例"
    assert record["category"] == "greengrocer"


def test_parse_poi_handles_missing_location() -> None:
    """缺 location 或格式非法时返回 None，不抛异常。"""
    assert parse_poi({"name": "无坐标"}, "clinic") is None
    assert parse_poi({"name": "坏坐标", "location": "abc"}, "clinic") is None
    assert parse_poi({"location": "108.9,34.2"}, "clinic") is None  # 缺 name


def test_parse_poi_joins_list_address() -> None:
    """address 为数组时拼成字符串。"""
    record = parse_poi(
        {"name": "店", "location": "108.9,34.2", "address": ["样例", "街", "1号"]}, "clinic"
    )
    assert record is not None
    assert record["address"] == "样例街1号"


def test_search_category_uses_configured_keywords(
    quadtree: QuadtreeConfig, config: AppConfig
) -> None:
    """按类目取关键词并解析结果。"""
    client = FakeClient(dense=10, sparse=10)

    records, stats = search_category(client, "clinic", BBOX, quadtree)

    assert stats.requests == len(quadtree.keywords_for("clinic"))
    assert all(record["category"] == "clinic" for record in records)
    assert records


def test_search_category_unknown_category_raises(
    quadtree: QuadtreeConfig, config: AppConfig
) -> None:
    """未配置关键词的类目给出中文错误与下一步。"""
    client = FakeClient()

    with pytest.raises(AmapUnavailable) as excinfo:
        search_category(client, "museum", BBOX, quadtree)
    assert "下一步" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 客户端：Key、缓存、429 退避
# --------------------------------------------------------------------------- #
def _ok_body(pois: Optional[List[Dict[str, str]]] = None) -> bytes:
    """构造一个成功响应体。"""
    payload = {"status": "1", "count": str(len(pois or [])), "pois": pois or []}
    return json.dumps(payload).encode("utf-8")


def test_client_without_key_raises(config: AppConfig) -> None:
    """无 Key 时抛出 AmapUnavailable 并提示如何配置。"""
    client = AmapClient(config, key="")

    with pytest.raises(AmapUnavailable) as excinfo:
        client.get("/v3/place/text", {"keywords": "菜市场"})
    assert "--set-key" in str(excinfo.value)


def test_client_caches_identical_query(config: AppConfig, tmp_path: Path) -> None:
    """相同查询第二次走缓存，不再打到网络。"""
    calls: List[str] = []

    def opener(url: str, timeout: float) -> Tuple[int, Dict[str, str], bytes]:
        calls.append(url)
        return 200, {}, _ok_body([{"name": "店", "location": "108.9,34.2"}])

    client = AmapClient(
        config, key="DUMMY_AMAP_KEY_NOT_REAL_0000cdef", opener=opener, cache_dir=tmp_path
    )

    first = client.get("/v3/place/text", {"keywords": "菜市场", "city": "西安"})
    second = client.get("/v3/place/text", {"keywords": "菜市场", "city": "西安"})

    assert first.cached is False
    assert second.cached is True
    assert len(calls) == 1
    assert len(second.pois) == 1


def test_client_retries_on_429_then_succeeds(config: AppConfig, tmp_path: Path) -> None:
    """429 时按 Retry-After 等待后重试成功。"""
    responses = [
        (429, {"retry-after": "0"}, b""),
        (200, {}, _ok_body([{"name": "店", "location": "108.9,34.2"}])),
    ]
    sleeps: List[float] = []

    def opener(url: str, timeout: float):
        return responses.pop(0)

    client = AmapClient(
        config,
        key="DUMMY_AMAP_KEY_NOT_REAL_0000cdef",
        opener=opener,
        sleeper=sleeps.append,
        cache_dir=tmp_path,
    )

    response = client.get("/v3/place/text", {"keywords": "菜市场"})

    assert response.ok
    assert sleeps == [0.0]
    assert client.request_count == 2


def test_client_429_exhausted_raises(config: AppConfig, tmp_path: Path) -> None:
    """429 重试耗尽后抛出明确错误，供上层降级。"""

    def opener(url: str, timeout: float):
        return 429, {"retry-after": "0"}, b""

    client = AmapClient(
        config,
        key="DUMMY_AMAP_KEY_NOT_REAL_0000cdef",
        opener=opener,
        sleeper=lambda seconds: None,
        cache_dir=tmp_path,
    )

    with pytest.raises(AmapUnavailable) as excinfo:
        client.get("/v3/place/text", {"keywords": "菜市场"})
    assert "离线" in str(excinfo.value)


def test_client_business_error_raises(config: AppConfig, tmp_path: Path) -> None:
    """status != 1 视为业务错误。"""

    def opener(url: str, timeout: float):
        body = json.dumps({"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"})
        return 200, {}, body.encode("utf-8")

    client = AmapClient(
        config,
        key="DUMMY_AMAP_KEY_NOT_REAL_0000cdef",
        opener=opener,
        cache_dir=tmp_path,
    )

    with pytest.raises(AmapUnavailable) as excinfo:
        client.get("/v3/place/text", {"keywords": "菜市场"})
    assert "INVALID_USER_KEY" in str(excinfo.value)


def test_cache_key_excludes_api_key() -> None:
    """缓存键不包含 Key，避免 Key 泄漏到文件名。"""
    first = cache_key("/v3/place/text", {"keywords": "菜市场", "key": "aaa"})
    second = cache_key("/v3/place/text", {"keywords": "菜市场", "key": "bbb"})

    assert first == second
    assert "aaa" not in first and "bbb" not in first


def test_client_masked_key(config: AppConfig, tmp_path: Path) -> None:
    """对外只暴露打码后的 Key。"""
    client = AmapClient(
        config,
        key="DUMMY_AMAP_KEY_NOT_REAL_0000cdef",
        opener=lambda url, timeout: (200, {}, _ok_body()),
        cache_dir=tmp_path,
    )

    assert client.has_key is True
    assert client.masked_key == "****cdef"
    assert "DUMMY_AMAP_KEY_NOT_REAL_0000cdef" not in client.masked_key


def test_search_stops_at_request_budget(quadtree: QuadtreeConfig) -> None:
    """用固定密集响应模拟区域过滤失效，验证请求预算终止。"""
    deep = QuadtreeConfig(
        poi_threshold=quadtree.poi_threshold,
        min_span_deg=0.0001,  # 极小跨度，只有预算能拦住递归
        max_depth=6,
        page_size=quadtree.page_size,
        keywords=quadtree.keywords,
    )
    client = FakeClient(dense=200, sparse=200)
    stats = QuadtreeStats()

    search_pois(client, ["菜市场"], BBOX, deep, stats=stats)

    assert stats.requests <= MAX_REQUESTS
    assert stats.requests == len(client.calls)
    assert stats.budget_exhausted is True
    assert stats.truncated_leaves >= 1
    assert "预算" in stats.summary()


def test_client_retries_on_business_rate_limit(config: AppConfig, tmp_path: Path) -> None:
    """业务层并发限流（10021 CUQPS）应退避重试，而不是直接失败。"""
    responses = [
        (
            200,
            {},
            json.dumps(
                {
                    "status": "0",
                    "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT",
                    "infocode": "10021",
                }
            ).encode("utf-8"),
        ),
        (200, {}, _ok_body([{"name": "店", "location": "108.9,34.2"}])),
    ]
    sleeps: List[float] = []

    client = AmapClient(
        config,
        key="DUMMY_AMAP_KEY_NOT_REAL_0000cdef",
        opener=lambda url, timeout: responses.pop(0),
        sleeper=sleeps.append,
        cache_dir=tmp_path,
    )

    response = client.get("/v3/place/text", {"keywords": "菜市场"})

    assert response.ok
    assert sleeps == [1.0]
    assert client.request_count == 2



def test_zero_retries_performs_only_initial_request(config: AppConfig) -> None:
    from dataclasses import replace
    config = replace(config, runtime=replace(config.runtime, max_retries=0, cache_enabled=False))
    waits = []
    client = AmapClient(config, key="test-only", opener=lambda *_: (500, {}, b""), sleeper=waits.append)
    with pytest.raises(AmapUnavailable):
        client.get("/v3/place/text", {})
    assert client.request_count == 1
    assert waits == []


def test_budget_keeps_results_already_obtained_from_parent(quadtree: QuadtreeConfig) -> None:
    class MultiKeywordClient:
        def get(self, path, params):
            start = (int(params["page"]) - 1) * 25
            word = params["keywords"]
            return AmapResponse(ok=True, data={
                "pois": [{"id": word + str(i), "name": word + str(i), "location": "108.945,34.26"}
                         for i in range(start, start + 25)],
                "count": "300",
            })
    stats = QuadtreeStats()
    words = ["k1", "k2", "k3", "k4", "k5"]
    rows = search_pois(MultiKeywordClient(), words, BBOX, quadtree, stats=stats)
    assert stats.budget_exhausted
    assert len(rows) == 1000
    assert {row["name"][:2] for row in rows} == set(words)
