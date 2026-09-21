"""持久化：流程 JSON 文件（data/flows/*.json）+ 运行记录 SQLite（runs 表）。"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
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
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY, flow_id TEXT, flow_name TEXT,
            status TEXT, created_at TEXT, data TEXT)""")
        self.conn.commit()

    def append(self, result_dict: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?)",
            (result_dict["run_id"], result_dict["flow_id"], result_dict["flow_name"],
             result_dict["status"],
             result_dict.get("finished_at") or result_dict.get("started_at") or
             dt.datetime.now().isoformat(timespec="seconds"),
             json.dumps(result_dict, ensure_ascii=False)))
        self.conn.commit()

    def list(self, flow_id: str | None = None, limit: int = 30) -> list[dict]:
        sql = ("SELECT run_id, flow_id, flow_name, status, created_at FROM runs "
               + ("WHERE flow_id=? " if flow_id else "")
               + "ORDER BY created_at DESC LIMIT ?")
        rows = (self.conn.execute(sql, (flow_id, limit)).fetchall() if flow_id
                else self.conn.execute(sql, (limit,)).fetchall())
        return [dict(zip(["run_id", "flow_id", "flow_name", "status", "created_at"],
                         r)) for r in rows]

    def get(self, run_id: str) -> dict | None:
        row = self.conn.execute("SELECT data FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def new_run_id(self) -> str:
        return uuid.uuid4().hex[:12]
