"""数字员工消息总线：SQLite 持久化的任务消息、线程与未读，配合进程内 Hub 做 SSE 推送。

与看板的边界：看板是状态（谁在做什么、到哪一步），总线是过程（说了什么）。
`seq` 单调递增，同时用作 SSE event id，支持 Last-Event-ID 断线重放。
"""

from __future__ import annotations

import datetime as dt
import json
import queue
import sqlite3
import threading
from pathlib import Path

MESSAGE_KINDS = ("assign", "request", "response", "review", "deliver", "announce", "human")
HUMAN_ACTOR = "human"
BROADCAST = "*"
REPLAY_LIMIT = 500
SUBSCRIBER_QUEUE = 256

_COLUMNS = ("seq", "workspace_id", "team_id", "from_actor", "to_actor", "kind",
            "subject", "body", "parent_seq", "thread_id", "round_no", "created_at")


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class Hub:
    """进程内事件广播；队列满时下发 overflow 事件，由客户端回退到快照刷新。"""

    def __init__(self) -> None:
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        sub: queue.Queue = queue.Queue(maxsize=SUBSCRIBER_QUEUE)
        with self._lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: queue.Queue) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def publish(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            try:
                sub.put_nowait(event)
            except queue.Full:
                # 队列满时先挤掉最旧的一条，overflow 信号才可能送得出去；
                # 否则慢客户端只会静默丢事件，永远收不到「去刷新快照」的提示
                try:
                    sub.get_nowait()
                    sub.put_nowait({"type": "overflow",
                                    "reason": "subscriber_lagged"})
                except (queue.Empty, queue.Full):
                    continue


def _row_to_message(row: sqlite3.Row) -> dict:
    data = {key: row[key] for key in _COLUMNS}
    data["body"] = json.loads(data["body"] or "{}")
    return data


class MessageBus:
    def __init__(self, db_path: Path, hub: Hub | None = None):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.RLock()
        self.conn.execute("""CREATE TABLE IF NOT EXISTS messages (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id TEXT NOT NULL,
            team_id TEXT NOT NULL,
            from_actor TEXT NOT NULL,
            to_actor TEXT NOT NULL,
            kind TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '{}',
            parent_seq INTEGER,
            thread_id INTEGER,
            round_no INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS reads (
            message_seq INTEGER NOT NULL, actor TEXT NOT NULL, read_at TEXT NOT NULL,
            PRIMARY KEY (message_seq, actor))""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_team ON messages(team_id, seq)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_msg_to ON messages(team_id, to_actor, kind)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_msg_thread ON messages(thread_id, seq)")
        self.conn.commit()
        self.hub = hub or Hub()

    # ------------------------------------------------------------------ 写入
    def post(self, *, workspace_id: str, team_id: str, from_actor: str,
             to_actor: str, kind: str, subject: str, body: dict | None = None,
             parent_seq: int | None = None, round_no: int = 0) -> dict:
        if kind not in MESSAGE_KINDS:
            raise ValueError(f"未知消息类型：{kind}")
        from_actor = str(from_actor or "").strip()
        to_actor = str(to_actor or "").strip() or BROADCAST
        subject = str(subject or "").strip()
        if not from_actor:
            raise ValueError("缺少发件人")
        if not subject:
            raise ValueError("消息主题不能为空")
        payload = body if isinstance(body, dict) else {"text": str(body or "")}
        thread_id = None
        if parent_seq is not None:
            parent = self.get(int(parent_seq))
            if parent is None:
                raise ValueError(f"回复的消息不存在：{parent_seq}")
            if parent["team_id"] != team_id:
                raise ValueError("不能回复其他团队的消息")
            thread_id = parent["thread_id"] or parent["seq"]
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO messages(workspace_id, team_id, from_actor, to_actor,"
                " kind, subject, body, parent_seq, thread_id, round_no, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (workspace_id, team_id, from_actor, to_actor, kind, subject[:200],
                 json.dumps(payload, ensure_ascii=False), parent_seq, thread_id,
                 round_no, _now()))
            seq = int(cur.lastrowid)
            self.conn.commit()
        message = self.get(seq)
        self.hub.publish({"type": "message", "workspace_id": workspace_id,
                          "team_id": team_id, "id": seq, "payload": message})
        return message

    def mark_read(self, seq: int, actor: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO reads(message_seq, actor, read_at) VALUES (?,?,?)",
                (int(seq), str(actor), _now()))
            self.conn.commit()

    # 「未读」只有一个口径：发给本人或全体广播、非本人所写、且没标记过已读。
    # unread 与 mark_read_inbox 必须共用它，否则广播消息会清不掉，节点角标永远亮着。
    _UNREAD_SQL = (" WHERE m.team_id=? AND m.to_actor IN (?,?)"
                   " AND m.from_actor<>? AND NOT EXISTS"
                   " (SELECT 1 FROM reads r WHERE r.message_seq=m.seq AND r.actor=?)")

    def unread_seqs(self, team_id: str, actor: str) -> list[int]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT m.seq FROM messages m" + self._UNREAD_SQL + " ORDER BY m.seq",
                (team_id, actor, BROADCAST, actor, actor)).fetchall()
        return [int(r["seq"]) for r in rows]

    def mark_read_inbox(self, team_id: str, actor: str) -> int:
        for seq in self.unread_seqs(team_id, actor):
            self.mark_read(seq, actor)
        return self.unread(team_id, actor)

    # ------------------------------------------------------------------ 读取
    def get(self, seq: int) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM messages WHERE seq=?", (int(seq),)).fetchone()
        return _row_to_message(row) if row else None

    def list(self, team_id: str, *, limit: int = 200, after_seq: int = 0,
             kind: str | None = None, actor: str | None = None) -> list[dict]:
        sql = "SELECT * FROM messages WHERE team_id=? AND seq>?"
        args: list = [team_id, int(after_seq)]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if actor:
            sql += " AND (to_actor IN (?,?) OR from_actor=?)"
            args += [actor, BROADCAST, actor]
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(max(1, min(500, int(limit))))
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [_row_to_message(r) for r in reversed(rows)]

    def thread(self, thread_id: int) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE thread_id=? OR seq=?"
                " ORDER BY seq", (int(thread_id), int(thread_id))).fetchall()
        return [_row_to_message(r) for r in rows]

    def inbox(self, team_id: str, actor: str, *, unread_only: bool = False,
              limit: int = 50) -> list[dict]:
        sql = ("SELECT * FROM messages m WHERE m.team_id=?"
               " AND m.to_actor IN (?,?) AND m.from_actor<>?")
        args: list = [team_id, actor, BROADCAST, actor]
        if unread_only:
            sql += (" AND NOT EXISTS (SELECT 1 FROM reads r"
                    " WHERE r.message_seq=m.seq AND r.actor=?)")
            args.append(actor)
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(max(1, min(200, int(limit))))
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [_row_to_message(r) for r in reversed(rows)]

    def unread(self, team_id: str, actor: str) -> int:
        return len(self.unread_seqs(team_id, actor))

    def pairs(self, team_id: str) -> list[dict]:
        """组织图连线权重：[{from, to, count}]。"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT from_actor, to_actor, COUNT(*) AS n FROM messages"
                " WHERE team_id=? GROUP BY from_actor, to_actor ORDER BY n DESC",
                (team_id,)).fetchall()
        return [{"from": r["from_actor"], "to": r["to_actor"],
                 "count": int(r["n"])} for r in rows]

    def stats(self, team_id: str) -> dict:
        with self.lock:
            total = self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE team_id=?", (team_id,)).fetchone()[0]
            by_kind = {r["kind"]: int(r["n"]) for r in self.conn.execute(
                "SELECT kind, COUNT(*) AS n FROM messages WHERE team_id=?"
                " GROUP BY kind", (team_id,)).fetchall()}
        return {"total": int(total), "by_kind": by_kind}

    def recent(self, *, workspace_id: str, after_seq: int = 0,
               limit: int = REPLAY_LIMIT) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE workspace_id=? AND seq>?"
                " ORDER BY seq LIMIT ?",
                (workspace_id, int(after_seq),
                 max(1, min(REPLAY_LIMIT, int(limit))))).fetchall()
        return [_row_to_message(r) for r in rows]
