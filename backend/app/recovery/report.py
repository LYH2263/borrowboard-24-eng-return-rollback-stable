"""报告渲染 —— 只读数据的展示层，不碰数据库、不做任何补偿决策。"""

from __future__ import annotations

import json


def render_scan(result: dict, as_json: bool = False) -> str:
    """渲染 scan 结果。报告必须列出全部残局 item_id。"""

    ids = result.get("stranded_item_ids", [])
    if as_json:
        return json.dumps(result, ensure_ascii=False, indent=2)
    lines = ["[残局扫描] 半插入（items.on_loan 但无 active loan）："]
    if ids:
        lines.append("  残局 item_id：" + ", ".join(str(i) for i in ids))
    else:
        lines.append("  残局 item_id：（空）")
    lines.append(f"  合计：{result.get('count', len(ids))}")
    return "\n".join(lines)


def render_reconcile(result: dict, as_json: bool = False) -> str:
    """渲染 reconcile 结果：补偿前列表、逐项动作、补偿后名单。"""

    if as_json:
        return json.dumps(result, ensure_ascii=False, indent=2)

    strategy = result.get("strategy", "?")
    before = result.get("before", [])
    after = result.get("after", [])
    actions = result.get("actions", [])
    lines = [f"[残局补偿] 策略：{strategy}"]
    lines.append("  补偿前列出的残局 item_id：" + (", ".join(map(str, before)) if before else "（空）"))
    if not actions:
        lines.append("  补偿动作：无")
    for a in actions:
        iid = a.get("item_id")
        act = a.get("action")
        if act == "inserted_active":
            lines.append(f"  - item {iid}：补 active 行（loan_id={a.get('loan_id')}，borrower=__recovered__）")
        elif act == "reset_available":
            lines.append(f"  - item {iid}：状态改回 available")
        elif act == "skipped_has_active":
            lines.append(f"  - item {iid}：已存在 {a.get('active_loans')} 行 active，跳过（不双插、不回退）")
        else:
            lines.append(f"  - item {iid}：{act}")
    lines.append("  补偿后残局 item_id：" + (", ".join(map(str, after)) if after else "（空）"))
    if result.get("converged"):
        lines.append("  对账结论：已收敛")
    else:
        lines.append(f"  对账结论：未收敛（{result.get('error') or '仍有残局残留'}）")
    return "\n".join(lines)
