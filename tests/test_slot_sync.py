"""槽位同步三红测试（BUGFIX-06）。

口径（槽位同步说明，当班封死，禁止折中、禁止双写）：
- 甲路径：正式批次落库与仓间槽位占用同事务；任一失败全单回滚。
  本仓实现以 temp_batches 正式行为唯一占用事实源（COUNT 同源即同事务），
  不另立可漂移的占用计数器。
- 乙路径：排产条可先落影子副本，正式占用零改；配额复核后原子并入，否则副本作废。
  本仓未启用影子路径：任何被拒写请求都不得留下影子行/半成品行/占用痕迹。
- 两条路径择一，不得混用；偏离者记“槽位偏航”并拒写。
- 探头、对账、封批、批次、仓间五处视图槽位必须同源同值。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str = "admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _chamber(c: TestClient, h: dict, code: str) -> dict:
    return next(x for x in c.get("/api/chambers", headers=h).json()["items"]
                if x["chamber_code"] == code)


def _probe(c: TestClient, h: dict, chamber_id: int, code: str) -> dict:
    rows = c.get(f"/api/probes?chamber_id={chamber_id}", headers=h).json()["items"]
    return next(x for x in rows if x["probe_code"] == code)


def _slot_from_db(chamber_id: int) -> int:
    with connect() as conn:
        return conn.execute(
            """SELECT COUNT(*) c FROM temp_batches
               WHERE chamber_id=? AND status NOT IN ('已封批','作废')""",
            (chamber_id,),
        ).fetchone()["c"]


def _batch_count(chamber_id: int) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM temp_batches WHERE chamber_id=?",
            (chamber_id,),
        ).fetchone()["c"]


# ---------- 红测甲：正式批次落库，槽位须同事务抬一，否则全单回滚 ----------

def test_jia_formal_batch_lifts_slot_same_txn():
    c = TestClient(app)
    h = _auth(c)
    ch = _chamber(c, h, "CK-HN-03")  # 种子仅 1 个草稿批，占用基线 1
    probe = _probe(c, h, ch["id"], "PR-HN-C1")
    before = _slot_from_db(ch["id"])
    assert before == 1

    r = c.post(
        "/api/batches",
        headers=h,
        json={
            "chamber_id": ch["id"],
            "probe_id": probe["id"],
            "cargo_ref": "甲路径同事务货载",
            "batch_code": "TB-SLOT-JIA-1",
            "temp_points": [{"t": "08:00", "c": 4.0}, {"t": "09:00", "c": 5.0}],
        },
    )
    assert r.status_code == 200, r.text
    # 批已建 → 仓间槽位必须立刻抬一（同事务，无滞后）
    assert _slot_from_db(ch["id"]) == before + 1
    detail = c.get(f"/api/chambers/{ch['id']}", headers=h).json()
    assert detail["active_batch_count"] == before + 1
    assert detail["quota_left"] == int(ch["quota_batches"]) - (before + 1)


def test_jia_quota_full_rejects_and_rolls_back():
    c = TestClient(app)
    h = _auth(c)
    # 新建配额为 1 的仓间，先占满，再争一个槽位
    r = c.post(
        "/api/chambers",
        headers=h,
        json={
            "chamber_code": "CK-Q-01",
            "region": "华东",
            "name": "配额一仓",
            "target_min": -22,
            "target_max": -18,
            "quota_batches": 1,
        },
    )
    assert r.status_code == 200, r.text
    qch = c.get("/api/chambers", headers=h).json()["items"][-1]
    pr = c.post(
        "/api/probes",
        headers=h,
        json={
            "probe_code": "PR-Q-1",
            "chamber_id": qch["id"],
            "model": "T-100",
            "calibration_due": "2027-01-01",
        },
    ).json()
    points = [{"t": "08:00", "c": -20.0}]
    first = c.post(
        "/api/batches",
        headers=h,
        json={"chamber_id": qch["id"], "probe_id": pr["id"],
              "cargo_ref": "首占", "batch_code": "TB-Q-1", "temp_points": points},
    )
    assert first.status_code == 200, first.text
    assert _slot_from_db(qch["id"]) == 1

    rejected = c.post(
        "/api/batches",
        headers=h,
        json={"chamber_id": qch["id"], "probe_id": pr["id"],
              "cargo_ref": "强挤", "batch_code": "TB-Q-2", "temp_points": points},
    )
    # 配额满必须拒写而非放行
    assert rejected.status_code == 409, rejected.text
    # 甲路径全单回滚：无第二行、槽位仍旧值、无半成品
    assert _batch_count(qch["id"]) == 1
    assert _slot_from_db(qch["id"]) == 1
    detail = c.get(f"/api/chambers/{qch['id']}", headers=h).json()
    assert detail["active_batch_count"] == 1
    assert detail["quota_left"] == 0


# ---------- 红测乙：未启用影子路径，被拒请求零痕迹（副本作废即不落） ----------

def test_yi_no_shadow_no_sidewrite_on_reject():
    c = TestClient(app)
    h = _auth(c)
    ch = _chamber(c, h, "CK-HB-02")  # 含离线探头 PR-HB-B2
    offline = _probe(c, h, ch["id"], "PR-HB-B2")
    slot_before = _slot_from_db(ch["id"])
    rows_before = _batch_count(ch["id"])

    rejected = c.post(
        "/api/batches",
        headers=h,
        json={
            "chamber_id": ch["id"],
            "probe_id": offline["id"],
            "cargo_ref": "影子企图",
            "batch_code": "TB-SHADOW-YI-1",
            "temp_points": [{"t": "08:00", "c": -21.0}],
        },
    )
    # 离线探头禁止写批次
    assert rejected.status_code in (409, 422), rejected.text
    # 乙路径封死：正式占用零改，且无任何影子/半成品行残留
    assert _slot_from_db(ch["id"]) == slot_before
    assert _batch_count(ch["id"]) == rows_before
    with connect() as conn:
        shadow = conn.execute(
            "SELECT COUNT(*) c FROM temp_batches WHERE batch_code=?",
            ("TB-SHADOW-YI-1",),
        ).fetchone()["c"]
    assert shadow == 0
    # 排产条（route_plans）与批次占用不得混用，拒写不新增排产条
    with connect() as conn:
        routes = conn.execute("SELECT COUNT(*) c FROM route_plans").fetchone()["c"]
    assert routes >= 1  # 种子排产条仍在
    assert not any(
        x.get("route_code") == "TB-SHADOW-YI-1"
        for x in c.get("/api/routes", headers=h).json()["items"]
    )


# ---------- 红测争用：双人同间抢建，串行占锁，后到只留痕不插第二行 ----------

def test_contention_two_dispatchers_one_row():
    import locks as locks_module

    c = TestClient(app)
    h_admin = _auth(c, "admin")
    h_ops = _auth(c, "ops")

    # 配额 1 的仓间：槽位只有一个，双请求无论如何时序都不可能双行
    r = c.post(
        "/api/chambers",
        headers=h_admin,
        json={
            "chamber_code": "CK-CONT-01",
            "region": "华东",
            "name": "争用单槽仓",
            "target_min": -22,
            "target_max": -18,
            "quota_batches": 1,
        },
    )
    assert r.status_code == 200, r.text
    ch = next(x for x in c.get("/api/chambers", headers=h_admin).json()["items"]
              if x["chamber_code"] == "CK-CONT-01")
    pr = c.post(
        "/api/probes",
        headers=h_admin,
        json={"probe_code": "PR-CONT-1", "chamber_id": ch["id"],
              "model": "T-100", "calibration_due": "2027-01-01"},
    ).json()
    lock_key = f"chamber:{ch['id']}"

    # 确定性部分 A：admin 先持仓间锁，ops 后到 → 409、留痕、零行业务
    assert c.post("/api/locks/acquire", headers=h_admin,
                  json={"lock_key": lock_key}).status_code == 200
    blocked = c.post(
        "/api/batches",
        headers=h_ops,
        json={"chamber_id": ch["id"], "probe_id": pr["id"],
              "cargo_ref": "后到调度", "batch_code": "TB-CONT-2",
              "temp_points": [{"t": "08:00", "c": -20.0}]},
    )
    assert blocked.status_code == 409, blocked.text
    traces = c.get("/api/contention", headers=h_admin).json()["items"]
    assert any(x["lock_key"] == lock_key and x["actor"] == "ops" for x in traces)
    assert _batch_count(ch["id"]) == 0
    assert _slot_from_db(ch["id"]) == 0
    c.post("/api/locks/release", headers=h_admin, json={"lock_key": lock_key})

    # 确定性部分 B：锁层同点齐发，恰一胜一负，负者留痕
    gate = threading.Event()
    lock_outcomes: list[bool] = []

    def lock_worker(holder: str) -> None:
        gate.wait()
        lock_outcomes.append((holder, locks_module.try_acquire(None, lock_key, holder)))

    lt1 = threading.Thread(target=lock_worker, args=("admin",))
    lt2 = threading.Thread(target=lock_worker, args=("ops",))
    lt1.start(); lt2.start()
    gate.set()
    lt1.join(10); lt2.join(10)
    assert sorted(ok for _, ok in lock_outcomes) == [False, True], lock_outcomes
    winner = next(holder for holder, ok in lock_outcomes if ok)
    held = c.get("/api/locks", headers=h_admin).json()["items"]
    assert [x["holder"] for x in held if x["lock_key"] == lock_key] == [winner]
    c.post("/api/locks/release", headers=h_admin, json={"lock_key": lock_key})

    # 真实双请求同间抢建（两连接，模拟两名调度同时点“建批”）。
    # 不挂跨事务闸（SQLite 写锁互持会挂）；确定性的同点争用由上面锁层齐发
    # 覆盖，这里用配额=1 保证无论时序都不可能双行。
    outcomes: list[dict] = []

    def worker(user: str, code: str) -> None:
        cc = TestClient(app)
        hh = _auth(cc, user)
        rr = cc.post(
            "/api/batches",
            headers=hh,
            json={"chamber_id": ch["id"], "probe_id": pr["id"],
                  "cargo_ref": f"并发-{user}", "batch_code": code,
                  "temp_points": [{"t": "08:00", "c": -20.0}]},
        )
        outcomes.append({"user": user, "status": rr.status_code, "body": rr.text})

    t1 = threading.Thread(target=worker, args=("admin", "TB-CONT-A"))
    t2 = threading.Thread(target=worker, args=("ops", "TB-CONT-B"))
    t1.start(); t2.start()
    t1.join(30); t2.join(30)
    assert not t1.is_alive() and not t2.is_alive(), "双请求挂死（库锁缺陷）"

    statuses = sorted(o["status"] for o in outcomes)
    # 合法形态：[200,409] 同点争用 或 [200,200] 严格串行（后到在释放后进场）；
    # 双 409 不可能；任何 500/挂死都是并发缺陷。
    assert 500 not in statuses, outcomes
    assert statuses in ([200, 409], [200, 200]), outcomes
    # 核心硬约束：绝不双行、槽位绝不超 1
    assert _batch_count(ch["id"]) == 1
    assert _slot_from_db(ch["id"]) == 1
    if statuses == [200, 409]:
        loser = next(o for o in outcomes if o["status"] == 409)
        assert loser["user"] in ("admin", "ops")
    codes = {
        r["batch_code"]
        for r in c.get("/api/batches", headers=h_admin).json()["items"]
        if r["chamber_id"] == ch["id"]
    }
    assert codes in ({"TB-CONT-A"}, {"TB-CONT-B"})


# ---------- 护栏：五视图槽位同源，封批释放槽位 ----------

def test_five_views_slot_same_source_and_seal_decrements():
    c = TestClient(app)
    h = _auth(c)
    ch = _chamber(c, h, "CK-HN-03")
    probe = _probe(c, h, ch["id"], "PR-HN-C1")
    base = _slot_from_db(ch["id"])  # 种子草稿批占 1

    r = c.post(
        "/api/batches",
        headers=h,
        json={
            "chamber_id": ch["id"],
            "probe_id": probe["id"],
            "cargo_ref": "五视图同源货载",
            "batch_code": "TB-SLOT-V5-1",
            "temp_points": [{"t": "08:00", "c": 4.0}, {"t": "09:00", "c": 6.5}],
        },
    )
    assert r.status_code == 200, r.text
    expect = base + 1

    # 仓间视图
    v_chamber = c.get(f"/api/chambers/{ch['id']}", headers=h).json()
    assert v_chamber["active_batch_count"] == expect
    # 探头视图
    v_probe = c.get(f"/api/probes/{probe['id']}", headers=h).json()
    assert v_probe["active_batch_count"] == expect
    # 对账视图
    v_recon = c.get(f"/api/reconcile/{ch['chamber_code']}", headers=h).json()
    assert v_recon["active_batches"] == expect
    # 批次视图
    rows = c.get("/api/batches", headers=h).json()["items"]
    row = next(x for x in rows if x["batch_code"] == "TB-SLOT-V5-1")
    assert row["chamber_active_batch_count"] == expect

    # 封批闭环后，封批视图读到的槽位与其它视图一起回落
    bid = row["id"]
    ex = c.post("/api/excursions/open", headers=h,
                json={"batch_id": bid, "threshold": 6.0})
    assert ex.status_code == 200, ex.text
    cl = c.post("/api/excursions/close", headers=h,
                json={"cursor_id": ex.json()["id"], "note": "闭环"})
    assert cl.status_code == 200, cl.text
    seal = c.post("/api/seals", headers=h, json={"batch_id": bid})
    assert seal.status_code == 200, seal.text
    after = expect - 1

    seals = c.get("/api/seals", headers=h).json()["items"]
    srow = next(x for x in seals if x["batch_id"] == bid)
    assert srow["chamber_active_batch_count"] == after
    assert c.get(f"/api/chambers/{ch['id']}", headers=h).json()["active_batch_count"] == after
    assert c.get(f"/api/probes/{probe['id']}", headers=h).json()["active_batch_count"] == after
    assert c.get(f"/api/reconcile/{ch['chamber_code']}", headers=h).json()["active_batches"] == after
    brows = c.get("/api/batches", headers=h).json()["items"]
    brow = next(x for x in brows if x["batch_code"] == "TB-SLOT-V5-1")
    assert brow["chamber_active_batch_count"] == after
    # 五处与库内事实源完全同值
    assert _slot_from_db(ch["id"]) == after
