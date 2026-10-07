"""扫描半插入残局并在立即事务内落地补偿。

残局定义：``items.status='on_loan'`` 但 ``loans`` 中没有任何 ``status='active'``
行指向该 item —— 借出流程把 item 状态写进去了，loan 行却没落成。

补偿策略（入口二选一，默认 insert_active）：

- ``insert_active``：补一行 active loan（借用人记为 ``__recovered__``）；
- ``reset_available``：把 item 状态改回 ``available``。

并发安全：所有补偿在一个 ``BEGIN IMMEDIATE`` 写事务内完成，与借出/归还路由
的立即事务互斥。动作前在事务内复检 active 行数：

- 已有 active 行 → 绝不插入第二条（杜绝双 active）；
- 已有 active 行 → 绝不回退 available（杜绝在借物被刷回可借栏）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.db import connect, immediate_tx

INSERT_ACTIVE = "insert_active"
RESET_AVAILABLE = "reset_available"
STRATEGIES = (INSERT_ACTIVE, RESET_AVAILABLE)

RECOVERED_BORROWER = "__recovered__"
RECOVERED_DUE_DATE = "1970-01-01"

# 对账失败（不收敛 / 拿不到写锁 / 数据库错误）的退出码，绝不与成功 0 混用。
EXIT_OK = 0
EXIT_RECONCILE_FAILED = 2


def stranded_item_ids(conn: sqlite3.Connection) -> list[int]:
    """扫出 on_loan 但没有任何 active loan 的 item id。"""

    rows = conn.execute(
        """SELECT items.id FROM items
           LEFT JOIN loans ON loans.item_id = items.id AND loans.status = 'active'
           WHERE items.status = 'on_loan' AND loans.id IS NULL
           ORDER BY items.id"""
    ).fetchall()
    return [r["id"] for r in rows]


def active_count(conn: sqlite3.Connection, item_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) c FROM loans WHERE item_id=? AND status='active'",
        (item_id,),
    ).fetchone()["c"]


def scan() -> dict:
    """只读扫描，不做任何修改。"""

    conn = connect()
    try:
        ids = stranded_item_ids(conn)
    finally:
        conn.close()
    return {"stranded_item_ids": ids, "count": len(ids)}


def reconcile(strategy: str = INSERT_ACTIVE) -> dict:
    """扫描并补偿，返回结构化结果；进程据 ``converged`` 决定退出码。"""

    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}, choose from {STRATEGIES}")

    before = scan()["stranded_item_ids"]
    actions: list[dict] = []
    error = ""

    conn = connect()
    conn.row_factory = sqlite3.Row
    try:
        try:
            with immediate_tx(conn):
                # 拿到写锁后重新扫描：此刻任何借出/归还都无法提交，以此集合为准。
                ids = stranded_item_ids(conn)
                now = datetime.now(timezone.utc).isoformat()
                for iid in ids:
                    n = active_count(conn, iid)
                    if n > 0:
                        # 锁内复检发现已有 active（深度防御）：两种策略都不得破坏现状。
                        actions.append({"item_id": iid, "action": "skipped_has_active", "active_loans": n})
                        continue
                    if strategy == INSERT_ACTIVE:
                        cur = conn.execute(
                            "INSERT INTO loans(item_id,borrower,status,due_date,lent_at)"
                            " VALUES (?,?,?,?,?)",
                            (iid, RECOVERED_BORROWER, "active", RECOVERED_DUE_DATE, now),
                        )
                        actions.append({"item_id": iid, "action": "inserted_active", "loan_id": cur.lastrowid})
                    else:
                        conn.execute("UPDATE items SET status='available' WHERE id=?", (iid,))
                        actions.append({"item_id": iid, "action": "reset_available"})
        except sqlite3.OperationalError as exc:  # database is locked 等：拿不到写锁
            error = f"lock_failed: {exc}"
        except Exception as exc:  # 补偿本身失败：事务已回滚，按对账失败上报，绝不冒充成功
            error = f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()

    after = scan()["stranded_item_ids"]
    return {
        "strategy": strategy,
        "before": before,
        "actions": actions,
        "after": after,
        "converged": not error and not after,
        "error": error,
    }
