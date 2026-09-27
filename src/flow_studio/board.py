"""共享看板：数字员工的唯一事实来源（任务卡、状态机、依赖与迁移历史）。

卡片迁移与消息是两件事：迁移只改状态，说了什么走 bus。驱动器负责成对写入。
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import uuid
from pathlib import Path

from .bus import Hub

COLUMNS = ("backlog", "doing", "review", "done", "blocked")
TRANSITIONS: dict[str, set[str]] = {
    "backlog": {"doing", "blocked"},
    "doing": {"backlog", "review", "done", "blocked"},
    "review": {"doing", "done", "blocked"},
    "blocked": {"doing", "backlog"},
    "done": {"review", "doing"},
}

_COLUMNS = ("id", "workspace_id", "team_id", "title", "detail", "role", "status",
            "priority", "depends_on", "output", "created_by", "created_at",
            "updated_at")


class BoardError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _row_to_card(row: sqlite3.Row) -> dict:
    card = {key: row[key] for key in _COLUMNS}
    card["depends_on"] = json.loads(card["depends_on"] or "[]")
    card["output"] = json.loads(card["output"] or "{}")
    return card


class KanbanBoard:
    def __init__(self, db_path: Path, hub: Hub | None = None):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.RLock()
        self.conn.execute("""CREATE TABLE IF NOT EXISTS cards (
            id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, team_id TEXT NOT NULL,
            title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'backlog',
            priority INTEGER NOT NULL DEFAULT 0, depends_on TEXT NOT NULL DEFAULT '[]',
            output TEXT NOT NULL DEFAULT '{}', created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS card_moves (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, team_id TEXT NOT NULL,
            card_id TEXT NOT NULL, from_status TEXT NOT NULL, to_status TEXT NOT NULL,
            actor TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL)""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_card_team ON cards(team_id)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_move_team ON card_moves(team_id, seq)")
        self.conn.commit()
        self.hub = hub or Hub()

    # ------------------------------------------------------------------ 写入
    def add(self, *, workspace_id: str, team_id: str, title: str, role: str = "",
            detail: str = "", depends_on: list[str] | None = None, priority: int = 0,
            created_by: str = "") -> dict:
        title = str(title or "").strip()
        if not title:
            raise BoardError("card_title_required", "卡片标题不能为空")
        depends = [str(d) for d in (depends_on or []) if str(d).strip()]
        card_id = uuid.uuid4().hex[:12]
        now = _now()
        with self.lock:
            self.conn.execute(
                "INSERT INTO cards(id, workspace_id, team_id, title, detail, role,"
                " status, priority, depends_on, output, created_by, created_at,"
                " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (card_id, workspace_id, team_id, title[:200], str(detail or ""),
                 str(role or ""), "backlog", int(priority),
                 json.dumps(depends, ensure_ascii=False), "{}", created_by, now, now))
            self.conn.execute(
                "INSERT INTO card_moves(team_id, card_id, from_status, to_status,"
                " actor, reason, created_at) VALUES (?,?,?,?,?,?,?)",
                (team_id, card_id, "", "backlog", created_by or "system", "建卡", now))
            self.conn.commit()
        card = self.get(card_id)
        self._publish(card, "created")
        return card

    def update(self, card_id: str, actor: str, *, title: str | None = None,
               detail: str | None = None, role: str | None = None,
               depends_on: list[str] | None = None,
               priority: int | None = None) -> dict:
        card = self._require(card_id)
        fields: list[str] = []
        args: list = []
        if title is not None:
            if not str(title).strip():
                raise BoardError("card_title_required", "卡片标题不能为空")
            fields.append("title=?")
            args.append(str(title).strip()[:200])
        if detail is not None:
            fields.append("detail=?")
            args.append(str(detail))
        if role is not None:
            fields.append("role=?")
            args.append(str(role))
        if priority is not None:
            fields.append("priority=?")
            args.append(int(priority))
        if depends_on is not None:
            for dep in depends_on:
                if str(dep) == card_id:
                    raise BoardError("card_self_dependency", "卡片不能依赖自身")
                if self.get(str(dep)) is None:
                    raise BoardError("card_dependency_missing",
                                     f"依赖的卡片不存在：{dep}")
            fields.append("depends_on=?")
            args.append(json.dumps([str(d) for d in depends_on], ensure_ascii=False))
        if not fields:
            return card
        fields.append("updated_at=?")
        args += [_now(), card_id]
        with self.lock:
            self.conn.execute(
                f"UPDATE cards SET {','.join(fields)} WHERE id=?", args)
            self.conn.commit()
        updated = self.get(card_id)
        self._publish(updated, "updated")
        return updated

    def move(self, card_id: str, to_status: str, actor: str, reason: str = "") -> dict:
        card = self._require(card_id)
        if to_status not in COLUMNS:
            raise BoardError("card_status_invalid", f"未知看板列：{to_status}")
        current = card["status"]
        if to_status == current:
            return card
        if to_status not in TRANSITIONS[current]:
            raise BoardError(
                "card_transition_denied",
                f"不允许的迁移：{current} → {to_status}")
        if to_status == "doing":
            blockers = [dep for dep in card["depends_on"]
                        if (self.get(dep) or {}).get("status") != "done"]
            if blockers:
                raise BoardError(
                    "card_dependency_blocked",
                    "依赖未完成，无法开工：" + "、".join(blockers))
        now = _now()
        with self.lock:
            self.conn.execute(
                "UPDATE cards SET status=?, updated_at=? WHERE id=?",
                (to_status, now, card_id))
            self.conn.execute(
                "INSERT INTO card_moves(team_id, card_id, from_status, to_status,"
                " actor, reason, created_at) VALUES (?,?,?,?,?,?,?)",
                (card["team_id"], card_id, current, to_status, actor,
                 str(reason or "")[:300], now))
            self.conn.commit()
        moved = self.get(card_id)
        self._publish(moved, "moved", {"from": current, "to": to_status,
                                       "reason": str(reason or "")})
        return moved

    def set_output(self, card_id: str, actor: str, output: dict) -> dict:
        """员工产出写入卡片（合并，不覆盖他人字段）。"""
        card = self._require(card_id)
        merged = {**card["output"], **(output or {}), "updated_by": actor,
                  "updated_at": _now()}
        with self.lock:
            self.conn.execute("UPDATE cards SET output=?, updated_at=? WHERE id=?",
                              (json.dumps(merged, ensure_ascii=False), _now(), card_id))
            self.conn.commit()
        updated = self.get(card_id)
        self._publish(updated, "output")
        return updated

    def comment(self, card_id: str, actor: str, text: str) -> dict:
        card = self._require(card_id)
        notes = list(card["output"].get("notes") or [])
        entry = {"actor": actor, "text": str(text or "").strip()[:4000],
                 "at": _now()}
        if not entry["text"]:
            raise BoardError("card_comment_empty", "评论内容为空")
        notes.append(entry)
        with self.lock:
            self.conn.execute("UPDATE cards SET output=?, updated_at=? WHERE id=?",
                              (json.dumps({**card["output"], "notes": notes},
                                          ensure_ascii=False), _now(), card_id))
            self.conn.commit()
        updated = self.get(card_id)
        self._publish(updated, "comment", {"note": entry})
        return updated

    def delete(self, card_id: str) -> bool:
        card = self.get(card_id)
        if card is None:
            return False
        with self.lock:
            self.conn.execute("DELETE FROM cards WHERE id=?", (card_id,))
            self.conn.execute("DELETE FROM card_moves WHERE card_id=?", (card_id,))
            self.conn.commit()
        self._publish(card, "deleted")
        return True

    # ------------------------------------------------------------------ 读取
    def get(self, card_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM cards WHERE id=?", (str(card_id),)).fetchone()
        return _row_to_card(row) if row else None

    def list(self, team_id: str, *, status: str | None = None,
             role: str | None = None) -> list[dict]:
        sql = "SELECT * FROM cards WHERE team_id=?"
        args: list = [team_id]
        if status:
            sql += " AND status=?"
            args.append(status)
        if role:
            sql += " AND role=?"
            args.append(role)
        sql += " ORDER BY priority DESC, created_at"
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [_row_to_card(r) for r in rows]

    def history(self, team_id: str, card_id: str | None = None,
                limit: int = 200) -> list[dict]:
        sql = ("SELECT * FROM card_moves WHERE team_id=?"
               + (" AND card_id=?" if card_id else "")
               + " ORDER BY seq DESC LIMIT ?")
        args: list = [team_id] + ([card_id] if card_id else []) + [
            max(1, min(500, int(limit)))]
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [{"seq": r["seq"], "team_id": r["team_id"], "card_id": r["card_id"],
                 "from": r["from_status"], "to": r["to_status"], "actor": r["actor"],
                 "reason": r["reason"], "created_at": r["created_at"]}
                for r in reversed(rows)]

    def progress(self, team_id: str) -> dict:
        cards = self.list(team_id)
        by_status = {col: 0 for col in COLUMNS}
        for card in cards:
            by_status[card["status"]] = by_status.get(card["status"], 0) + 1
        total = len(cards)
        return {"total": total, "by_status": by_status,
                "done_ratio": round(by_status["done"] / total, 3) if total else 0.0,
                "roles_open": sorted({c["role"] for c in cards
                                      if c["status"] not in ("done",)})}

    def _require(self, card_id: str) -> dict:
        card = self.get(card_id)
        if card is None:
            raise BoardError("card_not_found", f"卡片不存在：{card_id}")
        return card

    def _publish(self, card: dict | None, action: str,
                 extra: dict | None = None) -> None:
        if card is None:
            return
        self.hub.publish({"type": "card", "workspace_id": card["workspace_id"],
                          "team_id": card["team_id"], "id": card["id"],
                          "payload": {"action": action, "card": card,
                                      **(extra or {})}})
