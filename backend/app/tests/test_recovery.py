"""半插入残局补偿的验收测试：扫描、补偿、退出码、并发共处、世界一致性。

每个用例都用独立 DATA_DIR，绝不碰业务库，也不靠删表 / 重建刷绿。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from app import seed
from app.db import connect, db_path, immediate_tx
from app.recovery import report
from app.recovery.cli import main as cli_main
from app.recovery.inject import inject_half_insert
from app.recovery.reconcile import (
    INSERT_ACTIVE,
    RESET_AVAILABLE,
    active_count,
    reconcile,
    scan,
)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    return tmp_path


def raw_conn(busy_ms: int = 5000) -> sqlite3.Connection:
    c = sqlite3.connect(db_path(), timeout=busy_ms / 1000)
    c.row_factory = sqlite3.Row
    c.execute(f"PRAGMA busy_timeout={busy_ms}")
    return c


def item_status(iid: int) -> str:
    c = raw_conn()
    s = c.execute("SELECT status FROM items WHERE id=?", (iid,)).fetchone()["status"]
    c.close()
    return s


# ---------- 注入 + 扫描 ----------

def test_scan_lists_stranded_ids(db):
    # seed 的 item 4 是 on_loan 且有 active 行（正常在借），不得被当成残局。
    assert scan()["stranded_item_ids"] == []
    inject_half_insert(1)
    inject_half_insert(2)
    r = scan()
    assert r["stranded_item_ids"] == [1, 2] and r["count"] == 2
    text = report.render_scan(r)
    assert "1" in text and "2" in text  # 报告必须列出残局 id
    assert {1, 2} <= set(json.loads(report.render_scan(r, as_json=True))["stranded_item_ids"])


def test_inject_only_for_available(db):
    with pytest.raises(ValueError):
        inject_half_insert(4)  # 已 on_loan，拒绝
    with pytest.raises(LookupError):
        inject_half_insert(999)


# ---------- 两种补偿策略 ----------

def test_reconcile_insert_active_then_idempotent_empty(db):
    inject_half_insert(1)
    res = reconcile(INSERT_ACTIVE)
    assert res["before"] == [1] and res["after"] == [] and res["converged"] is True
    assert res["actions"][0]["action"] == "inserted_active"

    c = raw_conn()
    row = c.execute("SELECT status FROM items WHERE id=1").fetchone()
    n = c.execute("SELECT COUNT(*) c FROM loans WHERE item_id=1 AND status='active'").fetchone()["c"]
    who = c.execute("SELECT borrower FROM loans WHERE item_id=1 AND status='active'").fetchone()["borrower"]
    c.close()
    assert row["status"] == "on_loan" and n == 1 and who == "__recovered__"  # sqlite 直读对上

    # 连跑第二次：空名单，同样收敛
    again = reconcile(INSERT_ACTIVE)
    assert again["before"] == [] and again["after"] == [] and again["converged"] is True
    assert "（空）" in report.render_reconcile(again)


def test_reconcile_reset_available(db):
    inject_half_insert(2)
    loans_before = raw_conn().execute("SELECT COUNT(*) c FROM loans").fetchone()["c"]
    res = reconcile(RESET_AVAILABLE)
    assert res["converged"] and res["actions"][0]["action"] == "reset_available"
    assert item_status(2) == "available"
    c = raw_conn()
    loans_after = c.execute("SELECT COUNT(*) c FROM loans").fetchone()["c"]
    n = c.execute("SELECT COUNT(*) c FROM loans WHERE item_id=2").fetchone()["c"]
    c.close()
    assert loans_after == loans_before  # reset 不新增、更不删除任何 loan 行
    assert n == 0
    assert reconcile(RESET_AVAILABLE)["after"] == []


def test_reset_must_not_touch_item_with_active_loan(db):
    # item 4 = 正常在借（on_loan + active），reset 策略绝不能把它刷回 available
    assert item_status(4) == "on_loan"
    res = reconcile(RESET_AVAILABLE)
    assert res["actions"] == [] and res["converged"] is True
    assert item_status(4) == "on_loan"
    assert active_count(raw_conn(), 4) == 1


# ---------- 并发：补偿与别的写者共处，不产生双 active ----------

def test_concurrent_winner_active_is_not_double_inserted(db):
    inject_half_insert(1)
    winner_done = threading.Event()

    def winner():
        # 模拟在补偿排队等锁期间，另一个写者（邻居再借/另一补偿进程）先落成 active
        c = raw_conn()
        c.isolation_level = None
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            "INSERT INTO loans(item_id,borrower,status,due_date,lent_at) VALUES (?,?,?,?,?)",
            (1, "邻居乙", "active", "2026-12-31", "2026-10-07T00:00:00+00:00"),
        )
        winner_done.set()
        time.sleep(0.5)  # 持锁，迫使补偿在门外等
        c.execute("COMMIT")
        c.close()

    t = threading.Thread(target=winner)
    t.start()
    winner_done.wait(5)
    res = reconcile(INSERT_ACTIVE)  # busy_timeout=5000，排队等锁
    t.join()

    # 拿到锁时邻居已提交：item 不再是残局，补偿不得再补第二行
    assert res["converged"] is True and res["actions"] == [] and res["after"] == []
    c = raw_conn()
    n = c.execute("SELECT COUNT(*) c FROM loans WHERE item_id=1 AND status='active'").fetchone()["c"]
    who = c.execute("SELECT borrower FROM loans WHERE item_id=1 AND status='active'").fetchone()["borrower"]
    c.close()
    assert n == 1 and who == "邻居乙"  # 只有邻居那一笔，没有补偿的第二笔


def test_reconcile_coexists_with_real_lend_and_return(db):
    from app.main import LendIn, lend, return_loan

    # insert_active 补回的 loan 可以正常归还 → 回到可借栏 → 邻居再借成功
    inject_half_insert(1)
    lid = reconcile(INSERT_ACTIVE)["actions"][0]["loan_id"]
    assert return_loan(lid) == {"ok": True}
    assert item_status(1) == "available"
    lend(1, LendIn(borrower="邻居丙", due_date="2026-12-31"))
    assert item_status(1) == "on_loan"
    assert active_count(raw_conn(), 1) == 1

    # reset_available 后邻居直接再借
    inject_half_insert(2)
    reconcile(RESET_AVAILABLE)
    lend(2, LendIn(borrower="邻居丁", due_date="2026-12-31"))
    assert item_status(2) == "on_loan"
    assert active_count(raw_conn(), 2) == 1


# ---------- 看板 / 顶细条与 sqlite 直读同一世界 ----------

def test_board_counts_agree_with_sqlite_after_compensation(db):
    from app.main import board

    inject_half_insert(1)
    inject_half_insert(2)
    reconcile(INSERT_ACTIVE)
    b = board()

    c = raw_conn()
    avail_ids = {r["id"] for r in c.execute("SELECT id FROM items WHERE status='available'")}
    onloan_rows = c.execute(
        """SELECT items.id, COUNT(loans.id) n FROM items
           LEFT JOIN loans ON loans.item_id=items.id AND loans.status='active'
           WHERE items.status='on_loan' GROUP BY items.id"""
    ).fetchall()
    c.close()

    assert {i["id"] for i in b["available"]} == avail_ids
    assert all(r["n"] == 1 for r in onloan_rows)  # 每个在借物恰有一笔 active
    active_items = {l["item_id"] for l in b["active"]} | {l["item_id"] for l in b["overdue"]}
    assert active_items == {r["id"] for r in onloan_rows}
    assert b["counts"]["available"] == len(avail_ids)
    assert b["counts"]["active"] + b["counts"]["overdue"] == len(onloan_rows)


# ---------- 命令入口与退出码 ----------

def test_cli_two_runs_same_zero_exit_and_empty_report(db, capsys):
    assert cli_main(["inject", "1"]) == 0
    assert cli_main(["reconcile"]) == 0
    out1 = capsys.readouterr().out
    assert "1" in out1 and "已收敛" in out1

    code2 = cli_main(["reconcile"])
    out2 = capsys.readouterr().out
    assert code2 == 0  # 第二次退出码与第一次成功相同
    assert "补偿前列出的残局 item_id：（空）" in out2
    assert "补偿后残局 item_id：（空）" in out2

    assert cli_main(["scan"]) == 0  # 扫描本身成功也是 0
    assert "（空）" in capsys.readouterr().out


def test_cli_reconcile_failure_exit_code_is_not_zero(db, monkeypatch, capsys):
    import app.recovery.reconcile as rc

    inject_half_insert(1)
    release = threading.Event()

    def holder():
        c = raw_conn()
        c.isolation_level = None
        c.execute("BEGIN IMMEDIATE")
        release.set()
        time.sleep(1.0)
        c.execute("ROLLBACK")
        c.close()

    def no_wait_connect():
        c = sqlite3.connect(db_path(), timeout=0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=0")
        return c

    t = threading.Thread(target=holder)
    t.start()
    release.wait(5)
    monkeypatch.setattr(rc, "connect", no_wait_connect)  # 补偿拿不到锁立刻失败
    try:
        code = cli_main(["reconcile", "--json"])
    finally:
        t.join()
    assert code == 2  # 对账失败退出码绝不与成功 0 混用
    payload = json.loads(capsys_text(capsys))
    assert payload["converged"] is False and payload["after"] == [1]
    assert "lock_failed" in payload["error"]
    # 失败不得动数据：残局原样保留，没有偷偷补行
    assert item_status(1) == "on_loan"
    assert active_count(raw_conn(), 1) == 0


def test_cli_bad_inject_exit_one(db, capsys):
    assert cli_main(["inject", "999"]) == 1
    assert "注入失败" in capsys.readouterr().err


# ---------- 禁止删表 / 重建 ----------

def test_compensation_never_drops_loans_table(db):
    c = raw_conn()
    c.execute(
        "INSERT INTO loans(item_id,borrower,status,due_date,lent_at) VALUES (?,?,?,?,?)",
        (3, "留痕", "returned", "2020-02-01", "2020-01-01"),
    )
    c.commit()
    kept = [dict(r) for r in c.execute("SELECT * FROM loans WHERE item_id=3")]
    c.close()

    inject_half_insert(1)
    reconcile(INSERT_ACTIVE)
    c = raw_conn()
    still = [dict(r) for r in c.execute("SELECT * FROM loans WHERE item_id=3")]
    tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    c.close()
    assert still == kept and {"items", "loans", "settings"} <= tables


def capsys_text(capsys):
    return capsys.readouterr().out
