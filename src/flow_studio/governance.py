"""Flow Studio identity, tenancy, release metadata, and audit persistence."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


SESSION_COOKIE = "flow_studio_session"
SESSION_HOURS = 12
ROLES = ("owner", "admin", "editor", "viewer")
RESOURCE_TYPES = ("agent", "flow")

ROLE_CAPABILITIES = {
    "viewer": {"resource.read", "runtime.execute"},
    "editor": {"resource.read", "runtime.execute", "resource.write",
               "runtime.preview", "eval.execute"},
    "admin": {"resource.read", "runtime.execute", "resource.write",
              "runtime.preview", "eval.execute", "release.approve",
              "release.publish", "release.rollback", "member.manage",
              "audit.read"},
    "owner": {"*"},
}

DEFAULT_POLICY = {
    "allowed_models": [],
    "denied_tools": [],
    "denied_mcp_servers": [],
    "allowed_flow_node_types": [],
    "max_agent_steps": 12,
    "allowed_http_hosts": [],
    "require_approval": True,
    "required_eval_suite_id": "",
    "min_eval_pass_rate": 1.0,
}

_SECRET_KEYS = {
    "password", "password_hash", "session", "session_id", "session_token",
    "csrf", "csrf_token", "cookie", "authorization", "api_key", "secret_key",
    "token",
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _safe_id(value: str, label: str = "id") -> str:
    value = str(value or "").strip()
    if not value or any(not (c.isalnum() or c in "-_") for c in value):
        raise GovernanceError("invalid_id", f"{label} 只允许字母数字-_", 422)
    return value


def sanitize_details(value):
    """Remove secrets recursively before structured values enter the audit log."""
    if isinstance(value, dict):
        return {
            str(k): ("[REDACTED]" if str(k).lower() in _SECRET_KEYS
                     else sanitize_details(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [sanitize_details(v) for v in value]
    if isinstance(value, tuple):
        return [sanitize_details(v) for v in value]
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + "..."
    return value


class GovernanceError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400,
                 details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    display_name: str
    workspace_id: str
    workspace_name: str
    role: str
    session_id: str
    csrf_token: str
    request_id: str

    def can(self, capability: str) -> bool:
        caps = ROLE_CAPABILITIES.get(self.role, set())
        return "*" in caps or capability in caps


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    if not password:
        raise GovernanceError("invalid_password", "密码不能为空", 422)
    salt = salt or secrets.token_bytes(16)
    n, r, p = 2**14, 8, 1
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, n, r, p, salt_hex, digest_hex = encoded.split("$", 5)
        if algo != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode("utf-8"),
                                salt=bytes.fromhex(salt_hex), n=int(n),
                                r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(actual, bytes.fromhex(digest_hex))
    except (ValueError, TypeError):
        return False


class GovernanceStore:
    """Transactional governance database for one Flow Studio instance."""

    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._create_schema()

    def _create_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          user_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
          display_name TEXT NOT NULL, password_hash TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS workspaces (
          workspace_id TEXT PRIMARY KEY, name TEXT NOT NULL,
          created_by TEXT NOT NULL REFERENCES users(user_id), created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS members (
          workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
          user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
          role TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY (workspace_id, user_id));
        CREATE TABLE IF NOT EXISTS sessions (
          session_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
          user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
          csrf_token TEXT NOT NULL, created_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL, expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS login_attempts (
          username TEXT NOT NULL, ip_address TEXT NOT NULL,
          failures INTEGER NOT NULL, window_started TEXT NOT NULL,
          locked_until TEXT, PRIMARY KEY (username, ip_address));
        CREATE TABLE IF NOT EXISTS policies (
          workspace_id TEXT PRIMARY KEY REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
          policy_json TEXT NOT NULL, updated_by TEXT NOT NULL,
          updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS resource_versions (
          version_id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
          resource_type TEXT NOT NULL, resource_id TEXT NOT NULL,
          version_no INTEGER NOT NULL, status TEXT NOT NULL,
          action TEXT NOT NULL, snapshot_json TEXT, content_hash TEXT NOT NULL,
          created_by TEXT NOT NULL REFERENCES users(user_id), created_at TEXT NOT NULL,
          submitted_by TEXT, submitted_at TEXT,
          approved_by TEXT, approved_at TEXT,
          rejected_by TEXT, rejected_at TEXT,
          published_by TEXT, published_at TEXT,
          reason TEXT NOT NULL DEFAULT '', rollback_of TEXT,
          UNIQUE (workspace_id, resource_type, resource_id, version_no));
        CREATE INDEX IF NOT EXISTS idx_versions_resource
          ON resource_versions(workspace_id, resource_type, resource_id, version_no DESC);
        CREATE INDEX IF NOT EXISTS idx_versions_status
          ON resource_versions(workspace_id, status, created_at DESC);
        CREATE TABLE IF NOT EXISTS resource_releases (
          workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
          resource_type TEXT NOT NULL, resource_id TEXT NOT NULL,
          version_id TEXT NOT NULL REFERENCES resource_versions(version_id),
          updated_at TEXT NOT NULL,
          PRIMARY KEY (workspace_id, resource_type, resource_id));
        CREATE TABLE IF NOT EXISTS audit_events (
          event_id TEXT PRIMARY KEY, workspace_id TEXT, user_id TEXT,
          username TEXT, event_type TEXT NOT NULL, resource_type TEXT,
          resource_id TEXT, version_no INTEGER, request_id TEXT NOT NULL,
          outcome TEXT NOT NULL, ip_address TEXT,
          details_json TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_audit_workspace
          ON audit_events(workspace_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS migrations (
          migration_key TEXT PRIMARY KEY, applied_at TEXT NOT NULL,
          details_json TEXT NOT NULL);
        """)
        self.conn.commit()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                yield self.conn
            except Exception:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    def is_initialized(self) -> bool:
        return self.conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def setup_owner(self, username: str, password: str,
                    display_name: str = "") -> dict:
        username = _safe_id(username.lower(), "用户名")
        created = _now()
        user_id = uuid.uuid4().hex
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise GovernanceError("already_initialized", "实例已经初始化", 409)
            conn.execute(
                "INSERT INTO users VALUES (?,?,?,?,?,?)",
                (user_id, username, display_name.strip() or username,
                 hash_password(password), 1, created))
            conn.execute(
                "INSERT INTO workspaces VALUES (?,?,?,?)",
                ("default", "Default", user_id, created))
            conn.execute(
                "INSERT INTO members VALUES (?,?,?,?)",
                ("default", user_id, "owner", created))
            conn.execute(
                "INSERT INTO policies VALUES (?,?,?,?)",
                ("default", _json(DEFAULT_POLICY), user_id, created,))
        return self.get_user(user_id) or {}

    def get_user(self, user_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT user_id,username,display_name,active,created_at FROM users "
            "WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def _record_login_failure(self, username: str, ip_address: str) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        row = self.conn.execute(
            "SELECT * FROM login_attempts WHERE username=? AND ip_address=?",
            (username, ip_address)).fetchone()
        failures = 1
        window_started = now
        if row:
            started = _parse_time(row["window_started"]) or now
            if now - started <= dt.timedelta(minutes=5):
                failures = int(row["failures"]) + 1
                window_started = started
        locked_until = (now + dt.timedelta(minutes=10)).isoformat(timespec="seconds") \
            if failures >= 5 else None
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO login_attempts VALUES (?,?,?,?,?)",
                (username, ip_address, failures,
                 window_started.isoformat(timespec="seconds"), locked_until))

    def authenticate(self, username: str, password: str,
                     ip_address: str = "") -> dict:
        username = str(username or "").strip().lower()
        ip_address = ip_address or "unknown"
        attempt = self.conn.execute(
            "SELECT * FROM login_attempts WHERE username=? AND ip_address=?",
            (username, ip_address)).fetchone()
        if attempt:
            locked = _parse_time(attempt["locked_until"])
            if locked and locked > dt.datetime.now(dt.timezone.utc):
                raise GovernanceError("login_locked", "登录尝试过多，请稍后再试", 429)
        row = self.conn.execute(
            "SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
        if not row or not verify_password(password, row["password_hash"]):
            self._record_login_failure(username, ip_address)
            raise GovernanceError("invalid_credentials", "用户名或密码错误", 401)
        with self.transaction() as conn:
            conn.execute("DELETE FROM login_attempts WHERE username=? AND ip_address=?",
                         (username, ip_address))
        return {k: row[k] for k in
                ("user_id", "username", "display_name", "active", "created_at")}

    def create_session(self, user_id: str) -> dict:
        raw_token = secrets.token_urlsafe(32)
        session_id = uuid.uuid4().hex
        csrf = secrets.token_urlsafe(24)
        now = dt.datetime.now(dt.timezone.utc)
        expires = now + dt.timedelta(hours=SESSION_HOURS)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                (session_id, hashlib.sha256(raw_token.encode()).hexdigest(), user_id,
                 csrf, now.isoformat(timespec="seconds"),
                 now.isoformat(timespec="seconds"),
                 expires.isoformat(timespec="seconds")))
        return {"token": raw_token, "session_id": session_id,
                "csrf_token": csrf, "expires_at": expires.isoformat(timespec="seconds")}

    def revoke_session(self, raw_token: str) -> None:
        if not raw_token:
            return
        digest = hashlib.sha256(raw_token.encode()).hexdigest()
        with self.transaction() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (digest,))

    def resolve_session(self, raw_token: str, workspace_id: str | None,
                        request_id: str) -> Principal:
        if not raw_token:
            raise GovernanceError("authentication_required", "请先登录", 401)
        digest = hashlib.sha256(raw_token.encode()).hexdigest()
        row = self.conn.execute(
            "SELECT s.*,u.username,u.display_name,u.active FROM sessions s "
            "JOIN users u ON u.user_id=s.user_id WHERE s.token_hash=?", (digest,)).fetchone()
        if not row or not row["active"]:
            raise GovernanceError("invalid_session", "登录状态已失效", 401)
        expires = _parse_time(row["expires_at"])
        if not expires or expires <= dt.datetime.now(dt.timezone.utc):
            with self.transaction() as conn:
                conn.execute("DELETE FROM sessions WHERE session_id=?", (row["session_id"],))
            raise GovernanceError("session_expired", "登录状态已过期", 401)
        memberships = self.list_workspaces(row["user_id"])
        if not memberships:
            raise GovernanceError("workspace_required", "账号未加入任何 Workspace", 403)
        selected = None
        if workspace_id:
            selected = next((m for m in memberships
                             if m["workspace_id"] == workspace_id), None)
            if selected is None:
                raise GovernanceError("workspace_forbidden", "无权访问该 Workspace", 403)
        else:
            selected = next((m for m in memberships if m["role"] == "owner"),
                            memberships[0])
        with self.transaction() as conn:
            conn.execute("UPDATE sessions SET last_seen_at=? WHERE session_id=?",
                         (_now(), row["session_id"]))
        return Principal(
            user_id=row["user_id"], username=row["username"],
            display_name=row["display_name"], workspace_id=selected["workspace_id"],
            workspace_name=selected["name"], role=selected["role"],
            session_id=row["session_id"], csrf_token=row["csrf_token"],
            request_id=request_id)

    def list_workspaces(self, user_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT w.workspace_id,w.name,m.role,w.created_at FROM members m "
            "JOIN workspaces w ON w.workspace_id=m.workspace_id "
            "WHERE m.user_id=? ORDER BY CASE m.role WHEN 'owner' THEN 0 ELSE 1 END,w.name",
            (user_id,)).fetchall()
        return [dict(row) for row in rows]

    def create_workspace(self, workspace_id: str, name: str, user_id: str) -> dict:
        workspace_id = _safe_id(workspace_id, "Workspace id")
        now = _now()
        with self.transaction() as conn:
            try:
                conn.execute("INSERT INTO workspaces VALUES (?,?,?,?)",
                             (workspace_id, name.strip() or workspace_id, user_id, now))
                conn.execute("INSERT INTO members VALUES (?,?,?,?)",
                             (workspace_id, user_id, "owner", now))
                conn.execute("INSERT INTO policies VALUES (?,?,?,?)",
                             (workspace_id, _json(DEFAULT_POLICY), user_id, now))
            except sqlite3.IntegrityError as exc:
                raise GovernanceError("workspace_exists", "Workspace 已存在", 409) from exc
        return {"workspace_id": workspace_id, "name": name.strip() or workspace_id,
                "role": "owner", "created_at": now}

    def workspace_creator(self, workspace_id: str) -> str:
        row = self.conn.execute(
            "SELECT created_by FROM workspaces WHERE workspace_id=?", (workspace_id,)).fetchone()
        if not row:
            raise GovernanceError("workspace_not_found", "Workspace 不存在", 404)
        return str(row[0])

    def list_members(self, workspace_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT u.user_id,u.username,u.display_name,u.active,m.role,m.created_at "
            "FROM members m JOIN users u ON u.user_id=m.user_id "
            "WHERE m.workspace_id=? ORDER BY CASE m.role "
            "WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 WHEN 'editor' THEN 2 ELSE 3 END,u.username",
            (workspace_id,)).fetchall()
        return [dict(row) for row in rows]

    def add_member(self, workspace_id: str, username: str, display_name: str,
                   password: str, role: str, actor_role: str) -> dict:
        role = str(role or "viewer")
        if role not in ROLES or role == "owner":
            raise GovernanceError("invalid_role", "成员角色只允许 admin/editor/viewer", 422)
        if actor_role == "admin" and role == "admin":
            raise GovernanceError("role_forbidden", "只有 owner 可以授予 admin", 403)
        username = _safe_id(username.lower(), "用户名")
        now = _now()
        with self.transaction() as conn:
            user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if user is None:
                user_id = uuid.uuid4().hex
                conn.execute("INSERT INTO users VALUES (?,?,?,?,?,?)",
                             (user_id, username, display_name.strip() or username,
                              hash_password(password), 1, now))
            else:
                user_id = user["user_id"]
            try:
                conn.execute("INSERT INTO members VALUES (?,?,?,?)",
                             (workspace_id, user_id, role, now))
            except sqlite3.IntegrityError as exc:
                raise GovernanceError("member_exists", "成员已在 Workspace 中", 409) from exc
        return next(m for m in self.list_members(workspace_id) if m["user_id"] == user_id)

    def update_member_role(self, workspace_id: str, user_id: str, role: str,
                           actor_role: str) -> dict:
        if role not in ("admin", "editor", "viewer"):
            raise GovernanceError("invalid_role", "角色只允许 admin/editor/viewer", 422)
        if actor_role == "admin" and role == "admin":
            raise GovernanceError("role_forbidden", "只有 owner 可以授予 admin", 403)
        current = self.conn.execute(
            "SELECT role FROM members WHERE workspace_id=? AND user_id=?",
            (workspace_id, user_id)).fetchone()
        if not current:
            raise GovernanceError("member_not_found", "成员不存在", 404)
        if current["role"] == "owner":
            raise GovernanceError("owner_protected", "不能修改 owner 角色", 409)
        if actor_role == "admin" and current["role"] == "admin":
            raise GovernanceError("role_forbidden", "admin 不能修改其他 admin", 403)
        with self.transaction() as conn:
            conn.execute("UPDATE members SET role=? WHERE workspace_id=? AND user_id=?",
                         (role, workspace_id, user_id))
        return next(m for m in self.list_members(workspace_id) if m["user_id"] == user_id)

    def remove_member(self, workspace_id: str, user_id: str, actor_role: str) -> None:
        current = self.conn.execute(
            "SELECT role FROM members WHERE workspace_id=? AND user_id=?",
            (workspace_id, user_id)).fetchone()
        if not current:
            raise GovernanceError("member_not_found", "成员不存在", 404)
        if current["role"] == "owner":
            raise GovernanceError("owner_protected", "不能移除 Workspace owner", 409)
        if actor_role == "admin" and current["role"] == "admin":
            raise GovernanceError("role_forbidden", "admin 不能移除其他 admin", 403)
        with self.transaction() as conn:
            conn.execute("DELETE FROM members WHERE workspace_id=? AND user_id=?",
                         (workspace_id, user_id))

    def get_policy(self, workspace_id: str) -> dict:
        row = self.conn.execute(
            "SELECT policy_json FROM policies WHERE workspace_id=?", (workspace_id,)).fetchone()
        return {**DEFAULT_POLICY, **(json.loads(row[0]) if row else {})}

    def save_policy(self, workspace_id: str, policy: dict, user_id: str) -> dict:
        merged = {**DEFAULT_POLICY, **policy}
        merged["max_agent_steps"] = max(1, min(30, int(merged["max_agent_steps"])))
        merged["min_eval_pass_rate"] = max(
            0.0, min(1.0, float(merged["min_eval_pass_rate"])))
        for key in ("allowed_models", "denied_tools", "denied_mcp_servers",
                    "allowed_flow_node_types", "allowed_http_hosts"):
            merged[key] = sorted({str(v).strip() for v in merged.get(key, []) if str(v).strip()})
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO policies VALUES (?,?,?,?)",
                (workspace_id, _json(merged), user_id, _now()))
        return merged

    def save_version(self, workspace_id: str, resource_type: str, resource_id: str,
                     snapshot: dict | None, action: str, user_id: str,
                     rollback_of: str | None = None) -> dict:
        if resource_type not in RESOURCE_TYPES:
            raise GovernanceError("invalid_resource_type", "不支持的资源类型", 422)
        resource_id = _safe_id(resource_id, "资源 id")
        if action not in ("upsert", "delete"):
            raise GovernanceError("invalid_action", "不支持的版本动作", 422)
        canonical = _json(snapshot) if snapshot is not None else "null"
        content_hash = hashlib.sha256(f"{action}:{canonical}".encode()).hexdigest()
        latest = self.conn.execute(
            "SELECT * FROM resource_versions WHERE workspace_id=? AND resource_type=? "
            "AND resource_id=? ORDER BY version_no DESC LIMIT 1",
            (workspace_id, resource_type, resource_id)).fetchone()
        if latest and latest["status"] == "draft" and latest["content_hash"] == content_hash:
            return self._version_dict(latest)
        version_no = (int(latest["version_no"]) + 1) if latest else 1
        version_id = uuid.uuid4().hex
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO resource_versions "
                "(version_id,workspace_id,resource_type,resource_id,version_no,status,action,"
                "snapshot_json,content_hash,created_by,created_at,rollback_of) "
                "VALUES (?,?,?,?,?,'draft',?,?,?,?,?,?)",
                (version_id, workspace_id, resource_type, resource_id, version_no,
                 action, canonical if snapshot is not None else None, content_hash,
                 user_id, _now(), rollback_of))
        return self.get_version(workspace_id, resource_type, resource_id, version_no) or {}

    @staticmethod
    def _version_dict(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["snapshot"] = json.loads(data.pop("snapshot_json")) \
            if data.get("snapshot_json") else None
        return data

    def get_version(self, workspace_id: str, resource_type: str, resource_id: str,
                    version_no: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM resource_versions WHERE workspace_id=? AND resource_type=? "
            "AND resource_id=? AND version_no=?",
            (workspace_id, resource_type, resource_id, version_no)).fetchone()
        return self._version_dict(row) if row else None

    def get_version_by_id(self, version_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM resource_versions WHERE version_id=?", (version_id,)).fetchone()
        return self._version_dict(row) if row else None

    def list_versions(self, workspace_id: str, resource_type: str,
                      resource_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM resource_versions WHERE workspace_id=? AND resource_type=? "
            "AND resource_id=? ORDER BY version_no DESC",
            (workspace_id, resource_type, resource_id)).fetchall()
        return [self._version_dict(row) for row in rows]

    def list_pending(self, workspace_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT v.*,u.username AS creator_name FROM resource_versions v "
            "JOIN users u ON u.user_id=v.created_by "
            "WHERE v.workspace_id=? AND v.status IN ('pending','approved') "
            "ORDER BY v.created_at", (workspace_id,)).fetchall()
        return [self._version_dict(row) for row in rows]

    def latest_version(self, workspace_id: str, resource_type: str,
                       resource_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM resource_versions WHERE workspace_id=? AND resource_type=? "
            "AND resource_id=? ORDER BY version_no DESC LIMIT 1",
            (workspace_id, resource_type, resource_id)).fetchone()
        return self._version_dict(row) if row else None

    def set_version_status(self, version_id: str, expected: tuple[str, ...], status: str,
                           actor_id: str, reason: str = "") -> dict:
        fields = {
            "pending": ("submitted_by", "submitted_at"),
            "approved": ("approved_by", "approved_at"),
            "rejected": ("rejected_by", "rejected_at"),
            "published": ("published_by", "published_at"),
        }
        if status not in fields:
            raise GovernanceError("invalid_status", "不支持的版本状态", 422)
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM resource_versions WHERE version_id=?",
                               (version_id,)).fetchone()
            if not row:
                raise GovernanceError("version_not_found", "版本不存在", 404)
            if row["status"] == status:
                return self._version_dict(row)
            if row["status"] not in expected:
                raise GovernanceError(
                    "version_conflict", f"版本当前状态为 {row['status']}，不能变更为 {status}", 409)
            actor_col, time_col = fields[status]
            conn.execute(
                f"UPDATE resource_versions SET status=?,{actor_col}=?,{time_col}=?,reason=? "
                "WHERE version_id=?",
                (status, actor_id, _now(), reason[:500], version_id))
        return self.get_version_by_id(version_id) or {}

    def approver_count(self, workspace_id: str) -> int:
        row = self.conn.execute(
            "SELECT count(*) FROM members m JOIN users u ON u.user_id=m.user_id "
            "WHERE m.workspace_id=? AND m.role IN ('owner','admin') AND u.active=1",
            (workspace_id,)).fetchone()
        return int(row[0])

    def set_release(self, version_id: str, actor_id: str,
                    reason: str = "") -> tuple[dict, dict | None]:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM resource_versions WHERE version_id=?",
                               (version_id,)).fetchone()
            if not row:
                raise GovernanceError("version_not_found", "版本不存在", 404)
            old = conn.execute(
                "SELECT * FROM resource_releases WHERE workspace_id=? AND resource_type=? "
                "AND resource_id=?", (row["workspace_id"], row["resource_type"],
                                      row["resource_id"])).fetchone()
            conn.execute(
                "UPDATE resource_versions SET status='published',published_by=?,"
                "published_at=?,reason=? WHERE version_id=?",
                (actor_id, _now(), reason[:500], version_id))
            conn.execute(
                "INSERT OR REPLACE INTO resource_releases VALUES (?,?,?,?,?)",
                (row["workspace_id"], row["resource_type"], row["resource_id"],
                 version_id, _now()))
        return self.get_version_by_id(version_id) or {}, dict(old) if old else None

    def restore_release(self, version_id: str, old_release: dict | None) -> None:
        version = self.get_version_by_id(version_id)
        if not version:
            return
        with self.transaction() as conn:
            conn.execute(
                "UPDATE resource_versions SET status='approved',published_by=NULL,"
                "published_at=NULL WHERE version_id=?", (version_id,))
            if old_release:
                conn.execute(
                    "INSERT OR REPLACE INTO resource_releases VALUES (?,?,?,?,?)",
                    (old_release["workspace_id"], old_release["resource_type"],
                     old_release["resource_id"], old_release["version_id"], _now()))
            else:
                conn.execute(
                    "DELETE FROM resource_releases WHERE workspace_id=? AND resource_type=? "
                    "AND resource_id=?", (version["workspace_id"], version["resource_type"],
                                         version["resource_id"]))

    def get_release(self, workspace_id: str, resource_type: str,
                    resource_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT v.* FROM resource_releases r JOIN resource_versions v "
            "ON v.version_id=r.version_id WHERE r.workspace_id=? AND r.resource_type=? "
            "AND r.resource_id=?", (workspace_id, resource_type, resource_id)).fetchone()
        return self._version_dict(row) if row else None

    def list_releases(self, workspace_id: str, resource_type: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT v.* FROM resource_releases r JOIN resource_versions v "
            "ON v.version_id=r.version_id WHERE r.workspace_id=? AND r.resource_type=? "
            "ORDER BY v.resource_id", (workspace_id, resource_type)).fetchall()
        return [self._version_dict(row) for row in rows]

    def audit(self, event_type: str, *, workspace_id: str | None = None,
              user_id: str | None = None, username: str | None = None,
              resource_type: str | None = None, resource_id: str | None = None,
              version_no: int | None = None, request_id: str = "system",
              outcome: str = "success", ip_address: str = "",
              details: dict | None = None) -> dict:
        event = {
            "event_id": uuid.uuid4().hex, "workspace_id": workspace_id,
            "user_id": user_id, "username": username, "event_type": event_type,
            "resource_type": resource_type, "resource_id": resource_id,
            "version_no": version_no, "request_id": request_id,
            "outcome": outcome, "ip_address": ip_address,
            "details_json": _json(sanitize_details(details or {})), "created_at": _now(),
        }
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(event[k] for k in (
                    "event_id", "workspace_id", "user_id", "username", "event_type",
                    "resource_type", "resource_id", "version_no", "request_id",
                    "outcome", "ip_address", "details_json", "created_at")))
        return {**event, "details": json.loads(event["details_json"])}

    def list_audit(self, workspace_id: str, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM audit_events WHERE workspace_id=? ORDER BY created_at DESC LIMIT ?",
            (workspace_id, max(1, min(500, int(limit))))).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["details"] = json.loads(data.pop("details_json"))
            out.append(data)
        return out

    def migration_applied(self, key: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM migrations WHERE migration_key=?", (key,)).fetchone() is not None

    def mark_migration(self, key: str, details: dict) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO migrations VALUES (?,?,?)",
                         (key, _now(), _json(details)))
