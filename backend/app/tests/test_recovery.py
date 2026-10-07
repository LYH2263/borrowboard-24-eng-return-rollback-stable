"""半插入残局补偿的端到端测试。

覆盖：
- 注入 → 扫描报告列出 item_id → 补偿 → 可借栏/在借栏/顶细条(/api/board)与 sqlite 直读同一世界
- 成功路径连跑两次：第二次空名单，退出码与第一次同为 0
- 尚有 active 行的物件不得被改回 available
- 双 active / available 挂 active → 退出码 2，且 loans 一行不删
- 补偿与并发借 / 还共处：任何时刻无双 active、无 available 挂 active
"""
import os
import sqlite3
import threading
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("DATA_DIR", str(d))
    from app import seed
    seed.init_db()
    return d


@pytest.fixture()
def client(data_dir):
    from app.main import app
    return TestClient(app)


def raw(data_dir):
    c = sqlite3.connect(data_dir / "borrowboard.db")
    c.row_factory = sqlite3.Row
    return c


def assert_invariants(data_dir):
    """同一世界：不允许双 active，不允许 available 挂 active，不允许 on_loan 无 active。"""
    c = raw(data_dir)
    doubles = [dict(r) for r in c.execute(
        "SELECT item_id, COUNT(*) n FROM loans WHERE status='active' GROUP BY item_id HAVING n>=2")]
    bad_avail = [dict(r) for r in c.execute(
        """SELECT i.id FROM items i WHERE i.status='available'
           AND EXISTS (SELECT 1 FROM loans l WHERE l.item_id=i.id AND l.status='active')""")]
    orphans = [dict(r) for r in c.execute(
        """SELECT i.id FROM items i WHERE i.status='on_loan'
           AND NOT EXISTS (SELECT 1 FROM loans l WHERE l.item_id=i.id AND l.status='active')""")]
    c.close()
    assert doubles == [], doubles
    assert bad_avail == [], bad_avail
    assert orphans == [], orphans


def test_inject_scan_recovers_and_world_matches(data_dir, client):
    from app.recovery.injection import inject_half_lend
    from app.recovery.scanner import scan
    from app.recovery.compensate import compensate
    from app.cli import main as cli_main

    # 注入残局：item 1 被写成 on_loan 但没有 loans 行
    assert inject_half_lend(1)["ok"] is True
    findings = scan()
    assert findings["orphan_on_loan"] == [1], findings

    # 入口扫描报告必须列出该 id，退出码 1
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli_main(["scan"])
    assert code == 1
    assert "1" in buf.getvalue()

    # 此时 /api/board：可借栏没有 1，在借栏也没有 1（顶细条在借数与之一致）
    b = client.get("/api/board").json()
    assert all(i["id"] != 1 for i in b["available"])
    assert all(l["item_id"] != 1 for l in b["active"] + b["overdue"])
    assert b["counts"]["available"] == 2 and b["counts"]["active"] == 0 and b["counts"]["overdue"] == 1

    # 第一次补偿成功，退出码 0
    with contextlib.redirect_stdout(io.StringIO()):
        code1 = cli_main(["recover"])
    assert code1 == 0

    # 第二次：空名单，退出码与第一次相同（0）
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        code2 = cli_main(["recover"])
    assert code2 == 0 == code1
    assert "无（名单为空）" in buf2.getvalue()

    # 补偿后前端三个世界与 sqlite 直读一致
    b = client.get("/api/board").json()
    c = raw(data_dir)
    row = c.execute("SELECT status,data_quality FROM items WHERE id=1").fetchone()
    assert row["status"] == "available"
    c.close()
    # 可借栏出现 1；在借栏仍只有 seed 的 item 4；顶细条计数对齐
    assert any(i["id"] == 1 for i in b["available"])
    assert sorted(l["item_id"] for l in b["active"] + b["overdue"]) == [4]
    assert b["counts"] == {"available": 3, "active": 0, "overdue": 1}

    assert_invariants(data_dir)


def test_item_with_active_loan_is_not_reset_to_available(data_dir, client):
    """正常借出中的物件：补偿不得把它改回 available。"""
    from app.recovery.scanner import scan
    from app.recovery.compensate import compensate

    due = (date.today() + timedelta(days=7)).isoformat()
    r = client.post("/api/items/1/lend", json={"borrower": "邻居乙", "due_date": due})
    assert r.status_code == 200

    findings = scan()
    assert findings["orphan_on_loan"] == []
    result = compensate()
    assert result["ok"] is True and result["repaired"] == []

    c = raw(data_dir)
    st = c.execute("SELECT status FROM items WHERE id=1").fetchone()["status"]
    n = c.execute("SELECT COUNT(*) n FROM loans WHERE item_id=1 AND status='active'").fetchone()["n"]
    c.close()
    assert st == "on_loan" and n == 1
    assert_invariants(data_dir)


