import contextlib
import os
import sqlite3
from pathlib import Path

def db_path() -> Path:
    d = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "borrowboard.db"

def connect():
    c = sqlite3.connect(db_path(), timeout=5)
    c.row_factory = sqlite3.Row
    # 写锁竞争时等待而非立刻报 database is locked；补偿与再借/归还因此排队共处。
    c.execute("PRAGMA busy_timeout=5000")
    return c

@contextlib.contextmanager
def immediate_tx(c: sqlite3.Connection):
    """立即写事务：进入即拿全库写锁，借出/归还/补偿彼此串行化。"""
    prev = c.isolation_level
    c.isolation_level = None  # 关闭 sqlite3 隐式事务，手控 BEGIN/COMMIT
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except Exception:
        with contextlib.suppress(sqlite3.Error):
            c.execute("ROLLBACK")
        raise
    finally:
        c.isolation_level = prev
