"""素材库：图片 / 视频 / 音频等媒体资产的元数据（SQLite）与文件落盘。

目录布局（与 flows 同级）：
    data/media/assets.sqlite   元数据
    data/media/files/<id>.<ext>  实体文件（id 作文件名，杜绝路径拼接注入）
"""

from __future__ import annotations

import datetime as dt
import mimetypes
import sqlite3
import threading
import uuid
from pathlib import Path

KINDS = ("image", "video", "audio", "file")


class AssetStore:
    def __init__(self, media_dir: Path):
        self.dir = Path(media_dir)
        self.files = self.dir / "files"
        self.files.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.dir / "assets.sqlite", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE IF NOT EXISTS assets (
            id TEXT PRIMARY KEY, kind TEXT, name TEXT, path TEXT,
            mime TEXT, bytes INTEGER, flow_id TEXT, meta TEXT, created_at TEXT)""")
        self.conn.commit()

    # ---------------------------------------------------------------- 写入
    def add_bytes(self, data: bytes, ext: str, kind: str = "file", name: str = "",
                  flow_id: str = "", meta: dict | None = None) -> dict:
        if kind not in KINDS:
            kind = "file"
        aid = uuid.uuid4().hex[:12]
        ext = (ext or ".bin").lstrip(".")
        safe_ext = "".join(c for c in ext if c.isalnum())[:8] or "bin"
        fname = f"{aid}.{safe_ext}"
        (self.files / fname).write_bytes(data)
        row = {"id": aid, "kind": kind, "name": name or fname,
               "path": fname, "mime": mimetypes.guess_type(fname)[0] or "application/octet-stream",
               "bytes": len(data), "flow_id": flow_id,
               "meta": meta or {}, "created_at": dt.datetime.now().isoformat(timespec="seconds")}
        self._insert(row)
        return self.to_dict(row)

    def add_file(self, src: Path, kind: str = "file", name: str = "",
                 flow_id: str = "", meta: dict | None = None) -> dict:
        data = Path(src).read_bytes()
        return self.add_bytes(data, Path(src).suffix, kind, name, flow_id, meta)

    def _insert(self, row: dict) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?)",
                (row["id"], row["kind"], row["name"], row["path"], row["mime"],
                 row["bytes"], row["flow_id"],
                 __import__("json").dumps(row["meta"], ensure_ascii=False), row["created_at"]))
            self.conn.commit()

    # ---------------------------------------------------------------- 查询
    def list(self, kind: str | None = None, flow_id: str | None = None,
             limit: int = 200) -> list[dict]:
        sql, args = "SELECT * FROM assets WHERE 1=1", []
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if flow_id:
            sql += " AND flow_id=?"
            args.append(flow_id)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(min(limit, 1000))
        with self._lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [self.to_dict(r) for r in rows]

    def get(self, asset_id: str) -> dict | None:
        with self._lock:
            r = self.conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        return self.to_dict(r) if r else None

    def file_path(self, asset_id_or_name: str) -> Path | None:
        """按 id 或文件名解析实体文件；只允许最终文件名（防路径穿越）。"""
        if "/" in asset_id_or_name or "\\" in asset_id_or_name or ".." in asset_id_or_name:
            return None
        direct = self.files / Path(asset_id_or_name).name
        if direct.is_file():
            return direct
        rec = self.get(asset_id_or_name)
        fname = rec["path"] if rec else Path(asset_id_or_name).name
        if "/" in fname or ".." in fname:
            return None
        p = self.files / fname
        return p if p.exists() else None

    def delete(self, asset_id: str) -> bool:
        with self._lock:
            rec = self.get(asset_id)
            if rec is None:
                return False
            (self.files / rec["path"]).unlink(missing_ok=True)
            self.conn.execute("DELETE FROM assets WHERE id=?", (asset_id,))
            self.conn.commit()
            return True

    # ---------------------------------------------------------------- 序列化
    @staticmethod
    def to_dict(row) -> dict:
        import json

        if isinstance(row, dict):
            return {**row, "meta": row.get("meta") or {},
                    "url": f"/media/files/{row['path']}"}
        d = dict(row)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except Exception:  # noqa: BLE001
            d["meta"] = {}
        d["url"] = f"/media/files/{d['path']}"
        return d
