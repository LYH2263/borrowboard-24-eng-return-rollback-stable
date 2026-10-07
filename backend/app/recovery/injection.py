"""注入钩子：只负责制造 / 模拟"借出半插入"残局，不参与正常借还业务。

模拟的故障窗口：借出事务中 items 已被写成 on_loan，
而 loans 的 INSERT 失败（约束冲突、磁盘错误、进程被 kill 等），
于是留下一条 status='on_loan' 却没有任何 active loan 的物件。

该钩子独立于 app.main 的借出路由，仅由命令入口 app.cli 调用。
"""
from app.db import connect


def inject_half_lend(item_id: int) -> dict:
    """把指定 available 物件直接置为 on_loan，且不写 loans 行。

    返回 {"ok": True, "item_id": ...}；
    物件不存在 / 当前不可借 / 已有 active loan 时返回 {"ok": False, "reason": ...}，
    保证注入的一定是干净可识别的半插入残局，而不是双 active。
    """
    c = connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        item = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if item is None:
            c.rollback()
            return {"ok": False, "reason": "item_not_found"}
        active = c.execute(
            "SELECT COUNT(*) c FROM loans WHERE item_id=? AND status='active'", (item_id,)
        ).fetchone()["c"]
        if active > 0:
            c.rollback()
            return {"ok": False, "reason": "already_has_active_loan"}
        if item["status"] != "available":
            c.rollback()
            return {"ok": False, "reason": "item_not_available"}
        c.execute("UPDATE items SET status='on_loan' WHERE id=?", (item_id,))
        c.commit()
        return {"ok": True, "item_id": item_id}
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
