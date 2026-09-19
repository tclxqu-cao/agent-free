"""记忆：带作用域的键值存储（global / agent:<id> / session:<id>）。

流程的记忆节点与智能体的记忆工具共用一份 sqlite；值必须 JSON 兼容，
读出即得原类型（dict/list/number…），模板渲染时自动转字符串。
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path


class MemoryStore:
    def __init__(self, db_path: Path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS memory (
            scope TEXT, key TEXT, value TEXT, updated_at TEXT,
            PRIMARY KEY (scope, key))""")
        self.conn.commit()

    @staticmethod
    def normalize_scope(scope: str) -> str:
        s = str(scope or "global").strip() or "global"
        return s

    def set(self, scope: str, key: str, value) -> dict:
        scope, key = self.normalize_scope(scope), str(key or "").strip()
        if not key:
            raise ValueError("记忆 key 不能为空")
        if value is None or isinstance(value, (str, int, float, bool, dict, list)):
            pass
        else:
            value = str(value)
        self.conn.execute(
            "INSERT OR REPLACE INTO memory VALUES (?,?,?,?)",
            (scope, key, json.dumps(value, ensure_ascii=False),
             dt.datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()
        return {"scope": scope, "key": key, "value": value, "ok": True}

    def get(self, scope: str, key: str):
        row = self.conn.execute(
            "SELECT value FROM memory WHERE scope=? AND key=?",
            (self.normalize_scope(scope), str(key or "").strip())).fetchone()
        return json.loads(row[0]) if row else None

    def delete(self, scope: str, key: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM memory WHERE scope=? AND key=?",
            (self.normalize_scope(scope), str(key or "").strip()))
        self.conn.commit()
        return cur.rowcount > 0

    def list(self, scope: str | None = None, q: str | None = None,
             limit: int = 200) -> list[dict]:
        sql = "SELECT scope, key, value, updated_at FROM memory WHERE 1=1"
        args: list = []
        if scope:
            sql += " AND scope=?"
            args.append(self.normalize_scope(scope))
        if q:
            sql += " AND (key LIKE ? OR value LIKE ?)"
            args += [f"%{q}%", f"%{q}%"]
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(min(int(limit), 1000))
        out = []
        for r in self.conn.execute(sql, args).fetchall():
            try:
                val = json.loads(r[2])
            except Exception:  # noqa: BLE001
                val = r[2]
            out.append({"scope": r[0], "key": r[1], "value": val,
                        "updated_at": r[3]})
        return out

    def scopes(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT scope, COUNT(*), MAX(updated_at) FROM memory
               GROUP BY scope ORDER BY scope""").fetchall()
        return [{"scope": r[0], "count": r[1], "updated_at": r[2]} for r in rows]