def test_double_active_is_exit_2_and_nothing_deleted(data_dir, client):
    """双 active：对账失败退出码 2，不得删 loans、不得改 items 刷绿。"""
    from app.cli import main as cli_main
    import io, contextlib

    c = raw(data_dir)
    c.execute("INSERT INTO loans(item_id,borrower,status,due_date,lent_at) VALUES (?,?,?,?,?)",
              (4, "邻居丙", "active", "2099-01-01", "2026-10-01"))
    c.commit()
    before = c.execute("SELECT COUNT(*) n FROM loans").fetchone()["n"]
    c.close()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli_main(["recover"])
    assert code == 2
    assert "4" in buf.getvalue()

    c = raw(data_dir)
    after = c.execute("SELECT COUNT(*) n FROM loans").fetchone()["n"]
    item4 = c.execute("SELECT status FROM items WHERE id=4").fetchone()["status"]
    c.close()
    assert after == before, "禁止删 loans 行刷绿"
    assert item4 == "on_loan"
    # scan 同样报 2
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli_main(["scan"]) == 2


def test_available_with_active_is_exit_2(data_dir):
    """items=available 却挂 active loan：不可自动对账，退出码 2。"""
    from app.recovery.scanner import scan
    from app.recovery.compensate import compensate

    c = raw(data_dir)
    c.execute("UPDATE items SET status='available' WHERE id=4")
    c.commit(); c.close()

    findings = scan()
    assert findings["available_with_active"] == [4]
    result = compensate()
    assert result["ok"] is False and result["reason"] == "unreconciled_state"

    c = raw(data_dir)
    n = c.execute("SELECT COUNT(*) n FROM loans WHERE item_id=4 AND status='active'").fetchone()["n"]
    c.close()
    assert n == 1


def test_inject_refuses_non_available(data_dir, client):
    from app.recovery.injection import inject_half_lend
    due = (date.today() + timedelta(days=7)).isoformat()
    client.post("/api/items/1/lend", json={"borrower": "x", "due_date": due})
    assert inject_half_lend(1)["ok"] is False
    assert inject_half_lend(999)["ok"] is False


def test_cli_exit_codes_and_json(data_dir):
    """注入被拒退出码 3；scan/recover --json 输出合法 JSON 且退出码语义不变。"""
    import io, contextlib, json
    from app.cli import main as cli_main

    with contextlib.redirect_stdout(io.StringIO()):
        assert cli_main(["inject", "999"]) == 3

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli_main(["scan", "--json"])
    assert code == 0
    assert json.loads(buf.getvalue())["orphan_on_loan"] == []

    from app.recovery.injection import inject_half_lend
    inject_half_lend(1)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli_main(["scan", "--json"])
    assert code == 1
    assert json.loads(buf.getvalue())["orphan_on_loan"] == [1]


def test_recover_concurrent_with_lend_and_return(data_dir, client):
    """补偿与并发借出 / 归还共处：邻居不能借到双 active，补偿不抢在借之后改回 available。"""
    from app.recovery.injection import inject_half_lend
    from app.recovery.compensate import compensate

    # 两个残局 + 若干正常可借物
    inject_half_lend(1)
    inject_half_lend(2)

    errors = []
    barrier = threading.Barrier(3)

    def lend_return_loop():
        try:
            due = (date.today() + timedelta(days=10)).isoformat()
            barrier.wait()
            # 借出 item 3 并归还若干轮
            for _ in range(5):
                r = client.post("/api/items/3/lend", json={"borrower": "并发邻居", "due_date": due})
                if r.status_code != 200:
                    errors.append("lend status %s" % r.status_code); continue
                lid = r.json()["loan_id"]
                rr = client.post("/api/loans/%d/return" % lid, json={})
                if rr.status_code != 200:
                    errors.append("return status %s" % rr.status_code)
        except Exception as exc:
            errors.append(repr(exc))

    def lend_compensated_item():
        # 尝试抢借残局物件 2：补偿提交后才应可借；借出后补偿不得再把它改回
        try:
            barrier.wait()
            from app.db import connect as _connect
            import time
            due = (date.today() + timedelta(days=10)).isoformat()
            for _ in range(20):
                r = client.post("/api/items/2/lend", json={"borrower": "抢借邻居", "due_date": due})
                if r.status_code == 200:
                    break
                time.sleep(0.02)
            # 再跑补偿，item 2 必须保持 on_loan 且有 active
            result = compensate()
            assert result["ok"]
        except Exception as exc:
            errors.append(repr(exc))

    t1 = threading.Thread(target=lend_return_loop)
    t2 = threading.Thread(target=lend_compensated_item)
    t1.start(); t2.start()
    barrier.wait()

    result = compensate()
    t1.join(); t2.join()

    assert errors == [], errors
    assert result["ok"] is True
    # 1 必然被补；2 要么被补（没人抢借成功），要么被正常借出持有 —— 两种都必须账实相符
    c = raw(data_dir)
    st1 = c.execute("SELECT status FROM items WHERE id=1").fetchone()["status"]
    st2 = c.execute("SELECT status FROM items WHERE id=2").fetchone()["status"]
    n2 = c.execute("SELECT COUNT(*) n FROM loans WHERE item_id=2 AND status='active'").fetchone()["n"]
    c.close()
    assert st1 == "available"
    assert (st2 == "available" and n2 == 0) or (st2 == "on_loan" and n2 == 1)
    assert_invariants(data_dir)
