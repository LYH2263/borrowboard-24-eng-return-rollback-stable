"""残局补偿动作：只做一件事 —— 把"借出未成立"的物件改回 available。

残局语义是 loans 的 INSERT 失败，即这笔借出从未成立，因此落地策略选择
"把 items.status 改回 available"（而不是凭空补一行 active loan）。

与并发借 / 还共处的保证：
1. 全程在单个 `BEGIN IMMEDIATE` 事务里，先拿写锁，SQLite 同一时刻只有一个写者，
   并发借出 / 归还会阻塞到本事务提交后再继续，不存在交叉写入窗口。
2. 拿锁之后二次扫描，只对当时仍为 orphan（on_loan 且无 active 行）的 id 落地；
   若邻居的借出已在拿锁前提交，该物件已带 active 行，直接跳过，绝不改回 available。
3. 落地语句本身带 `NOT EXISTS (active)` 守卫并检查 rowcount；提交前在同一事务内
   复核：不得再有 orphan、不得有双 active、不得有 available 挂 active，任一不过
   整体回滚，一行都不落地。
本模块不删除、不重建任何表。
"""
from app.db import connect


def compensate() -> dict:
    """执行一次补偿，返回结构化结果，不抛"可预期"的对账异常。

    成功: {"ok": True, "repaired": [ids], "skipped": [...], "findings": 扫描快照}
    失败: {"ok": False, "reason": ..., "repaired": [], ...}（调用方对应退出码 2，已回滚）
    """
    c = connect()
    repaired, skipped = [], []
    try:
        c.execute("BEGIN IMMEDIATE")
        findings = _scan_locked(c)

        if findings["double_active"] or findings["available_with_active"]:
            c.rollback()
            return {"ok": False, "reason": "unreconciled_state", "repaired": [],
                    "skipped": [], "findings": findings}

        for iid in findings["orphan_on_loan"]:
            cur = c.execute(
                """UPDATE items
                      SET status='available', data_quality='recovered'
                    WHERE id=? AND status='on_loan'
                      AND NOT EXISTS (
                        SELECT 1 FROM loans l WHERE l.item_id=? AND l.status='active'
                      )""",
                (iid, iid),
            )
            if cur.rowcount == 1:
                repaired.append(iid)
            else:
                # 拿锁后仍有 active 行（理论上被二次扫描挡住），保守跳过并在复核阶段判失败。
                skipped.append(iid)

        post = _scan_locked(c)
        if post["orphan_on_loan"] or post["double_active"] or post["available_with_active"]:
            c.rollback()
            return {"ok": False, "reason": "post_check_failed", "repaired": [],
                    "skipped": skipped, "findings": post}

        c.commit()
        return {"ok": True, "repaired": repaired, "skipped": skipped, "findings": findings}
    except Exception as exc:
        c.rollback()
        return {"ok": False, "reason": "error:" + type(exc).__name__, "repaired": [],
                "skipped": skipped, "findings": None, "detail": str(exc)}
    finally:
        c.close()


def _scan_locked(c):
    """同一查询在已持写锁的事务内执行，读到的快照即本次落地依据。"""
    orphan_rows = c.execute(
        """SELECT i.id FROM items i
           WHERE i.status='on_loan'
             AND NOT EXISTS (SELECT 1 FROM loans l WHERE l.item_id=i.id AND l.status='active')
           ORDER BY i.id"""
    ).fetchall()
    double_rows = c.execute(
        """SELECT item_id, COUNT(*) c FROM loans
           WHERE status='active' GROUP BY item_id HAVING c >= 2 ORDER BY item_id"""
    ).fetchall()
    mismatch_rows = c.execute(
        """SELECT i.id FROM items i
           WHERE i.status='available'
             AND EXISTS (SELECT 1 FROM loans l WHERE l.item_id=i.id AND l.status='active')
           ORDER BY i.id"""
    ).fetchall()
    return {
        "orphan_on_loan": [r["id"] for r in orphan_rows],
        "double_active": [{"item_id": r["item_id"], "active_count": r["c"]} for r in double_rows],
        "available_with_active": [r["id"] for r in mismatch_rows],
    }
