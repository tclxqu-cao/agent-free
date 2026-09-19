"""知识库：文档入库 → 分块 → SQLite FTS5 全文检索（BM25 排序）。

中文检索策略（零分词依赖）：入库与查询统一做「字符切分」——CJK 逐字、
英文/数字整词保留，再以空格拼接后交给 FTS5 unicode61 分词器，两端一致
即可命中。文档原文存 data/kb/<id>/docs/，向量仅存分块文本于 FTS 表。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import uuid
from pathlib import Path

_CHUNK_MAX = 600        # 单块目标上限（字符）
_LATIN = re.compile(r"[a-zA-Z0-9_+#@.]+")


def _cjk_runs(text: str):
    """切出 CJK/日文/韩文连续段（不含标点空白）。"""
    return re.finditer(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]+", text)


def _seg(text: str) -> str:
    """检索词形：英文/数字整词 + CJK 双字组（bigram）。
    bigram 是无分词器中文 FTS 的标准做法：单字太容易误命中，全词太脆。"""
    tokens: list[str] = []
    pos = 0
    for m in _cjk_runs(text):
        run = m.group(0)
        tokens.extend(_LATIN.findall(text[pos:m.start()]))
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
        pos = m.end()
    tokens.extend(_LATIN.findall(text[pos:]))
    return " ".join(tokens)


def _split_chunks(text: str) -> list[str]:
    """段落优先分块；超长段落按句子二次切分。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paras:
        pieces = [para]
        if len(para) > _CHUNK_MAX:
            sentences = re.split(r"(?<=[。！？!?；;])\s*", para)
            pieces, cur = [], ""
            for s in sentences:
                if cur and len(cur) + len(s) > _CHUNK_MAX:
                    pieces.append(cur)
                    cur = s
                else:
                    cur += s
            if cur.strip():
                pieces.append(cur)
        for piece in pieces:
            if buf and len(buf) + len(piece) + 2 > _CHUNK_MAX:
                chunks.append(buf)
                buf = piece
            else:
                buf = f"{buf}\n{piece}" if buf else piece
    if buf.strip():
        chunks.append(buf)
    return [c.strip() for c in chunks if c.strip()]


