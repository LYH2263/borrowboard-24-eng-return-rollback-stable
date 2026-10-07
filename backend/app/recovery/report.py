"""报告渲染：只负责把扫描 / 补偿结果渲染成人读文本或 JSON，不碰数据库。"""
import json


def render_scan(findings: dict) -> str:
    orphans = findings["orphan_on_loan"]
    doubles = findings["double_active"]
    mismatches = findings["available_with_active"]
    lines = ["[残局扫描报告]"]
    lines.append("半插入残局 item_id（on_loan 但无 active loan）: %s"
                 % (orphans if orphans else "无"))
    if doubles:
        lines.append("双 active（不可自动补偿，需人工介入）:")
        for d in doubles:
            lines.append("  item_id=%s active 笔数=%s" % (d["item_id"], d["active_count"]))
    else:
        lines.append("双 active: 无")
    if mismatches:
        lines.append("available 却挂 active loan（不可自动补偿）: %s" % mismatches)
    else:
        lines.append("available 挂 active: 无")
    return "\n".join(lines)


def render_recover(result: dict) -> str:
    lines = ["[残局补偿报告]"]
    if not result.get("ok"):
        lines.append("补偿失败 / 已回滚，原因: %s" % result.get("reason"))
        if result.get("detail"):
            lines.append("细节: %s" % result["detail"])
        if result.get("findings"):
            lines.append(render_scan(result["findings"]))
        return "\n".join(lines)
    lines.append("补偿成功。修复 item_id: %s"
                 % (result["repaired"] if result["repaired"] else "无（名单为空）"))
    if result.get("skipped"):
        lines.append("跳过（已有 active 行，未改回 available）: %s" % result["skipped"])
    return "\n".join(lines)


def render_inject(outcome: dict) -> str:
    if outcome.get("ok"):
        return "[注入] 已制造半插入残局，item_id=%s（items=on_loan，无 loans 行）" % outcome["item_id"]
    return "[注入] 未注入: %s" % outcome.get("reason")


def to_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
