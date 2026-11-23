"""仓间锁与争用登记（公共并发面）。

锁表 chamber_locks 与争用表 contention_log 的读写一律走独立自动提交连接，
与业务事务解耦：
- 占锁立即对全库可见，两名调度同仓间并发建批时后到者必然看到先到者的锁；
- 业务 409 回滚不会把争用痕或锁行带走；
- 业务 finally 释放锁不受业务连接是否已提交影响。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit

router = APIRouter(tags=["locks"])


class LockIn(BaseModel):
    lock_key: str
    action: str = "acquire"


def _lock_conn():
    # 独立连接 + 自动提交：锁状态不等业务事务提交即生效
    conn = connect()
    conn.isolation_level = None
    return conn


def _record_contention(lock_key: str, holder: str, detail: str) -> None:
    with _lock_conn() as conn:
        conn.execute(
            """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
               VALUES (?,?,?,?,?)""",
            (lock_key, holder, "争用", detail, now_iso()),
        )


def try_acquire(conn, lock_key: str, holder: str) -> bool:
    """占锁。conn 仅为沿用调用面，实际在独立自动提交连接上完成。"""
    with _lock_conn() as lconn:
        # 无冲突语义：锁已存在时 SELECT 端为空、rowcount=0；
        # 不用 INSERT OR IGNORE —— 其 IGNORE 在并发下会走替换路径改写持有者。
        cur = lconn.execute(
            """INSERT INTO chamber_locks(lock_key,holder,acquired_at)
               SELECT ?, ?, ? WHERE NOT EXISTS (
                   SELECT 1 FROM chamber_locks WHERE lock_key=?)""",
            (lock_key, holder, now_iso(), lock_key),
        )
        if cur.rowcount == 1:
            return True
        row = lconn.execute(
            "SELECT holder FROM chamber_locks WHERE lock_key=?", (lock_key,)
        ).fetchone()
        if row is not None and row["holder"] == holder:
            return True
    # 后到只写痕，不插业务行
    _record_contention(
        lock_key, holder, f"持有者={row['holder'] if row else '未知'}"
    )
    return False


def release(conn, lock_key: str, holder: str) -> None:
    with _lock_conn() as lconn:
        lconn.execute(
            "DELETE FROM chamber_locks WHERE lock_key=? AND holder=?",
            (lock_key, holder),
        )


def require_lock(conn, lock_key: str, holder: str) -> None:
    if not try_acquire(conn, lock_key, holder):
        raise HTTPException(409, "争用：后到只记争用，不写业务")


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM chamber_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/contention")
def list_contention(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/locks/acquire")
def acquire_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    ok = try_acquire(None, body.lock_key, user)
    if not ok:
        raise HTTPException(409, "争用：未能获得锁")
    with connect() as conn:
        write_audit(conn, user, "lock_acquire", body.lock_key, {"ok": ok})
        conn.commit()
    return {"ok": True, "lock_key": body.lock_key, "holder": user}


@router.post("/api/locks/release")
def release_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    release(None, body.lock_key, user)
    with connect() as conn:
        write_audit(conn, user, "lock_release", body.lock_key, {})
        conn.commit()
    return {"ok": True}
