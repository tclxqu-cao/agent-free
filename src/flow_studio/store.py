"""持久化：流程 JSON 文件（data/flows/*.json）+ 运行记录 SQLite（runs 表）。"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path

from .graph import FlowGraph, graph_from_dict, graph_to_dict


class FlowStore:
    def __init__(self, flows_dir: Path):
        self.dir = Path(flows_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, flow_id: str) -> Path:
        safe = "".join(c for c in flow_id if c.isalnum() or c in "-_")
        if not safe or safe != flow_id:
            raise ValueError(f"非法流程 id：{flow_id!r}（只允许字母数字-_）")
        return self.dir / f"{safe}.json"

    def list(self) -> list[FlowGraph]:
        out = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                out.append(graph_from_dict(json.loads(p.read_text(encoding="utf-8"))))
            except Exception:  # noqa: BLE001 单文件损坏不拖垮列表
                continue
        return out

    def get(self, flow_id: str) -> FlowGraph | None:
        p = self._path(flow_id)
        if not p.exists():
            return None
        return graph_from_dict(json.loads(p.read_text(encoding="utf-8")))

    def save(self, graph: FlowGraph) -> FlowGraph:
        # graph_from_dict 已在调用侧校验；此处确保 id 合法可写
        path = self._path(graph.id)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(graph_to_dict(graph), ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return graph

    def delete(self, flow_id: str) -> bool:
        p = self._path(flow_id)
        if p.exists():
            p.unlink()
            return True
        return False

    def seed_if_empty(self, flows: list[dict]) -> int:
        """流程库为空时写入内置流程，返回写入数。"""
        if any(self.dir.glob("*.json")):
            return 0
        n = 0
        for data in flows:
            self.save(graph_from_dict(data))
            n += 1
        return n

    def seed_missing(self, flows: list[dict]) -> int:
        """按 id 增量补种缺失的内置流程（绝不覆盖已有流程），返回写入数。"""
        n = 0
        for data in flows:
            fid = str(data.get("id") or "")
            if fid and self.get(fid) is None:
                self.save(graph_from_dict(data))
                n += 1
        return n


class RunStore:
    """运行记录：SQLite 单表，存 RunResult 序列化 JSON。"""

    def __init__(self, db_path: Path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY, flow_id TEXT, flow_name TEXT,
            status TEXT, created_at TEXT, data TEXT)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS run_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL, data TEXT NOT NULL)""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS run_events_cursor ON run_events(run_id, seq)")
        self.conn.commit()

    def append(self, result_dict: dict) -> None:
        with self._lock, self.conn:
            self._save(result_dict)

    def _save(self, result_dict: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?)",
            (result_dict["run_id"], result_dict["flow_id"], result_dict["flow_name"],
             result_dict["status"],
             result_dict.get("finished_at") or result_dict.get("started_at") or
             dt.datetime.now().isoformat(timespec="seconds"),
             json.dumps(result_dict, ensure_ascii=False)))

    def record(self, snapshot: dict, event: dict) -> None:
        """Commit the incremental snapshot and its event together."""
        event = {"timestamp": dt.datetime.now().isoformat(timespec="milliseconds"),
                 **event, "run_id": snapshot["run_id"]}
        with self._lock, self.conn:
            self._save(snapshot)
            self.conn.execute("INSERT INTO run_events(run_id, data) VALUES (?, ?)",
                              (snapshot["run_id"], json.dumps(event, ensure_ascii=False)))

    def events(self, run_id: str, after: int = 0, limit: int = 200) -> dict:
        limit = min(max(limit, 1), 500)
        with self._lock:
            rows = self.conn.execute(
                "SELECT seq, data FROM run_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                (run_id, max(after, 0), limit + 1)).fetchall()
            events = [{**json.loads(data), "seq": seq} for seq, data in rows[:limit]]
            return {"events": events, "next_seq": events[-1]["seq"] if events else after,
                    "has_more": len(rows) > limit, "run": self.get(run_id)}

    def interrupt_pending(self) -> None:
        """A new runtime cannot resume the previous process's Python call stack."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT data FROM runs WHERE status IN ('queued', 'running')").fetchall()
            for (data,) in rows:
                run = json.loads(data)
                run.update(status="interrupted", error="服务重启，执行已中断",
                           finished_at=dt.datetime.now().isoformat(timespec="seconds"))
                for node in run.get("node_runs", []):
                    if node.get("status") == "running":
                        node.update(status="failed", error=run["error"])
                self.record(run, {"type": "run.finished", "level": "error",
                                  "message": run["error"]})

    def list(self, flow_id: str | None = None, limit: int = 30) -> list[dict]:
        sql = ("SELECT run_id, flow_id, flow_name, status, created_at FROM runs "
               + ("WHERE flow_id=? " if flow_id else "")
               + "ORDER BY created_at DESC LIMIT ?")
        with self._lock:
            rows = (self.conn.execute(sql, (flow_id, limit)).fetchall() if flow_id
                    else self.conn.execute(sql, (limit,)).fetchall())
        return [dict(zip(["run_id", "flow_id", "flow_name", "status", "created_at"],
                         r)) for r in rows]

    def get(self, run_id: str) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT data FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def new_run_id(self) -> str:
        return uuid.uuid4().hex[:12]
