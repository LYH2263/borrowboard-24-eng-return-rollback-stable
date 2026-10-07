"""扫描入口：只读，把残局 / 异常状态扫出来，不做任何修改。

三类状态：
- orphan_on_loan       items.status='on_loan' 却没有 active loan —— 借出半插入残局，可补偿
- double_active        同一物件存在 >=2 笔 active loan —— 不可自动对账，必须人工介入
- available_with_active items.status='available' 却挂着 active loan —— 同样不可自动对账

扫描不区分数据来源，因此并发借出 / 归还期间产生的中间态也会被如实反映；
补偿动作（compensate.py）在写锁内二次扫描后才落地。
"""
from app.db import connect


def scan() -> dict:
    c = connect()
    try:
        orphan_rows = c.execute(
            """SELECT i.id FROM items i
               WHERE i.status='on_loan'
                 AND NOT EXISTS (
                   SELECT 1 FROM loans l WHERE l.item_id=i.id AND l.status='active'
                 )
               ORDER BY i.id"""
        ).fetchall()
        double_rows = c.execute(
            """SELECT item_id, COUNT(*) c FROM loans
               WHERE status='active' GROUP BY item_id HAVING c >= 2
               ORDER BY item_id"""
        ).fetchall()
        mismatch_rows = c.execute(
            """SELECT i.id FROM items i
               WHERE i.status='available'
                 AND EXISTS (
                   SELECT 1 FROM loans l WHERE l.item_id=i.id AND l.status='active'
                 )
               ORDER BY i.id"""
        ).fetchall()
        return {
            "orphan_on_loan": [r["id"] for r in orphan_rows],
            "double_active": [{"item_id": r["item_id"], "active_count": r["c"]} for r in double_rows],
            "available_with_active": [r["id"] for r in mismatch_rows],
        }
    finally:
        c.close()


def is_reconciled(findings: dict) -> bool:
    """除可补偿的 orphan 外无其他异常，即视为账实相符。"""
    return not findings["double_active"] and not findings["available_with_active"]
