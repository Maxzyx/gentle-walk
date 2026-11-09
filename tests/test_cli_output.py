"""``src/cli.py`` 输出测试：离线模式、Key 命令、参数错误、Streamlit 缺失降级。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import cli, keys


@pytest.fixture()
def run_cli(capsys: pytest.CaptureFixture[str]):
    """执行 CLI 并返回（退出码, 输出文本）。"""

    def _run(argv):
        code = cli.main(argv)
        captured = capsys.readouterr()
        return code, captured.out + captured.err

    return _run


# --------------------------------------------------------------------------- #
# 主链路
# --------------------------------------------------------------------------- #
def test_default_run_outputs_full_text_route(run_cli) -> None:
    """默认参数应输出完整文字路线、风险提示与数据来源声明。"""
    code, out = run_cli([])

    assert code == 0
    assert "离线演示" in out
    assert "推荐路线" in out
    assert "路线步骤" in out
    assert "风险提示" in out
    assert "数据来源声明" in out
    assert "样例合成" in out
    assert "AMAP_KEY" in out  # 说明无需 Key


def test_category_clinic(run_cli) -> None:
    """--category clinic 走社区医院数据。"""
    code, out = run_cli(["--category", "clinic"])

    assert code == 0
    assert "社区医院" in out
    assert "已在 3 个社区医院中比较" in out


def test_category_chinese_alias(run_cli) -> None:
    """中文类目别名可用。"""
    code, out = run_cli(["--category", "菜店"])

    assert code == 0
    assert "社区菜店" in out


def test_destination_by_id(run_cli) -> None:
    """--destination 指定编号后只规划该目的地。"""
    code, out = run_cli(["--destination", "shop_002"])

    assert code == 0
    assert "钟楼东生鲜超市" in out


def test_from_coordinate(run_cli) -> None:
    """--from 指定起点后输出该起点坐标。"""
    code, out = run_cli(["--from", "108.9440,34.2610"])

    assert code == 0
    assert "108.9440,34.2610" in out


def test_list_destinations(run_cli) -> None:
    """--list 列出该类目全部目的地与编号。"""
    code, out = run_cli(["--list", "--category", "clinic"])

    assert code == 0
    assert "共 3 个" in out
    assert "clinic_001" in out
    assert "clinic_003" in out


def test_no_route_from_far_origin(run_cli) -> None:
    """起点离路网过远时不崩溃，给出可操作提示。"""
    code, out = run_cli(["--from", "109.5000,34.9000"])

    assert code == 0
    assert "未能生成路线" in out or "未找到" in out
    assert "下一步" in out or "换一个" in out


# --------------------------------------------------------------------------- #
# 参数错误
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["bad", "108.9", "东经108.9,34.2", "108.9,34.2,5"])
def test_invalid_origin_exit_code(run_cli, bad: str) -> None:
    """起点格式错误返回退出码 2 并说明正确写法。"""
    code, out = run_cli(["--from", bad])

    assert code == 2
    assert "起点参数错误" in out
    assert "下一步" in out


def test_invalid_category_exit_code(run_cli) -> None:
    """不支持的目的地类型返回退出码 2。"""
    code, out = run_cli(["--category", "museum"])

    assert code == 2
    assert "不支持的目的地类型" in out
    assert "greengrocer" in out


def test_invalid_origin_out_of_range(run_cli) -> None:
    """经纬度超范围时提示顺序问题。"""
    code, out = run_cli(["--from", "34.2,108.9"])

    assert code == 2
    assert "经度" in out


# --------------------------------------------------------------------------- #
# Key 命令
# --------------------------------------------------------------------------- #
def test_key_status_masks_value(run_cli, monkeypatch: pytest.MonkeyPatch) -> None:
    """--key-status 只显示打码值，绝不打印完整 Key。"""
    monkeypatch.setenv("AMAP_KEY", "DUMMY_AMAP_KEY_NOT_REAL_0000cdef")

    code, out = run_cli(["--key-status"])

    assert code == 0
    assert "****cdef" in out
    assert "DUMMY_AMAP_KEY_NOT_REAL_0000cdef" not in out
    assert "--set-key" in out


def test_key_status_when_missing(run_cli, monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置时给出三种设置方式。"""
    monkeypatch.delenv("AMAP_KEY", raising=False)
    monkeypatch.setattr(keys, "_import_keyring", lambda: None)
    monkeypatch.setattr(keys, "read_dotenv_key", lambda path=None: None)

    code, out = run_cli(["--key-status"])

    assert code == 0
    assert "未配置" in out
    assert ".env" in out


