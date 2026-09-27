"""数字员工编制：岗位模板、任务 → 团队计划（LLM 优先、规则降级）与团队/员工/轮次存储。

岗位是模板，员工是岗位在一次任务里的实例；实例默认用内存里的岗位提示词运行，
已发布的同名智能体资产会覆盖它（见 crew.CrewDriver._agent_for）。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import threading
import uuid
from pathlib import Path

from .agentrt import uuid4_hex

CORE_ROLES = ("product", "architect", "developer", "qa")

ROLE_TEMPLATES: dict[str, dict] = {
    "product": {
        "key": "product", "name": "产品经理", "icon": "📦", "color": "#fdb022",
        "charter": "把诉求转成可开发的目标：用户与场景、范围与不做什么、验收标准、优先级。",
        "deliverables": "PRD 要点（目标用户/核心场景/功能清单/验收标准/里程碑）",
        "downstream": ["architect"],
        "keywords": ["产品", "需求", "功能", "prd", "用户", "场景", "验收"],
    },
    "architect": {
        "key": "architect", "name": "技术架构", "icon": "🏗", "color": "#8b98f8",
        "charter": "选定技术方案并给出边界：模块划分、数据模型、接口契约、技术风险与取舍。",
        "deliverables": "架构方案（分层/模块/数据模型/API 契约/选型理由/风险）",
        "downstream": ["developer"],
        "keywords": ["架构", "技术", "接口", "api", "数据模型", "选型", "性能"],
    },
    "developer": {
        "key": "developer", "name": "开发", "icon": "💻", "color": "#3dd68c",
        "charter": "把方案落成可实现的工作项：改动清单、关键实现思路、依赖与联调顺序。",
        "deliverables": "实现方案（改动清单/关键代码思路/数据与接口落地/联调顺序）",
        "downstream": ["qa"],
        "keywords": ["开发", "实现", "编码", "前端", "后端", "代码", "bug"],
    },
    "qa": {
        "key": "qa", "name": "测试", "icon": "🧪", "color": "#f97066",
        "charter": "证明它能用并守住回归：用例设计、边界与异常路径、质量门禁与放行结论。",
        "deliverables": "测试方案（用例矩阵/边界与异常/回归范围/放行标准与结论）",
        "downstream": ["ops"],
        "keywords": ["测试", "用例", "质量", "回归", "验收", "bug"],
    },
    "ops": {
        "key": "ops", "name": "运维", "icon": "🚀", "color": "#22d3ee",
        "charter": "让它稳定上线：部署拓扑、发布与回滚步骤、监控告警与容量预案。",
        "deliverables": "发布方案（部署拓扑/发布与回滚步骤/监控告警/容量与故障预案）",
        "downstream": ["sales"],
        "keywords": ["部署", "上线", "运维", "监控", "发布", "容量", "sla", "回滚"],
    },
    "sales": {
        "key": "sales", "name": "销售", "icon": "📣", "color": "#f472b6",
        "charter": "把成果换成订单：目标客户、卖点与差异、报价与异议应对、上市话术。",
        "deliverables": "上市包（目标客户/核心卖点/差异化对比/常见异议应对/首版话术）",
        "downstream": [],
        "keywords": ["销售", "推广", "客户", "定价", "上市", "渠道", "营收", "文案"],
    },
}

CHAIN_ORDER = ["product", "architect", "developer", "qa", "ops", "sales"]

TEAM_STATUSES = ("draft", "running", "paused", "waiting_human", "done", "failed",
                 "stopped")
TEAM_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"running", "stopped"},
    "running": {"paused", "waiting_human", "done", "failed", "stopped"},
    "paused": {"running", "stopped"},
    "waiting_human": {"running", "stopped"},
    "done": {"running", "stopped"},
    "failed": {"running", "paused", "stopped"},
    "stopped": set(),
}
EMPLOYEE_STATUSES = ("idle", "working", "blocked", "done")

_DEV_RE = re.compile(
    r"开发|做一?个|构建|搭建|实现|建一?个|产品|系统|平台|应用|网站|工具|功能|"
    r"prototype|mvp|app|platform|system", re.I)


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _elapsed_ms(started_at: str) -> int:
    try:
        started = dt.datetime.fromisoformat(started_at)
    except (TypeError, ValueError):
        return 0
    return max(0, int((dt.datetime.now() - started).total_seconds() * 1000))


def role_card(role_key: str) -> dict:
    role = ROLE_TEMPLATES.get(str(role_key))
    if not role:
        raise ValueError(f"未知岗位：{role_key}")
    return dict(role)


def build_agent_spec(role_key: str, task: str, focus: str, *,
                     include_tools: list[str] | None = None) -> dict:
    """岗位实例的智能体配置（对话式 ReAct + 协作工具）。"""
    role = role_card(role_key)
    tools = ["board_list", "bus_read", "bus_post", "board_move", "board_add",
             "board_comment", *(include_tools or [])]
    system = (
        f"你是「{role['name']}」，一名数字员工。职责：{role['charter']}\n"
        f"你的交付物：{role['deliverables']}\n"
        f"本轮重点：{focus or '按职责推进你名下的任务卡'}\n"
        "协作协议（必须遵守）：\n"
        "1. 先 board_list 看清看板与自己和上下游的卡片状态；\n"
        "2. 再 bus_read 读你的收件箱，逐条回应未读；\n"
        "3. 动手推进时 board_move 改卡片状态，并用 board_comment 写下可核对的结论；\n"
        "4. 有结论就 bus_post 发给下游岗位（to 用岗位名如 developer，或 human=老板，"
        "或 *=全员），受阻时也发给 human 求助；\n"
        "5. 只做职责内的事，不替别的岗位下结论；信息不足时明确说缺什么。\n"
        "最终回复用不超过 200 字的中文小结，它会自动作为你在群里的一句话发言。")
    return {
        "id": f"wf-{role_key}", "name": role["name"],
        "description": f"数字员工 · {role['name']}（{task[:40]}）",
        "system": system, "flow_id": "", "kb_ids": [], "skill_ids": [],
        "tool_ids": tools, "mcp_servers": [], "memory": True, "max_steps": 8,
        "role": role_key,
    }


# ------------------------------------------------------------------ 自动编制
def _rule_plan(task: str) -> dict:
    text = task or ""
    lowered = text.lower()
    hits = {key: sum(1 for kw in role["keywords"] if kw in lowered)
            for key, role in ROLE_TEMPLATES.items()}
    devish = bool(_DEV_RE.search(text))
    if devish:
        roles = list(CHAIN_ORDER)
    else:
        roles = [key for key in CHAIN_ORDER if hits[key] > 0]
        if not roles:
            roles = list(CHAIN_ORDER)
    roster = [{
        "role": key,
        "reason": (f"任务命中该岗位关键词（{hits[key]} 次）" if hits[key] else
                   "开发一个产品需要这条完整价值链" if devish else
                   "默认编制补齐该环节"),
        "focus": ROLE_TEMPLATES[key]["deliverables"],
    } for key in roles]
    milestones = []
    previous = None
    for key in roles:
        role = ROLE_TEMPLATES[key]
        milestones.append({
            "key": f"m-{key}", "title": f"{role['name']}：{role['deliverables']}",
            "role": key, "detail": f"任务：{text[:400]}",
            "depends_on": [previous] if previous else []})
        previous = f"m-{key}"
    if "qa" in roles and "ops" in roles:
        milestones.append({
            "key": "m-accept", "title": "产品经理：交付验收与结论汇总",
            "role": "product",
            "detail": "汇总各环节产出，给出是否可交付的结论与后续待办。",
            "depends_on": [f"m-{key}" for key in roles if key in ("ops", "sales")]})
    return {
        "goal": f"{(text or '未命名任务').strip()[:120]}",
        "roster": roster, "milestones": milestones, "via": "rules",
        "reason": ("识别为产品/系统交付类任务，按 产品→架构→开发→测试→运维→销售 "
                   "全链路建队" if devish else
                   "按任务关键词匹配岗位：" + "、".join(
                       ROLE_TEMPLATES[k]["name"] for k in roles)),
    }


def _parse_llm_plan(raw: dict | None, task: str) -> dict | None:
    if not isinstance(raw, dict):
        return None
    roster, seen = [], set()
    for item in raw.get("roster") or []:
        key = str((item or {}).get("role") or "").strip()
        if key not in ROLE_TEMPLATES or key in seen:
            continue
        seen.add(key)
        roster.append({"role": key,
                       "reason": str(item.get("reason") or "")[:200],
                       "focus": str(item.get("focus") or
                                    ROLE_TEMPLATES[key]["deliverables"])[:300]})
    if not roster or len(roster) > len(ROLE_TEMPLATES):
        return None
    known = {r["role"] for r in roster}
    milestones = []
    used_keys: set[str] = set()
    for item in raw.get("milestones") or []:
        role = str((item or {}).get("role") or "")
        title = str((item or {}).get("title") or "").strip()
        if role not in known or not title:
            continue
        key = str(item.get("key") or "").strip() or f"m{len(milestones) + 1}"
        while key in used_keys:
            key = f"{key}-"
        used_keys.add(key)
        milestones.append({
            "key": key, "title": title[:200], "role": role,
            "detail": str(item.get("detail") or f"任务：{task[:400]}"),
            "depends_on": [str(d) for d in (item.get("depends_on") or [])]})
    if not milestones:
        milestones = _chain_milestones(task, list(known), roster)
    return {
        "goal": str(raw.get("goal") or task)[:200],
        "roster": roster, "milestones": milestones, "via": "llm",
        "reason": str(raw.get("reason") or "模型依据任务内容选择岗位")[:300],
    }


def _chain_milestones(task: str, roles: list[str], roster: list[dict]) -> list[dict]:
    ordered = [key for key in CHAIN_ORDER if key in set(roles)]
    milestones, previous = [], None
    for key in ordered:
        role = ROLE_TEMPLATES[key]
        focus = next((r["focus"] for r in roster if r["role"] == key),
                     role["deliverables"])
        milestones.append({"key": f"m-{key}",
                           "title": f"{role['name']}：{focus}"[:200],
                           "role": key, "detail": f"任务：{task[:400]}",
                           "depends_on": [previous] if previous else []})
        previous = f"m-{key}"
    return milestones


def _restrict(plan: dict, allowed: set[str]) -> dict:
    """裁剪到允许岗位，并清理悬空/自引用依赖。"""
    plan["roster"] = [r for r in plan["roster"] if r["role"] in allowed]
    keys = {r["role"] for r in plan["roster"]}
    plan["milestones"] = [
        {**m, "depends_on": [d for d in m["depends_on"] if d != m["key"]]}
        for m in plan["milestones"] if m["role"] in keys]
    known = {m["key"] for m in plan["milestones"]}
    for milestone in plan["milestones"]:
        milestone["depends_on"] = [d for d in milestone["depends_on"] if d in known]
    if not plan["roster"]:
        raise ValueError("策略允许的岗位为空，无法编制团队")
    return plan


def normalize_plan(task: str, raw: dict | None) -> dict:
    """校验人工编辑的计划；不合法直接报错，只有 milestone 缺失时按岗位链补齐。"""
    plan = _parse_llm_plan(raw, str(task or "").strip())
    if plan is None:
        raise ValueError("团队计划不合法：roster 需要至少一个已知岗位")
    plan["via"] = "human"
    plan["reason"] = str(raw.get("reason") or "人工调整编制")[:300]
    return _restrict(plan, set(ROLE_TEMPLATES))


def compose(task: str, llm_cfg: dict | None = None, *,
            allowed_roles: list[str] | None = None,
            llm_json_fn=None) -> dict:
    """任务 → 团队计划。LLM 优先，任何失败都退回可解释的规则计划。"""
    task = str(task or "").strip()
    if not task:
        raise ValueError("任务描述不能为空")
    plan = _rule_plan(task)
    allowed = set(allowed_roles or []) or set(ROLE_TEMPLATES)
    llm = None
    if llm_cfg and llm_cfg.get("enabled"):
        from .llm import llm_json as default_llm_json
        call = llm_json_fn or default_llm_json
        catalog = "\n".join(
            f"- {key}（{r['name']}）：{r['charter']} 交付：{r['deliverables']}；"
            f"通常交给 {r['downstream'] or '（末端）'}"
            for key, r in ROLE_TEMPLATES.items() if key in allowed)
        llm = call(
            llm_cfg,
            "你是数字员工团队的编制者。根据任务挑出必要岗位并排出里程碑，"
            "只输出 JSON，不要解释。role 必须来自给定清单。"
            "格式：{\"goal\":\"...\",\"reason\":\"...\",\"roster\":"
            "[{\"role\":\"product\",\"reason\":\"...\",\"focus\":\"...\"}],"
            "\"milestones\":[{\"key\":\"m1\",\"title\":\"...\",\"role\":\"product\","
            "\"detail\":\"...\",\"depends_on\":[\"m2\"]}]}",
            f"任务：{task}\n可选岗位：\n{catalog}")
    parsed = _parse_llm_plan(llm, task)
    if parsed:
        plan = parsed
    return _restrict(plan, allowed)


# ------------------------------------------------------------------ 团队存储
class WorkforceStore:
    def __init__(self, db_path: Path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.lock = threading.RLock()
        self.conn.execute("""CREATE TABLE IF NOT EXISTS teams (
            id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, task TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft', plan TEXT NOT NULL DEFAULT '{}',
            round_no INTEGER NOT NULL DEFAULT 0, max_rounds INTEGER NOT NULL DEFAULT 12,
            summary TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS employees (
            id TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES teams(id),
            role TEXT NOT NULL, name TEXT NOT NULL, agent_id TEXT NOT NULL DEFAULT '',
            focus TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'idle',
            created_at TEXT NOT NULL, last_active_at TEXT, turns INTEGER NOT NULL DEFAULT 0,
            messages_sent INTEGER NOT NULL DEFAULT 0, produced TEXT NOT NULL DEFAULT '{}')""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS rounds (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, team_id TEXT NOT NULL,
            round_no INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'running',
            detail TEXT NOT NULL DEFAULT '{}', started_at TEXT NOT NULL,
            finished_at TEXT, ms INTEGER NOT NULL DEFAULT 0)""")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_team_ws ON teams(workspace_id, created_at)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_emp_team ON employees(team_id)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_round_team ON rounds(team_id, round_no)")
        self.conn.commit()

    # ---------------------------------------------------------------- 团队
    def create_team(self, *, workspace_id: str, task: str, plan: dict,
                    created_by: str, max_rounds: int = 12) -> dict:
        team_id = uuid4_hex()
        with self.lock:
            self.conn.execute(
                "INSERT INTO teams(id, workspace_id, task, status, plan, max_rounds,"
                " created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (team_id, workspace_id, task[:4000], "draft",
                 json.dumps(plan, ensure_ascii=False), int(max_rounds), created_by,
                 _now()))
            self.conn.commit()
        return self.get_team(team_id)

    def get_team(self, team_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
        return self._team(row) if row else None

    def list_teams(self, workspace_id: str, *, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM teams WHERE workspace_id=? ORDER BY created_at DESC"
                " LIMIT ?", (workspace_id, max(1, min(200, int(limit))))).fetchall()
        return [self._team(r) for r in rows]

    def set_team_status(self, team_id: str, to_status: str, *, error: str = "",
                        summary: str = "") -> dict:
        team = self.get_team(team_id)
        if team is None:
            raise ValueError(f"团队不存在：{team_id}")
        current = team["status"]
        if to_status == current:
            return team
        if to_status not in TEAM_TRANSITIONS.get(current, set()):
            raise ValueError(f"不允许的团队状态迁移：{current} → {to_status}")
        fields = ["status=?"]
        args: list = [to_status]
        if error:
            fields.append("error=?")
            args.append(error[:1000])
        if summary:
            fields.append("summary=?")
            args.append(summary[:4000])
        if to_status == "running" and not team["started_at"]:
            fields.append("started_at=?")
            args.append(_now())
        if to_status in ("done", "failed", "stopped"):
            fields.append("finished_at=?")
            args.append(_now())
        args.append(team_id)
        with self.lock:
            self.conn.execute(
                f"UPDATE teams SET {','.join(fields)} WHERE id=?", args)
            self.conn.commit()
        return self.get_team(team_id)

    def set_team_plan(self, team_id: str, plan: dict) -> dict:
        with self.lock:
            self.conn.execute("UPDATE teams SET plan=? WHERE id=?",
                              (json.dumps(plan, ensure_ascii=False), team_id))
            self.conn.commit()
        return self.get_team(team_id)

    def set_task(self, team_id: str, task: str) -> dict:
        with self.lock:
            self.conn.execute("UPDATE teams SET task=? WHERE id=?",
                              (str(task or "")[:4000], team_id))
            self.conn.commit()
        return self.get_team(team_id)

    def bump_round(self, team_id: str) -> int:
        with self.lock:
            self.conn.execute(
                "UPDATE teams SET round_no = round_no + 1 WHERE id=?", (team_id,))
            self.conn.commit()
            row = self.conn.execute(
                "SELECT round_no FROM teams WHERE id=?", (team_id,)).fetchone()
        return int(row["round_no"])

    def set_max_rounds(self, team_id: str, max_rounds: int) -> None:
        with self.lock:
            self.conn.execute("UPDATE teams SET max_rounds=? WHERE id=?",
                              (max(1, min(50, int(max_rounds))), team_id))
            self.conn.commit()

    def count_teams(self, workspace_id: str, *,
                    statuses: tuple[str, ...] | None = None) -> int:
        sql = "SELECT COUNT(*) FROM teams WHERE workspace_id=?"
        args: list = [workspace_id]
        if statuses:
            sql += " AND status IN (" + ",".join("?" * len(statuses)) + ")"
            args += list(statuses)
        with self.lock:
            return int(self.conn.execute(sql, args).fetchone()[0])

    # ---------------------------------------------------------------- 员工
    def add_employee(self, *, team_id: str, role: str, name: str, agent_id: str,
                     focus: str = "") -> dict:
        employee_id = f"e{uuid.uuid4().hex[:8]}"
        with self.lock:
            self.conn.execute(
                "INSERT INTO employees(id, team_id, role, name, agent_id, focus,"
                " status, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (employee_id, team_id, role, name, agent_id, focus[:500], "idle",
                 _now()))
            self.conn.commit()
        return self.get_employee(employee_id)

    def get_employee(self, employee_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
        return self._employee(row) if row else None

    def employees(self, team_id: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM employees WHERE team_id=? ORDER BY created_at",
                (team_id,)).fetchall()
        return [self._employee(r) for r in rows]

    def set_employee(self, employee_id: str, *, status: str | None = None,
                     produced: dict | None = None,
                     messages_sent: int | None = None,
                     turns: int | None = None) -> dict:
        employee = self.get_employee(employee_id)
        if employee is None:
            raise ValueError(f"员工不存在：{employee_id}")
        fields = ["last_active_at=?"]
        args: list = [_now()]
        if status:
            if status not in EMPLOYEE_STATUSES:
                raise ValueError(f"未知员工状态：{status}")
            fields.append("status=?")
            args.append(status)
        if produced is not None:
            merged = {**employee["produced"], **produced}
            fields.append("produced=?")
            args.append(json.dumps(merged, ensure_ascii=False))
        if messages_sent is not None:
            fields.append("messages_sent=?")
            args.append(int(messages_sent))
        if turns is not None:
            fields.append("turns=?")
            args.append(int(turns))
        args.append(employee_id)
        with self.lock:
            self.conn.execute(
                f"UPDATE employees SET {','.join(fields)} WHERE id=?", args)
            self.conn.commit()
        return self.get_employee(employee_id)

    def find_employee(self, team_id: str, token: str) -> dict | None:
        """按员工 id、岗位 key 或中文名解析收件人。"""
        token = str(token or "").strip()
        if not token:
            return None
        employees = self.employees(team_id)
        for emp in employees:
            if token in (emp["id"], emp["role"], emp["name"], emp["agent_id"]):
                return emp
        for emp in employees:
            if token.lower() in emp["name"].lower():
                return emp
        return None

    # ---------------------------------------------------------------- 轮次
    def begin_round(self, team_id: str, round_no: int) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO rounds(team_id, round_no, status, detail, started_at)"
                " VALUES (?,?,?,?,?)",
                (team_id, round_no, "running", "{}", _now()))
            self.conn.commit()
            return int(cur.lastrowid)

    def end_round(self, seq: int, *, status: str, detail: dict) -> None:
        with self.lock:
            row = self.conn.execute(
                "SELECT started_at FROM rounds WHERE seq=?", (seq,)).fetchone()
            started = row["started_at"] if row else _now()
            ms = _elapsed_ms(started)
            self.conn.execute(
                "UPDATE rounds SET status=?, detail=?, finished_at=?, ms=?"
                " WHERE seq=?",
                (status, json.dumps(detail, ensure_ascii=False), _now(), ms, seq))
            self.conn.commit()

    def rounds(self, team_id: str, *, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM rounds WHERE team_id=? ORDER BY seq DESC LIMIT ?",
                (team_id, max(1, min(200, int(limit))))).fetchall()
        return [{"seq": r["seq"], "round_no": r["round_no"], "status": r["status"],
                 "detail": json.loads(r["detail"] or "{}"), "started_at": r["started_at"],
                 "finished_at": r["finished_at"], "ms": r["ms"]}
                for r in reversed(rows)]

    def restartable(self, workspace_id: str | None = None) -> list[dict]:
        """进程重启后需要降级的团队（running 不可信）。"""
        sql = "SELECT id FROM teams WHERE status='running'"
        args: list = []
        if workspace_id:
            sql += " AND workspace_id=?"
            args.append(workspace_id)
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [r["id"] for r in rows]

    @staticmethod
    def _team(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "workspace_id": row["workspace_id"],
                "task": row["task"], "status": row["status"],
                "plan": json.loads(row["plan"] or "{}"), "round_no": row["round_no"],
                "max_rounds": row["max_rounds"], "summary": row["summary"],
                "created_by": row["created_by"], "created_at": row["created_at"],
                "started_at": row["started_at"], "finished_at": row["finished_at"],
                "error": row["error"]}

    @staticmethod
    def _employee(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "team_id": row["team_id"], "role": row["role"],
                "name": row["name"], "agent_id": row["agent_id"],
                "focus": row["focus"], "status": row["status"],
                "created_at": row["created_at"], "last_active_at": row["last_active_at"],
                "turns": row["turns"], "messages_sent": row["messages_sent"],
                "produced": json.loads(row["produced"] or "{}")}
