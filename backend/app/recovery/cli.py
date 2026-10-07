"""残局处理的命令入口 —— 与前端借还出入口彻底分开。

用法（backend/ 目录下）：

    python -m app.recovery.cli scan [--json]
    python -m app.recovery.cli reconcile [--strategy insert_active|reset_available] [--json]
    python -m app.recovery.cli inject <item_id>        # 演练/测试钩子：制造残局

退出码：
    0  成功（scan 正常完成；reconcile 已收敛）
    2  对账失败（reconcile 后仍有残局 / 拿不到写锁 / 补偿出错）
    1  用法或注入错误
"""

from __future__ import annotations

import argparse
import sys

from app.recovery import report
from app.recovery.inject import inject_half_insert
from app.recovery.reconcile import (
    EXIT_OK,
    EXIT_RECONCILE_FAILED,
    INSERT_ACTIVE,
    STRATEGIES,
    reconcile,
    scan,
)


def _print(text: str) -> None:
    print(text, flush=True)


def cmd_scan(args: argparse.Namespace) -> int:
    result = scan()
    _print(report.render_scan(result, as_json=args.json))
    return EXIT_OK


def cmd_reconcile(args: argparse.Namespace) -> int:
    result = reconcile(strategy=args.strategy)
    _print(report.render_reconcile(result, as_json=args.json))
    return EXIT_OK if result["converged"] else EXIT_RECONCILE_FAILED


def cmd_inject(args: argparse.Namespace) -> int:
    try:
        result = inject_half_insert(args.item_id)
    except (LookupError, ValueError) as exc:
        print(f"注入失败：{exc}", file=sys.stderr, flush=True)
        return 1
    if args.json:
        import json
        _print(json.dumps(result, ensure_ascii=False))
    else:
        _print(f"已制造半插入残局：item_id={result['item_id']}（status=on_loan，无 loans 行）")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.recovery.cli",
                                description="半插入残局的扫描与补偿（命令入口，不属于借还业务路由）")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("scan", help="只读扫描残局 item_id")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_scan)

    rp = sub.add_parser("reconcile", help="扫描并落地补偿")
    rp.add_argument("--strategy", choices=STRATEGIES, default=INSERT_ACTIVE,
                    help="insert_active=补一行 active（默认）；reset_available=状态改回 available")
    rp.add_argument("--json", action="store_true")
    rp.set_defaults(func=cmd_reconcile)

    ip = sub.add_parser("inject", help="（演练钩子）把指定 available 物件打成 on_loan 残局")
    ip.add_argument("item_id", type=int)
    ip.add_argument("--json", action="store_true")
    ip.set_defaults(func=cmd_inject)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
