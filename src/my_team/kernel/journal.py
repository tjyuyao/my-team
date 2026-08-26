"""Journal — 内核态设备：记录内核所见（到达 + outcome），SQLite 持久化。

记录口径 = 内核所见：每个被内核处理的事件（总线到达、内核态设备产出）
记一行，丢弃事件带原因。journal_record 由内核直接投递给本设备（不走
process_event，避免自指记录）。

第一版最小：只记录（memory_search 查询工具未实现）。
"""

import json
import sqlite3
import time

from my_team.kernel.process import VOID, KernelModeDevice

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS events ("
    " seq INTEGER PRIMARY KEY AUTOINCREMENT,"
    " ts TEXT, source TEXT, target TEXT, kind TEXT,"
    " payload TEXT, outcome TEXT, reason TEXT)"
)


class Journal(KernelModeDevice):
    def __init__(self, path: str):
        super().__init__("journal")
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(SCHEMA)
        self._db.commit()

    async def respond(self, event):
        payload = event["payload"]
        if payload.get("command") == "journal_record":
            e = payload.get("event") or {}
            self._db.execute(
                "INSERT INTO events (ts, source, target, kind, payload, outcome, reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    e.get("source"),
                    e.get("target"),
                    e.get("kind"),
                    json.dumps(e, ensure_ascii=False, default=str),
                    payload.get("outcome"),
                    payload.get("reason"),
                ),
            )
            self._db.commit()
        return VOID
