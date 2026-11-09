#!/usr/bin/env python3
"""交互式写入 AMAP_KEY 到项目根目录的 .env。

用法：
    python3 scripts/set_amap_key.py            # 交互输入（不回显，推荐）
    python3 scripts/set_amap_key.py --stdin    # 从标准输入读一行（适合管道/自动化）
    python3 scripts/set_amap_key.py --check    # 只检查当前是否已有 Key，不写入

为什么单独做成脚本：
- 项目约定「Key 只从环境变量 ``AMAP_KEY`` 读取，不硬编码、不提交真实 Key」（见 AGENTS.md）。
  该脚本只把 Key 写进项目根目录的 ``.env``（已在 ``.gitignore`` 中），不写进任何源码。
- 走 ``src.keys.write_dotenv_key``：保留 ``.env`` 里其它变量，并把文件权限收紧到 ``600``。
- 输入不回显；写入后只打印打码值（前 4 位 + 后 4 位），避免 Key 出现在终端历史/录屏里。

写入成功后，重建碑林区 POI（约 11 次高德请求，结果进哈希缓存，重复运行零网络请求）：

    python3 -m src.adapters.amap_pois --district 碑林区 \
        --ring-json data/osm_beilin/boundary.json \
        --require-connected --config config/beilin.json --write
"""

from __future__ import annotations

import argparse
import getpass
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import keys  # noqa: E402

#: 高德 Key 形如 32 位十六进制字符串
KEY_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")

NEXT_STEPS = f"""
下一步（重建碑林区 POI，约 11 次高德请求）：
  cd "{ROOT}"
  python3 -m src.adapters.amap_pois --district 碑林区 \\
      --ring-json data/osm_beilin/boundary.json \\
      --require-connected --config config/beilin.json --write

只试算不写盘（默认行为，不生成 CSV）：
  python3 -m src.adapters.amap_pois --district 碑林区 --ring-json data/osm_beilin/boundary.json

查看 Key 状态（打码）：
  python3 -m src.cli --key-status
  python3 -m src.selfcheck | sed -n '/== Key/,+3p'
"""


def _prompt_key(use_stdin: bool) -> str:
    """读取用户输入的 Key；终端下不回显。"""
    if use_stdin:
        line = sys.stdin.readline()
        return line.strip()
    prompt = "请粘贴 AMAP_KEY（32 位十六进制，输入不回显）："
    if sys.stdin.isatty():
        try:
            return getpass.getpass(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消，未写入任何内容。", file=sys.stderr)
            raise SystemExit(1) from None
    # 非交互环境（IDE 控制台等）退化为可见输入，避免 getpass 直接失败
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消，未写入任何内容。", file=sys.stderr)
        raise SystemExit(1) from None


def _mask(normalized: str) -> str:
    """打码显示，仅暴露首尾各 4 位。"""
    if len(normalized) <= 8:
        return "*" * len(normalized)
    return f"{normalized[:4]}{'*' * (len(normalized) - 8)}{normalized[-4:]}"


def check_only() -> int:
    """只检查现有 Key（.env → 环境变量 → 钥匙串），不写入。"""
    resolved = keys.resolve_key()
    if resolved.key:
        where = resolved.source or "未知来源"
        print(f"已配置：{_mask(resolved.key)}（来源：{where}，长度 {len(resolved.key)}）")
        return 0
    print("未配置 AMAP_KEY。下一步：python3 scripts/set_amap_key.py")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 scripts/set_amap_key.py",
        description="交互式写入 AMAP_KEY 到项目根目录的 .env（不改源码、不回显）",
    )
    parser.add_argument("--stdin", action="store_true", help="从标准输入读一行 Key（适合管道）")
    parser.add_argument("--check", action="store_true", help="只检查 Key 是否已配置，不写入")
    parser.add_argument("--force", action="store_true", help="已有 Key 时也覆盖写入")
    args = parser.parse_args(argv)

    if args.check:
        return check_only()

    target = keys.env_file_path()
    print("=" * 66)
    print("高德 Key 写入工具")
    print("=" * 66)
    print(f"目标文件：{target}")
    print("说明：Key 只写入该 .env（已在 .gitignore 中），并收紧权限到 600；")
    print("      源码、日志、README 都不会出现 Key 明文。")
    print()

    existing = keys.read_dotenv_key(target)
    if existing and not args.force:
        print(f"检测到 .env 已有 Key：{_mask(existing)}")
        answer = ""
        if sys.stdin.isatty():
            answer = input("是否覆盖？输入 y 覆盖，直接回车保留现有：").strip().lower()
        if answer not in {"y", "yes"}:
            print("已保留现有 Key，未做任何修改。")
            print(NEXT_STEPS)
            return 0

    for attempt in range(1, 4):
        raw = _prompt_key(args.stdin)
        normalized = keys.normalize_key(raw)
        if not normalized:
            print(f"[{attempt}/3] 输入为空。下一步：重新运行本脚本并粘贴完整 Key。", file=sys.stderr)
            if args.stdin:
                return 1
            continue
        if not KEY_PATTERN.match(normalized):
            print(
                f"[{attempt}/3] 格式不对：应为 32 位十六进制，当前长度 {len(normalized)}。"
                "下一步：确认复制的是「Web 服务」类型的 Key，没有多余空格/引号。",
                file=sys.stderr,
            )
            if args.stdin:
                return 1
            continue

        ok, message = keys.write_dotenv_key(normalized, target)
        if not ok:
            print(f"写入失败：{message}", file=sys.stderr)
            return 1
        # 回读校验，确保真的落盘且能被 keys 解析
        written = keys.read_dotenv_key(target)
        if written != normalized:
            print(
                "写入后回读不一致。下一步：检查 .env 是否有重复的 AMAP_KEY 行或编码异常。",
                file=sys.stderr,
            )
            return 1
        print()
        print(f"写入成功：{_mask(normalized)}（{message}）")
        print(f"回读校验通过，文件权限：{oct(target.stat().st_mode & 0o777)}")
        print(NEXT_STEPS)
        return 0

    print("连续 3 次输入无效，已放弃写入。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())