class KBStore:
    """知识库集合：kb 元数据 + 文档 + FTS5 分块索引，全部在一个 sqlite。"""

    def __init__(self, base: Path):
        self.base = Path(base)
        self.base.mkdir(parents=True, exist_ok=True)
        self.docs_dir = self.base / "docs"
        self.docs_dir.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(self.base / "kb.sqlite", check_same_thread=False)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS kbs (
            id TEXT PRIMARY KEY, name TEXT, description TEXT, created_at TEXT)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS docs (
            id TEXT PRIMARY KEY, kb_id TEXT, name TEXT, bytes INTEGER,
            chunks INTEGER, created_at TEXT)""")
        self.conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
            content, raw UNINDEXED, kb_id UNINDEXED, doc_id UNINDEXED,
            name UNINDEXED, tokenize='unicode61')""")
        self.conn.commit()

    # ---------------------------------------------------------------- 知识库
    def create_kb(self, kb_id: str, name: str, description: str = "") -> dict:
        kb_id = _safe_id(kb_id)
        try:
            self.conn.execute(
                "INSERT INTO kbs VALUES (?,?,?,?)",
                (kb_id, name or kb_id, description or "",
                 dt.datetime.now().isoformat(timespec="seconds")))
            self.conn.commit()
        except sqlite3.IntegrityError as e:
            raise ValueError(f"知识库 id 已存在：{kb_id}") from e
        return {"id": kb_id, "name": name or kb_id, "description": description or "",
                "docs": 0, "chunks": 0}

    def delete_kb(self, kb_id: str) -> bool:
        if not self.conn.execute("SELECT 1 FROM kbs WHERE id=?", (kb_id,)).fetchone():
            return False
        self.conn.execute("DELETE FROM chunks WHERE kb_id=?", (kb_id,))
        self.conn.execute("DELETE FROM docs WHERE kb_id=?", (kb_id,))
        self.conn.execute("DELETE FROM kbs WHERE id=?", (kb_id,))
        self.conn.commit()
        return True

    def list_kbs(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT k.id, k.name, k.description, COUNT(DISTINCT d.id),
                      COALESCE(SUM(d.chunks), 0)
               FROM kbs k LEFT JOIN docs d ON d.kb_id = k.id
               GROUP BY k.id ORDER BY k.created_at""").fetchall()
        return [{"id": r[0], "name": r[1], "description": r[2],
                 "docs": r[3], "chunks": r[4]} for r in rows]

    # ---------------------------------------------------------------- 文档
    def add_doc(self, kb_id: str, name: str, text: str) -> dict:
        if not self.conn.execute("SELECT 1 FROM kbs WHERE id=?", (kb_id,)).fetchone():
            raise ValueError(f"知识库不存在：{kb_id}")
        chunks = _split_chunks(text)
        if not chunks:
            raise ValueError("文档内容为空，无法入库")
        doc_id = uuid.uuid4().hex[:10]
        now = dt.datetime.now().isoformat(timespec="seconds")
        self.conn.execute("INSERT INTO docs VALUES (?,?,?,?,?,?)",
                          (doc_id, kb_id, name, len(text.encode("utf-8")),
                           len(chunks), now))
        self.conn.executemany(
            "INSERT INTO chunks (content, raw, kb_id, doc_id, name) VALUES (?,?,?,?,?)",
            [(_seg(c), c, kb_id, doc_id, name) for c in chunks])
        self.conn.commit()
        (self.docs_dir / f"{kb_id}__{doc_id}.json").write_text(json.dumps(
            {"doc_id": doc_id, "kb_id": kb_id, "name": name, "text": text,
             "created_at": now}, ensure_ascii=False), encoding="utf-8")
        return {"doc_id": doc_id, "kb_id": kb_id, "name": name, "chunks": len(chunks)}

    def delete_doc(self, kb_id: str, doc_id: str) -> bool:
        if not self.conn.execute(
                "SELECT 1 FROM docs WHERE kb_id=? AND id=?",
                (kb_id, doc_id)).fetchone():
            return False
        self.conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        self.conn.execute("DELETE FROM docs WHERE id=?", (doc_id,))
        self.conn.commit()
        (self.docs_dir / f"{kb_id}__{doc_id}.json").unlink(missing_ok=True)
        return True

    def list_docs(self, kb_id: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT id, name, bytes, chunks, created_at FROM docs
               WHERE kb_id=? ORDER BY created_at""", (kb_id,)).fetchall()
        return [{"doc_id": r[0], "name": r[1], "bytes": r[2],
                 "chunks": r[3], "created_at": r[4]} for r in rows]

    # ---------------------------------------------------------------- 检索
    def search(self, query: str, kb_ids: list[str] | None = None,
               top_k: int = 5) -> list[dict]:
        """逐 token（bigram/整词）查询聚合，得分 = Σ(-bm25)。
        语料为本地规模，多次 FTS 查询开销可忽略。"""
        tokens = list(dict.fromkeys(_seg(query).split()))[:24]
        if not tokens:
            return []
        extra, args_extra = "", []
        if kb_ids:
            extra = " AND kb_id IN (%s)" % ",".join("?" * len(kb_ids))
            args_extra = list(kb_ids)
        agg: dict[int, dict] = {}      # rowid → 聚合
        for tok in tokens:
            try:
                rows = self.conn.execute(
                    f"""SELECT rowid, raw, kb_id, doc_id, name,
                               bm25(chunks) FROM chunks
                        WHERE chunks MATCH ?{extra}""",
                    (tok, *args_extra)).fetchall()
            except sqlite3.OperationalError:   # 非法 token
                continue
            for rowid, raw, kb_id, doc_id, name, score in rows:
                item = agg.setdefault(rowid, {
                    "raw": raw, "kb_id": kb_id, "doc_id": doc_id,
                    "name": name, "score": 0.0, "hits": 0})
                item["hits"] += 1
                item["score"] += max(0.0, -score)
        picked = sorted(agg.values(), key=lambda i: (-i["hits"], -i["score"]))
        return [{"kb_id": i["kb_id"], "doc_id": i["doc_id"], "name": i["name"],
                 "text": i["raw"], "score": round(i["score"], 4)}
                for i in picked[:max(1, int(top_k))]]


def _safe_id(raw: str) -> str:
    s = "".join(c for c in str(raw or "").strip() if c.isalnum() or c in "-_")
    if not s:
        raise ValueError("id 只允许字母数字-_")
    return s