def test_set_key_command_reports_result(
    run_cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--set-key 调用交互式设置并打印结果（不接触真实钥匙串）。"""
    monkeypatch.setattr(
        keys,
        "prompt_and_store_key",
        lambda config=None, **kwargs: (True, "已写入系统钥匙串（测试替身），值 ****cdef"),
    )

    code, out = run_cli(["--set-key"])

    assert code == 0
    assert "已写入系统钥匙串" in out
    assert "****cdef" in out


def test_set_key_failure_exit_code(run_cli, monkeypatch: pytest.MonkeyPatch) -> None:
    """设置失败时返回非 0 退出码并给出指引。"""
    monkeypatch.setattr(
        keys,
        "prompt_and_store_key",
        lambda config=None, **kwargs: (False, "当前系统没有可用的安全钥匙串。下一步：写入 .env"),
    )

    code, out = run_cli(["--set-key"])

    assert code == 1
    assert "下一步" in out


def test_clear_key_command(run_cli, monkeypatch: pytest.MonkeyPatch) -> None:
    """--clear-key 调用删除逻辑。"""
    monkeypatch.setattr(
        keys, "clear_stored_key", lambda config=None, **kwargs: (True, "已从系统钥匙串删除 AMAP_KEY。")
    )

    code, out = run_cli(["--clear-key"])

    assert code == 0
    assert "已从系统钥匙串删除" in out


# --------------------------------------------------------------------------- #
# Streamlit 缺失降级
# --------------------------------------------------------------------------- #
def test_web_mode_falls_back_when_streamlit_missing(
    run_cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未安装 Streamlit 时 --web 提示改用 CLI，而不是报错退出。"""
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: None)

    code, out = run_cli(["--web"])

    assert code == 0
    assert "未安装 Streamlit" in out
    assert "CLI 文字模式" in out


def test_web_mode_missing_ui_file(run_cli, monkeypatch: pytest.MonkeyPatch) -> None:
    """Streamlit 存在但页面文件缺失时同样降级。"""
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(cli.Path, "is_file", lambda self: False)

    code, out = run_cli(["--web"])

    assert code == 0
    assert "未找到页面文件" in out


# --------------------------------------------------------------------------- #
# 帮助与配置
# --------------------------------------------------------------------------- #
def test_help_epilog_mentions_set_key(capsys: pytest.CaptureFixture[str]) -> None:
    """帮助文本包含 --set-key 与离线示例。"""
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out

    assert "--set-key" in out
    assert "--key-status" in out
    assert "python -m src.cli" in out


def test_missing_config_file(run_cli, tmp_path: Path) -> None:
    """配置文件不存在时返回 2 并提示下一步。"""
    code, out = run_cli(["--config", str(tmp_path / "nope.json")])

    assert code == 2
    assert "配置错误" in out
    assert "下一步" in out



def test_web_child_receives_selected_config(monkeypatch) -> None:
    import dataclasses
    import types
    from src.config import load_config
    config = load_config(Path("config/beilin.json"), env={})
    config = dataclasses.replace(config, runtime=dataclasses.replace(config.runtime, offline_demo=False))
    calls = []
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr("subprocess.call", lambda args, **kwargs: calls.append((args, kwargs)) or 0)
    cli._handle_web(types.SimpleNamespace(web=True), config)
    env = calls[0][1]["env"]
    assert env["APP_CONFIG"] == str(config.source_path.resolve())
    assert env["OFFLINE_DEMO"] == "false"
    assert env["DATA_DIR"] == str(config.runtime.data_dir.resolve())


def test_sidebar_returns_state_for_same_planning_snapshot(monkeypatch) -> None:
    import types
    from src import ui_streamlit as ui, service
    from src.config import load_config
    from src.models import Poi
    from src.data_loader import Dataset
    state = service.prepare_state(config=load_config(env={}), dataset=Dataset(
        pois=[Poi("p", "店", "greengrocer", 108.945, 34.26)]
    ))
    class Sidebar:
        def __getattr__(self, key):
            return lambda *args, **kwargs: None
        def radio(self, *args, **kwargs): return kwargs["options"][0]
        def columns(self, n): return [self] * n
        def number_input(self, *args, **kwargs): return kwargs["value"]
        def selectbox(self, *args, **kwargs): return kwargs["options"][1]
        def button(self, *args, **kwargs): return True
    calls = []
    monkeypatch.setattr(ui, "prepare_state", lambda **kwargs: calls.append(1) or state)
    monkeypatch.setattr(keys, "resolve_key", lambda **kwargs: keys.KeyResolution(None, "none"))
    request, selected_state = ui._render_sidebar(types.SimpleNamespace(sidebar=Sidebar()), state.config)
    assert selected_state is state and request.destination_id == "p"
    assert len(calls) == 1
