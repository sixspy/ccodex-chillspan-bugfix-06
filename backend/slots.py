"""仓间槽位同步 —— 唯一事实源与口径（当班封死，禁止折中）。

槽位同步说明（本说明即纪要，写入即生效，任何偏离按“槽位偏航”拒写）：

1. 甲/乙分叉择一，两边不得混用：
   - 甲路径（本仓唯一启用）：正式批次落库与仓间槽位占用同事务。
     temp_batches 正式行是槽位的唯一事实源；批次 INSERT 成功即占用抬一，
     事务内任何一步失败则整单回滚，槽位与批次不会出现“批在表、槽位空”。
   - 乙路径（本仓未启用）：排产条先落影子副本，正式占用零改；配额复核后
     原子并入，否则副本作废。本仓不允许影子行/半成品行落 temp_batches，
     也不允许用 route_plans 排产条顶替正式占用。
2. 禁止双写：不另立可漂移的占用计数器（双写必然出现批在表、槽位空）。
   所有读口一律经本模块 COUNT 正式行，五处视图天然同源。
3. 禁止只调色仓间视图环：读口不得对事实值做 +/- 修饰。
4. 并发：两名调度同仓间建批须串行，先拿仓间锁者写；后到只记 contention_log
   争用痕，不插第二行。
5. 占用口径：status NOT IN ('已封批','作废') 的 temp_batches 行数。
"""
from __future__ import annotations

from fastapi import HTTPException

OCCUPIED_EXCLUDE_STATUSES = ("已封批", "作废")


def count_active_slots(conn, chamber_id: int) -> int:
    """仓间当前占用槽位（唯一事实源，五个视图共用此函数）。"""
    placeholders = ",".join("?" for _ in OCCUPIED_EXCLUDE_STATUSES)
    row = conn.execute(
        f"""SELECT COUNT(*) c FROM temp_batches
            WHERE chamber_id=? AND status NOT IN ({placeholders})""",
        (chamber_id, *OCCUPIED_EXCLUDE_STATUSES),
    ).fetchone()
    return int(row["c"])


def quota_left(conn, chamber) -> int:
    return int(chamber["quota_batches"]) - count_active_slots(conn, chamber["id"])


def ensure_slot_available(conn, chamber) -> int:
    """甲路径配额复核：满额即拒写（409），由外层事务保证全单回滚。"""
    occupied = count_active_slots(conn, chamber["id"])
    if occupied >= int(chamber["quota_batches"]):
        raise HTTPException(409, "仓间在批配额已满，拒绝建批（槽位偏航拒写）")
    return occupied
