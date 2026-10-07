"""残局注入钩子 —— 只在命令行 / 测试场景下使用，绝不挂在借出业务路由上。

模拟 "items 已写成 on_loan，但 loans 插入失败" 的半插入残局：
item 被置为 on_loan，却没有任何 active loan 与之对应。
"""

from __future__ import annotations

import sqlite3

from app.db import connect


def inject_half_insert(item_id: int, conn: sqlite3.Connection | None = None) -> dict:
    """把一个 available 物件打成 on_loan 而不写 loans 行 —— 制造残局。

    仅由命令入口 ``python -m app.recovery.cli inject <item_id>`` 或测试调用，
    与借出业务路由完全隔离。传入 ``conn`` 时不提交，由调用方掌控事务边界。
    """

    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise LookupError(f"item {item_id} not found")
        if row["status"] != "available":
            raise ValueError(f"item {item_id} is {row['status']}, only available item can be stranded")
        conn.execute("UPDATE items SET status='on_loan' WHERE id=?", (item_id,))
        if own:
            conn.commit()
        return {"item_id": item_id, "stranded": True}
    finally:
        if own:
            conn.close()
