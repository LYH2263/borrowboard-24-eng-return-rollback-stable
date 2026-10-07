"""残局处理命令入口（与借出业务路由分离，前端不感知）。

用法:
    python -m app.cli scan [--json]      # 只扫描并报告，不写库
    python -m app.cli recover [--json]   # 扫描 + 落地补偿
    python -m app.cli inject ITEM_ID [--json]  # 注入半插入残局（演练用）

退出码（对账失败绝不与成功 0 混用）:
    0  成功：账实相符；或补偿已完成（含空名单）；或注入成功
    1  扫描发现可补偿的半插入残局（orphan on_loan），尚未处理
    2  对账失败 / 补偿中止回滚：双 active、available 挂 active、提交前复核不过
    3  用法错误 / 注入被拒（物件不存在、不可借等）
"""
import argparse
import json
import sys

from app import seed
from app.recovery import report
from app.recovery.compensate import compensate
from app.recovery.injection import inject_half_lend
from app.recovery.scanner import is_reconciled, scan

EXIT_OK = 0
EXIT_ORPHAN_FOUND = 1
EXIT_UNRECONCILED = 2
EXIT_USAGE = 3


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description="借出半插入残局处理")
    sub = parser.add_subparsers(dest="command")
    sub.required = True
    for name in ("scan", "recover"):
        p = sub.add_parser(name)
        p.add_argument("--json", action="store_true", dest="as_json")
    p_inject = sub.add_parser("inject")
    p_inject.add_argument("item_id", type=int)
    p_inject.add_argument("--json", action="store_true", dest="as_json")

    args = parser.parse_args(argv)
    seed.init_db()

    if args.command == "scan":
        findings = scan()
        print(report.to_json(findings) if args.as_json else report.render_scan(findings))
        if not is_reconciled(findings):
            return EXIT_UNRECONCILED
        return EXIT_ORPHAN_FOUND if findings["orphan_on_loan"] else EXIT_OK

    if args.command == "recover":
        result = compensate()
        print(report.to_json(result) if args.as_json else report.render_recover(result))
        return EXIT_OK if result["ok"] else EXIT_UNRECONCILED

    if args.command == "inject":
        outcome = inject_half_lend(args.item_id)
        payload = {"outcome": outcome}
        print(report.to_json(payload) if args.as_json else report.render_inject(outcome))
        return EXIT_OK if outcome["ok"] else EXIT_USAGE

    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
