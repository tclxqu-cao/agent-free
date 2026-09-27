"""数字员工协作驱动器：轮次调度、看板/总线协作工具、无 LLM 降级产出与收敛兜底。

一个团队一个后台线程；员工在一次轮次里按价值链顺序依次上场：读收件箱 → 看自己的卡
→ 调 AgentRuntime（装配团队作用域的协作工具）→ 迁移卡片并发消息。轮次边界响应 pause/stop。

收敛不依赖模型自觉：员工把做完的卡推到 review，下游岗位（末端交给产品经理）验收为 done；
一轮没有任何新消息与新迁移即停滞，连续两轮停滞转为 waiting_human 并向老板求助。
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from dataclasses import dataclass, field

from .agentrt import AgentRuntime
from .board import BoardError
from .bus import BROADCAST, HUMAN_ACTOR
from .tools import ToolRegistry
from .workforce import CHAIN_ORDER, ROLE_TEMPLATES, build_agent_spec

TEAM_TOOLS = ("board_list", "bus_read", "bus_post", "board_move", "board_add",
              "board_comment")
STALL_ROUNDS = 2
INBOX_ITEMS = 8
BOARD_ITEMS = 12
OUTPUT_CHARS = 1200
SUMMARY_CHARS = 400
KINDS = ("assign", "request", "response", "review", "deliver", "announce")


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _clip(text, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _index(role: str) -> int:
    return CHAIN_ORDER.index(role) if role in CHAIN_ORDER else len(CHAIN_ORDER)


@dataclass
class Actor:
    """协作工具的执行身份（只在驱动线程内有效，靠 threading.local 隔离）。"""

    workspace_id: str
    team_id: str
    employee_id: str
    role: str
    name: str
    round_no: int
    posted: int = 0
    moved: int = 0
    touched: list[str] = field(default_factory=list)


class CrewError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class CrewDriver:
    def __init__(self, *, teams, board, bus, runtime, governance=None):
        self.teams = teams
        self.board = board
        self.bus = bus
        self.runtime = runtime
        self.governance = governance
        self.hub = bus.hub
        self._threads: dict[str, threading.Thread] = {}
        self._flags: dict[str, set[str]] = {}
        self._guards = threading.RLock()
        self._tls = threading.local()

    # ------------------------------------------------------------------ 装配
    def provision(self, team_id: str, *, created_by: str = "") -> dict:
        """按计划落员工与任务卡，并把开工指令作为分派消息发给每个员工（幂等）。"""
        team = self._require_team(team_id)
        if self.teams.employees(team_id):
            return {"employees": len(self.teams.employees(team_id)), "reused": True}
        plan = team["plan"] or {}
        roster = sorted(plan.get("roster") or [], key=lambda r: _index(r.get("role")))
        by_role: dict[str, dict] = {}
        for entry in roster:
            role = str(entry.get("role") or "")
            if role not in ROLE_TEMPLATES or role in by_role:
                continue
            by_role[role] = self.teams.add_employee(
                team_id=team_id, role=role, name=ROLE_TEMPLATES[role]["name"],
                agent_id=f"wf-{role}", focus=str(entry.get("focus") or ""))
        card_ids: dict[str, str] = {}
        for milestone in plan.get("milestones") or []:
            employee = by_role.get(str(milestone.get("role") or ""))
            if employee is None:
                continue
            key = str(milestone.get("key") or "")
            card = self.board.add(
                workspace_id=team["workspace_id"], team_id=team_id,
                title=_clip(milestone.get("title") or f"{employee['name']} 任务", 200),
                role=employee["role"],
                detail=str(milestone.get("detail") or team["task"])[:2000],
                depends_on=[card_ids[d] for d in (milestone.get("depends_on") or [])
                            if d in card_ids],
                created_by=employee["id"])
            if key:
                card_ids[key] = card["id"]
        for employee in by_role.values():
            mine = self.board.list(team_id, role=employee["role"])
            own = [c for c in mine if not c["depends_on"]]
            first = (own or mine)[0]["title"] if (own or mine) else "按职责推进"
            self.bus.post(
                workspace_id=team["workspace_id"], team_id=team_id,
                from_actor=HUMAN_ACTOR, to_actor=employee["id"], kind="assign",
                subject=f"开工：{first}", round_no=1,
                body={"text": f"目标：{team['task'][:600]}\n你的第一项任务：{first}"})
        self._audit("workforce.team.provision", team, created_by,
                    {"employees": len(by_role), "cards": len(card_ids)})
        return {"employees": len(by_role), "cards": len(card_ids)}

    # ------------------------------------------------------------------ 控制
    def start(self, team_id: str, *, actor: str = "",
              max_rounds: int | None = None) -> dict:
        team = self._require_team(team_id)
        with self._guards:
            if self._alive(team_id):
                raise CrewError("crew_already_running", "该团队已在运行")
            if team["status"] == "draft":
                self.provision(team_id, created_by=actor)
            if max_rounds:
                self.teams.set_max_rounds(team_id, max_rounds)
            self._to_status(team_id, "running")
            self._flags[team_id] = set()
            thread = threading.Thread(target=self._loop, args=(team_id,),
                                      daemon=True, name=f"crew-{team_id}")
            self._threads[team_id] = thread
            thread.start()
        team = self._require_team(team_id)
        self._audit("workforce.team.start", team, actor,
                    {"resumed": team["round_no"] > 0})
        self._emit_team(team_id)
        return {"ok": True, "status": team["status"]}

    def step(self, team_id: str, *, actor: str = "") -> dict:
        """同步推进一轮（不起线程），用于单轮调试与演示。"""
        with self._guards:
            if self._alive(team_id):
                raise CrewError("crew_already_running", "该团队已在运行")
        self.provision(team_id, created_by=actor)
        self._to_status(team_id, "running")
        detail = self._run_round(team_id)
        team = self._require_team(team_id)
        if team["status"] == "running":
            self._to_status(team_id, "paused")
        self._audit("workforce.team.step", team, actor, detail)
        return {"ok": True, "status": self._require_team(team_id)["status"],
                "round": detail}

    def pause(self, team_id: str, *, actor: str = "") -> dict:
        return self._signal(team_id, "pause", actor)

    def stop(self, team_id: str, *, actor: str = "") -> dict:
        return self._signal(team_id, "stop", actor)

    def running(self, team_id: str) -> bool:
        with self._guards:
            return self._alive(team_id)

    def degrade_interrupted(self, workspace_id: str | None = None) -> list[str]:
        """进程重启后 running 不可信：降级为 paused 并在计划上标记可恢复。"""
        changed: list[str] = []
        for team_id in self.teams.restartable(workspace_id):
            team = self._require_team(team_id)
            plan = dict(team["plan"] or {})
            plan["restart"] = True
            self.teams.set_team_plan(team_id, plan)
            try:
                self.teams.set_team_status(team_id, "paused",
                                           error="进程重启，驱动线程已中断")
            except ValueError:
                continue
            changed.append(team_id)
        return changed

    def _signal(self, team_id: str, flag: str, actor: str) -> dict:
        team = self._require_team(team_id)
        with self._guards:
            alive = self._alive(team_id)
            if alive:
                self._flags.setdefault(team_id, set()).add(flag)
        if not alive:
            target = {"pause": "paused", "stop": "stopped"}[flag]
            self._to_status(team_id, target)
        self._audit(f"workforce.team.{flag}", self._require_team(team_id), actor,
                    {"signalled_thread": alive, "before": team["status"]})
        updated = self._require_team(team_id)
        self._emit_team(team_id)
        return {"ok": True, "status": updated["status"], "signalled": alive}

    def _alive(self, team_id: str) -> bool:
        thread = self._threads.get(team_id)
        return bool(thread and thread.is_alive())

    def _to_status(self, team_id: str, status: str) -> None:
        try:
            self.teams.set_team_status(team_id, status)
        except ValueError as e:
            raise CrewError("team_status_conflict", str(e)) from e

    # ------------------------------------------------------------------ 主循环
    def _loop(self, team_id: str) -> None:
        stall = 0
        try:
            while True:
                with self._guards:
                    flags = set(self._flags.get(team_id) or set())
                if "stop" in flags:
                    self._to_status(team_id, "stopped")
                    self._emit_team(team_id)
                    return
                if "pause" in flags:
                    self._to_status(team_id, "paused")
                    self._emit_team(team_id)
                    return
                team = self._require_team(team_id)
                if int(team["round_no"]) >= int(team["max_rounds"]):
                    self._to_status(team_id, "paused")
                    self._notify(team, kind="request", to=HUMAN_ACTOR,
                                 subject="达到轮次上限，需要人决定",
                                 text="已跑到 max_rounds，看板仍有未完成任务："
                                      "请追加轮次、缩小范围或给出新指示。")
                    self._emit_team(team_id)
                    return
                detail = self._run_round(team_id)
                if detail.get("terminal"):
                    return
                stall = 0 if (detail["messages"] or detail["moves"]) else stall + 1
                if stall >= STALL_ROUNDS:
                    self._to_status(team_id, "waiting_human")
                    self._notify(self._require_team(team_id), kind="request",
                                 to=HUMAN_ACTOR, subject="团队停滞，等待人类指示",
                                 text=f"连续 {stall} 轮没有新消息与卡片迁移。"
                                      f"最近一轮明细：{_clip(json.dumps(detail, ensure_ascii=False), 500)}")
                    self._emit_team(team_id)
                    return
        except Exception as e:  # noqa: BLE001 驱动线程不得静默消失
            self._fail(team_id, e)
        finally:
            with self._guards:
                self._threads.pop(team_id, None)
                self._flags.pop(team_id, None)

    def _fail(self, team_id: str, error: Exception) -> None:
        team = self._require_team(team_id)
        try:
            self.teams.set_team_status(team_id, "failed", error=str(error)[:500])
        except ValueError:
            pass
        self._audit("workforce.team.failed", team, "crew",
                    {"error": str(error)[:500]}, outcome="failure")
        self._emit_team(team_id)

    def _run_round(self, team_id: str) -> dict:
        team = self._require_team(team_id)
        round_started = _now()
        round_no = self.teams.bump_round(team_id)
        seq = self.teams.begin_round(team_id, round_no)
        messages_before = int(self.bus.stats(team_id)["total"])
        moves_before = len(self.board.history(team_id, limit=500))
        detail: dict = {"round_no": round_no, "participants": [], "skipped": [],
                        "messages": 0, "moves": 0, "errors": [], "terminal": False}
        employees = sorted(self.teams.employees(team_id),
                           key=lambda e: (_index(e["role"]), e["created_at"]))
        for employee in employees:
            outcome = self._act(team_id, employee, round_no)
            if outcome.get("skipped"):
                detail["skipped"].append(employee["role"])
            else:
                detail["participants"].append(employee["role"])
            if outcome.get("error"):
                detail["errors"].append({"role": employee["role"],
                                         "error": outcome["error"]})
        detail["moves"] += self._sweep_review(team_id, round_no, round_started)
        self._converge(team_id, detail, round_no)
        detail["messages"] = int(self.bus.stats(team_id)["total"]) - messages_before
        detail["moves"] += len(self.board.history(team_id, limit=500)) - moves_before
        self.teams.end_round(
            seq, status="done" if not detail["errors"] else "partial",
            detail={k: v for k, v in detail.items() if k != "terminal"})
        self.hub.publish({"type": "round", "workspace_id": team["workspace_id"],
                          "team_id": team_id, "id": f"r{seq}", "payload": detail})
        return detail

    # ------------------------------------------------------------------ 单次上场
    def _act(self, team_id: str, employee: dict, round_no: int) -> dict:
        team = self._require_team(team_id)
        inbox = self.bus.inbox(team_id, employee["id"], unread_only=True,
                               limit=INBOX_ITEMS)
        cards = self._actionable_cards(team_id, employee["role"])
        reviews = self._review_queue(team_id, employee["role"])
        if not inbox and not cards and not reviews:
            self.teams.set_employee(employee["id"], status="idle")
            return {"skipped": True}
        actor = Actor(workspace_id=team["workspace_id"], team_id=team_id,
                      employee_id=employee["id"], role=employee["role"],
                      name=employee["name"], round_no=round_no)
        self._tls.actor = actor
        self.teams.set_employee(employee["id"], status="working")
        self._emit_employee(team_id, employee["id"])
        brief = self._brief(team, employee, inbox, cards, reviews)
        error = ""
        try:
            out = self._invoke(team, employee, brief)
        except Exception as e:  # noqa: BLE001 单个员工失败不拖垮团队
            out, error = {}, str(e)[:300]
        finally:
            self._tls.actor = None
        for message in inbox:
            self.bus.mark_read(message["seq"], employee["id"])
        summary = _clip(out.get("text") or "", SUMMARY_CHARS)
        if actor.posted == 0:
            self._post(actor, kind="response",
                       subject=_clip(summary.splitlines()[0] if summary else
                                     f"{employee['name']} 本轮无产出", 60),
                       text=summary or out.get("error") or error or "（无内容）",
                       to=self._next_role(team_id, employee["role"]),
                       parent_seq=(inbox[-1]["seq"] if inbox else None))
        self._reconcile(actor, cards, reviews,
                        summary or "（本轮无文字小结）",
                        errored=bool(error or out.get("error")))
        self.teams.set_employee(
            employee["id"], status="blocked" if error else "idle",
            turns=int(employee["turns"]) + 1, messages_sent=actor.posted,
            produced={"last_summary": summary, "last_at": _now(),
                      "mode": out.get("mode") or "react",
                      "steps": (out.get("steps") or [])[-12:]})
        self._emit_employee(team_id, employee["id"])
        if error:
            self._post(actor, kind="request", subject="执行受阻，需要人类判断",
                       text=f"这轮我出错了：{error}\n请指示继续还是换方向。",
                       to=HUMAN_ACTOR)
        return {"error": error}

    def _invoke(self, team: dict, employee: dict, brief: str) -> dict:
        if not self._llm_ready():
            return self._fallback(team, employee)
        agent = self._agent_for(team, employee)
        # 会话按轮次隔离：外部运行可能超过单轮等待，复用同会话会 409 占用冲突
        out = self._team_runtime().run(
            agent, brief,
            session_id=f"team:{team['id']}:{employee['id']}:r{int(team['round_no']) + 1}")
        if out.get("error"):
            merged = self._fallback(team, employee)
            merged["steps"] = (out.get("steps") or []) + (merged.get("steps") or [])
            return merged
        return out

    def _llm_ready(self) -> bool:
        cfg = self.runtime.llm_cfg or {}
        return bool(cfg.get("enabled") and cfg.get("api_key"))

    def _agent_for(self, team: dict, employee: dict) -> dict:
        """已发布的同岗位智能体资产优先（人改过提示词/能力），否则用岗位模板实例。"""
        spec = build_agent_spec(employee["role"], team["task"], employee["focus"])
        spec["id"] = employee["agent_id"] or spec["id"]
        spec["name"] = employee["name"]
        if self.governance is not None:
            released = self.governance.get_release(team["workspace_id"], "agent",
                                                   spec["id"]) or {}
            snapshot = released.get("snapshot")
            if isinstance(snapshot, dict) and snapshot.get("system"):
                spec = {**spec, **snapshot, "id": spec["id"], "name": employee["name"],
                        "tool_ids": sorted(set(list(snapshot.get("tool_ids") or [])
                                               + list(TEAM_TOOLS)))}
        return spec

    def _team_runtime(self) -> AgentRuntime:
        """协作工具是团队作用域的：复制基础工具表并挂上当前执行身份。"""
        base = self.runtime.tools
        reg = ToolRegistry()
        for name in base.names():
            tool = base.get(name)
            if tool and name not in TEAM_TOOLS:
                reg.register(tool.name, tool.description, tool.fn, tool.parameters)
        self._register_team_tools(reg)
        runtime = self.runtime
        return AgentRuntime(runtime.llm_cfg, reg, skills=runtime.skills,
                            memory=runtime.memory, kb=runtime.kb, mcp=runtime.mcp,
                            obs=runtime.obs, flow_invoker=runtime.invoke_flow,
                            credential_resolver=runtime.agent_rt.credential_resolver,
                            bridge_cfg=runtime.bridge_cfg)

    # ------------------------------------------------------------------ 提示词
    def _brief(self, team: dict, employee: dict, inbox: list[dict],
               cards: list[dict], reviews: list[dict]) -> str:
        role = ROLE_TEMPLATES[employee["role"]]
        lines = [f"# 任务目标\n{team['task'][:1200]}",
                 f"# 你的岗位\n{role['name']}：{role['charter']}",
                 f"# 本轮：第 {team['round_no'] + 1} 轮"]
        if cards:
            lines.append("# 你名下的卡片（board_move 推进，board_comment 写结论）")
            lines += [f"- {c['id']} [{c['status']}] {c['title']}"
                      + (f"（依赖 {'、'.join(c['depends_on'])}）" if c["depends_on"]
                         else "") for c in cards]
        if reviews:
            lines.append("# 等你验收的上游卡片（认可就 board_move 到 done 并发 review 消息）")
            lines += [f"- {c['id']} {c['title']}（{c['role']} 交付）" for c in reviews]
        if inbox:
            lines.append("# 收件箱未读（逐条回应）")
            for m in inbox:
                lines.append(
                    f"- #{m['seq']} {self._label(team['id'], m['from_actor'])} → "
                    f"{self._label(team['id'], m['to_actor'])}"
                    f"（{m['kind']}）{m['subject']}\n  "
                    f"{_clip((m['body'] or {}).get('text'), 500)}")
        upstream = self._upstream_outputs(team["id"], employee["role"])
        if upstream:
            lines.append("# 上游已交付内容（作为你的输入）")
            lines += [f"- {u['label']}：{_clip(u['text'], 400)}" for u in upstream]
        lines.append("# 现在推进你的下一步，并按协作协议通信")
        return "\n".join(lines)

    def _upstream_outputs(self, team_id: str, role: str) -> list[dict]:
        out = []
        for card in self.board.list(team_id):
            if _index(card["role"]) >= _index(role) \
                    or card["status"] not in ("review", "done"):
                continue
            text = str((card["output"] or {}).get("deliverable") or "")
            if text:
                name = ROLE_TEMPLATES.get(card["role"], {}).get("name", card["role"])
                out.append({"label": f"{name}·{card['title']}", "text": text})
        return out[:6]

    # ------------------------------------------------------------------ 降级
    def _fallback(self, team: dict, employee: dict) -> dict:
        """无 LLM：按岗位产出结构化交付物，仍真实写看板、发总线消息。"""
        actor = self._current_actor(team["id"], employee)
        role = ROLE_TEMPLATES[employee["role"]]
        refs = "；".join(_clip(u["label"], 40)
                         for u in self._upstream_outputs(team["id"], employee["role"])
                         [:3]) or "（无上游）"
        shape = {
            "product": ["目标用户与核心场景", "范围内功能清单（P0/P1）", "明确不做什么",
                        "验收标准"],
            "architect": ["分层与模块划分", "数据模型与关键字段", "对外接口契约",
                          "技术选型与取舍", "主要风险与对策"],
            "developer": ["改动清单（按模块）", "关键实现思路", "接口与数据落地顺序",
                          "自测点与联调依赖"],
            "qa": ["用例矩阵（正常/边界/异常）", "回归范围", "放行标准",
                   "当前结论"],
            "ops": ["部署拓扑", "发布与回滚步骤", "监控与告警项", "容量与故障预案"],
            "sales": ["目标客户与决策人", "核心卖点三条", "差异化对比",
                      "常见异议与应对", "首版话术"],
        }.get(employee["role"], ["结论要点"])
        deliverable = (
            f"【{role['name']}交付｜规则降级产出，未调用大模型】\n"
            f"任务：{_clip(team['task'], 200)}\n输入来源：{refs}\n"
            + "\n".join(f"{i}. {item}" for i, item in enumerate(shape, 1))
            + "\n说明：本轮按岗位模板给出可核对的结构化结论；配置 LLM 后同一岗位"
              "会用真实推理替换本产出，协作链路与看板/消息形态不变。")
        cards = 0
        for card in self._actionable_cards(team["id"], employee["role"]):
            if card["status"] == "backlog":
                self._move(actor, card["id"], "doing", "开工")
            self._comment(actor, card["id"], deliverable)
            self._move(actor, card["id"], "review", "交付待验收")
            cards += 1
        for card in self._review_queue(team["id"], employee["role"])[:3]:
            self._move(actor, card["id"], "done", f"{employee['name']}验收通过")
            self._post(actor, kind="review",
                       subject=f"验收通过：{card['title'][:60]}",
                       text="上游交付我认可，已置为完成。",
                       to=card["created_by"] or BROADCAST)
        self._post(actor, kind="deliver", subject=f"{role['name']}交付完成",
                   text=deliverable, to=self._next_role(team["id"], employee["role"]))
        self._post(actor, kind="announce", subject=f"{role['name']}本轮完成",
                   text=_clip(deliverable, 200), to=BROADCAST)
        return {"text": _clip(deliverable, SUMMARY_CHARS), "mode": "fallback",
                "steps": [{"type": "fallback", "name": "rule_deliverable",
                           "args": {"role": employee["role"]}, "ok": True, "ms": 0,
                           "result": f"规则产出 {cards} 张卡，{actor.posted} 条消息"}]}

    def _current_actor(self, team_id: str, employee: dict) -> Actor:
        existing = getattr(self._tls, "actor", None)
        if existing is not None:
            return existing
        team = self._require_team(team_id)
        return Actor(workspace_id=team["workspace_id"], team_id=team_id,
                     employee_id=employee["id"], role=employee["role"],
                     name=employee["name"], round_no=int(team["round_no"]))

    # ------------------------------------------------------------------ 收敛
    def _reconcile(self, actor: Actor, cards: list[dict], reviews: list[dict],
                   summary: str, errored: bool = False) -> None:
        """保证「有产出必有痕迹」：模型没写看板时由驱动补齐，避免空转。"""
        working = next((c for c in cards if c["status"] in ("doing", "blocked")), None)
        if working and working["id"] not in actor.touched:
            self.board.set_output(working["id"], actor.employee_id,
                                 {"deliverable": summary, "source": "driver"})
            self._move(actor, working["id"], "review", "本轮产出待验收")
        elif working is None and summary and not errored:
            # 外部员工（Customer Agent 等）没有看板工具：有产出但未触卡时，
            # 驱动按合法迁移代推最老的未触卡（backlog→doing→review）。
            pending = next((c for c in cards if c["status"] in ("backlog", "doing")
                            and c["id"] not in actor.touched), None)
            if pending is not None:
                self.board.set_output(pending["id"], actor.employee_id,
                                      {"deliverable": summary, "source": "driver"})
                if pending["status"] == "backlog":
                    self._move(actor, pending["id"], "doing", "本轮产出驱动开工")
                self._move(actor, pending["id"], "review", "本轮产出待验收")
        for card in reviews:
            if card["id"] not in actor.touched and not card["output"].get("deliverable"):
                continue
            if card["id"] in actor.touched:
                continue
            if summary and not card["output"].get("accepted_by"):
                self._move(actor, card["id"], "done",
                           f"{actor.name}验收通过：{_clip(summary, 80)}")

    def _sweep_review(self, team_id: str, round_no: int,
                      since: str) -> int:
        """验收兜底：卡在 review 超过一轮仍未被下游处理时，交给下游（末端交产品经理）放行。"""
        employees = {e["role"]: e for e in self.teams.employees(team_id)}
        accepted = 0
        for card in self.board.list(team_id, status="review"):
            if card["updated_at"] > since:
                continue
            reviewer = employees.get(self._next_role(team_id, card["role"])) \
                or employees.get("product")
            if reviewer is None or reviewer["role"] == card["role"]:
                continue
            team = self._require_team(team_id)
            actor = Actor(workspace_id=team["workspace_id"], team_id=team_id,
                          employee_id=reviewer["id"], role=reviewer["role"],
                          name=reviewer["name"], round_no=round_no)
            self.board.set_output(card["id"], reviewer["id"],
                                  {"accepted_by": reviewer["id"],
                                   "accepted_at": _now(), "auto": True})
            self._move(actor, card["id"], "done", "超出一轮未处理，按默认放行")
            self._post(actor, kind="review",
                       subject=f"验收通过：{card['title'][:60]}",
                       text="这张卡在我的队列里超过一轮，已按默认放行；"
                            "如有异议可退回 review。",
                       to=card["created_by"] or BROADCAST)
            accepted += 1
        return accepted

    def _next_role(self, team_id: str, role: str) -> str:
        live = {e["role"] for e in self.teams.employees(team_id)}
        for key in CHAIN_ORDER[_index(role) + 1:]:
            if key in live:
                return key
        return "product" if role != "product" and "product" in live else HUMAN_ACTOR

    def _converge(self, team_id: str, detail: dict, round_no: int) -> None:
        progress = self.board.progress(team_id)
        if not progress["total"] or progress["done_ratio"] < 1.0:
            return
        team = self._require_team(team_id)
        summary = self._summary(team, progress)
        self.teams.set_team_status(team_id, "done", summary=summary)
        self.bus.post(workspace_id=team["workspace_id"], team_id=team_id,
                      from_actor="crew", to_actor=HUMAN_ACTOR, kind="announce",
                      subject="任务交付完成", body={"text": summary,
                                                  "progress": progress},
                      round_no=round_no)
        self._audit("workforce.deliver.summary", team, "crew",
                    {"round_no": round_no, "cards": progress["total"]})
        self._emit_team(team_id)
        detail["terminal"] = True

    def _summary(self, team: dict, progress: dict) -> str:
        lines = [f"任务「{_clip(team['task'], 80)}」已交付。"]
        for card in self.board.list(team["id"]):
            lines.append(f"- [{card['role']}] {card['title']}："
                         f"{_clip((card['output'] or {}).get('deliverable'), 200)}")
        lines.append(f"{progress['total']} 张卡全部完成，"
                     f"{self.bus.stats(team['id'])['total']} 条消息，"
                     f"跑了 {team['round_no']} 轮。")
        return "\n".join(lines)[:4000]

    # ------------------------------------------------------------------ 协作工具
    def _register_team_tools(self, reg: ToolRegistry) -> None:
        def who() -> Actor:
            actor = getattr(self._tls, "actor", None)
            if actor is None:
                raise RuntimeError("协作工具只能在团队轮次内调用")
            return actor

        reg.register("board_list", "查看团队共享看板（卡片与进度）",
                     lambda a: {"cards": [
                         {k: c[k] for k in ("id", "title", "role", "status",
                                            "depends_on", "output")}
                         for c in self.board.list(
                             who().team_id,
                             role=str(a.get("role") or "").strip() or None)],
                         "progress": self.board.progress(who().team_id)},
                     {"type": "object", "properties": {
                         "role": {"type": "string", "description": "只看某岗位，可留空"}}})
        reg.register("bus_read", "读你的收件箱（同事发给你的消息）",
                     lambda a: {"messages": self.bus.inbox(
                         who().team_id, who().employee_id,
                         unread_only=bool(a.get("unread_only", True)),
                         limit=int(a.get("limit") or INBOX_ITEMS))},
                     {"type": "object", "properties": {
                         "unread_only": {"type": "boolean"},
                         "limit": {"type": "number"}}})
        reg.register("bus_post", "给员工/岗位、老板(human)或全员(*)发消息",
                     lambda a: self._tool_post(who(), a),
                     {"type": "object", "properties": {
                         "to": {"type": "string", "description":
                                "员工 id、岗位 key（如 developer）、human 或 *"},
                         "kind": {"type": "string", "enum": list(KINDS)},
                         "subject": {"type": "string"}, "text": {"type": "string"},
                         "parent_seq": {"type": "number",
                                        "description": "回复某条消息，可选"}},
                         "required": ["to", "subject", "text"]})
        reg.register("board_move", "推进看板卡片（受状态机与依赖约束）",
                     lambda a: self._tool_move(who(), a),
                     {"type": "object", "properties": {
                         "card_id": {"type": "string"},
                         "to": {"type": "string", "enum": [
                             "backlog", "doing", "review", "done", "blocked"]},
                         "reason": {"type": "string"}},
                         "required": ["card_id", "to"]})
        reg.register("board_add", "新建一张任务卡并分派给某岗位",
                     lambda a: self._tool_add(who(), a),
                     {"type": "object", "properties": {
                         "title": {"type": "string"},
                         "role": {"type": "string", "description": "岗位 key，默认你自己"},
                         "detail": {"type": "string"},
                         "depends_on": {"type": "array", "items": {"type": "string"}}},
                         "required": ["title"]})
        reg.register("board_comment", "给卡片补一条可核对的结论",
                     lambda a: self._tool_comment(who(), a),
                     {"type": "object", "properties": {
                         "card_id": {"type": "string"}, "text": {"type": "string"}},
                         "required": ["card_id", "text"]})

    def _tool_post(self, actor: Actor, args: dict) -> dict:
        to = self._resolve_recipient(actor.team_id, args.get("to"))
        message = self._post(actor, kind=str(args.get("kind") or "response"),
                             subject=str(args.get("subject") or "来自同事的消息"),
                             text=str(args.get("text") or ""), to=to,
                             parent_seq=(int(args["parent_seq"])
                                         if args.get("parent_seq") else None))
        return {"ok": True, "seq": message["seq"], "to": to}

    def _tool_move(self, actor: Actor, args: dict) -> dict:
        card = self.board.get(str(args.get("card_id") or ""))
        if card is None or card["team_id"] != actor.team_id:
            raise BoardError("card_not_found", "卡片不存在或不属于本团队")
        if card["role"] != actor.role and not self._may_review(actor, card):
            raise BoardError("card_owner_mismatch",
                             f"这张卡属于 {card['role']}，你没有验收权限")
        moved = self._move(actor, card["id"], str(args.get("to") or ""),
                           str(args.get("reason") or ""))
        return {"ok": True, "card": {k: moved[k] for k in ("id", "title", "status")}}

    def _tool_add(self, actor: Actor, args: dict) -> dict:
        role = str(args.get("role") or actor.role)
        if role not in ROLE_TEMPLATES:
            raise BoardError("role_unknown", f"未知岗位：{role}")
        card = self.board.add(
            workspace_id=actor.workspace_id, team_id=actor.team_id,
            title=str(args.get("title") or ""), role=role,
            detail=str(args.get("detail") or ""),
            depends_on=[str(d) for d in (args.get("depends_on") or [])],
            created_by=actor.employee_id)
        actor.touched.append(card["id"])
        self._post(actor, kind="assign", subject=f"新增任务卡：{card['title'][:60]}",
                   text=str(args.get("detail") or "")[:500] or "见看板",
                   to={"to": role} if role != actor.role else {"to": BROADCAST})
        return {"ok": True, "card_id": card["id"]}

    def _tool_comment(self, actor: Actor, args: dict) -> dict:
        card = self.board.get(str(args.get("card_id") or ""))
        if card is None or card["team_id"] != actor.team_id:
            raise BoardError("card_not_found", "卡片不存在或不属于本团队")
        text = str(args.get("text") or "")[:OUTPUT_CHARS]
        if not text:
            raise BoardError("card_comment_empty", "评论内容为空")
        self.board.comment(card["id"], actor.employee_id, text)
        self.board.set_output(card["id"], actor.employee_id, {"deliverable": text})
        actor.touched.append(card["id"])
        return {"ok": True}

    def _may_review(self, actor: Actor, card: dict) -> bool:
        return (card["status"] == "review"
                and _index(actor.role) > _index(card["role"])
                and actor.role in {e["role"] for e in self.teams.employees(card["team_id"])})

    # ------------------------------------------------------------------ 写动作
    def _post(self, actor: Actor, *, kind: str, subject: str, text: str,
              to: dict | str | None = None, parent_seq: int | None = None) -> dict:
        target = to if isinstance(to, dict) else {"to": str(to or BROADCAST)}
        recipient = str(target.get("to") or BROADCAST)
        if recipient not in (BROADCAST, HUMAN_ACTOR):
            found = self.teams.find_employee(actor.team_id, recipient)
            recipient = found["id"] if found else BROADCAST
        message = self.bus.post(
            workspace_id=actor.workspace_id, team_id=actor.team_id,
            from_actor=actor.employee_id, to_actor=recipient,
            kind=kind if kind in KINDS else "response", subject=subject,
            body={"text": text, "from_role": actor.role},
            parent_seq=parent_seq, round_no=actor.round_no)
        actor.posted += 1
        self._audit("workforce.message.post", self._require_team(actor.team_id),
                    actor.employee_id,
                    {"kind": message["kind"], "to": recipient, "seq": message["seq"]})
        return message

    def _move(self, actor: Actor, card_id: str, to: str, reason: str) -> dict:
        moved = self.board.move(card_id, to, actor.employee_id, reason)
        actor.moved += 1
        actor.touched.append(card_id)
        self._audit("workforce.card.move", self._require_team(actor.team_id),
                    actor.employee_id,
                    {"card_id": card_id, "to": to, "reason": reason[:200]})
        return moved

    def _comment(self, actor: Actor, card_id: str, text: str) -> None:
        body = text[:OUTPUT_CHARS]
        self.board.comment(card_id, actor.employee_id, body)
        self.board.set_output(card_id, actor.employee_id, {"deliverable": body})
        actor.touched.append(card_id)

    def _notify(self, team: dict, *, kind: str, subject: str, text: str,
                to: str) -> dict:
        message = self.bus.post(workspace_id=team["workspace_id"], team_id=team["id"],
                                from_actor="crew", to_actor=to,
                                kind=kind if kind in KINDS else "request",
                                subject=subject, body={"text": text},
                                round_no=int(team["round_no"]))
        self._emit_team(team["id"])
        return message

    def _resolve_recipient(self, team_id: str, token) -> dict:
        raw = str(token or "").strip()
        if raw in ("", "*", "all", "全员"):
            return {"to": BROADCAST}
        if raw in (HUMAN_ACTOR, "老板", "human:boss"):
            return {"to": HUMAN_ACTOR}
        found = self.teams.find_employee(team_id, raw)
        if found:
            return {"to": found["id"], "role": found["role"]}
        if raw in ROLE_TEMPLATES:
            raise BoardError("recipient_not_in_team",
                             f"该岗位 {raw} 不在本团队编制内")
        raise BoardError("recipient_unknown",
                         f"收件人不存在：{raw}（可用岗位 key、员工 id、human 或 *）")

    def _label(self, team_id: str, actor_id: str) -> str:
        if actor_id == HUMAN_ACTOR:
            return "老板"
        if actor_id == "crew":
            return "调度器"
        if actor_id == BROADCAST:
            return "全员"
        found = self.teams.find_employee(team_id, actor_id)
        return found["name"] if found else actor_id

    def _actionable_cards(self, team_id: str, role: str) -> list[dict]:
        out = []
        for card in self.board.list(team_id, role=role):
            if card["status"] in ("doing", "blocked"):
                out.append(card)
            elif card["status"] == "backlog" and all(
                    (self.board.get(dep) or {}).get("status") == "done"
                    for dep in card["depends_on"]):
                out.append(card)
        return out[:BOARD_ITEMS]

    def _review_queue(self, team_id: str, role: str) -> list[dict]:
        employees = {e["role"] for e in self.teams.employees(team_id)}
        out = []
        for card in self.board.list(team_id, status="review"):
            reviewer = self._next_role(team_id, card["role"])
            if card["role"] == role or reviewer != role or role not in employees:
                continue
            if card["output"].get("accepted_by"):
                continue
            out.append(card)
        return out[:BOARD_ITEMS]

    # ------------------------------------------------------------------ 事件
    def _emit_team(self, team_id: str) -> None:
        team = self._require_team(team_id)
        self.hub.publish({"type": "team", "workspace_id": team["workspace_id"],
                          "team_id": team_id, "id": f"team:{team_id}",
                          "payload": {"status": team["status"],
                                      "round_no": team["round_no"],
                                      "summary": team["summary"]}})

    def _emit_employee(self, team_id: str, employee_id: str) -> None:
        employee = self.teams.get_employee(employee_id)
        if employee is None:
            return
        team = self._require_team(team_id)
        self.hub.publish({"type": "employee", "workspace_id": team["workspace_id"],
                          "team_id": team_id, "id": employee_id,
                          "payload": {k: employee[k] for k in
                                      ("id", "status", "turns", "messages_sent",
                                       "last_active_at", "produced")}})

    # ------------------------------------------------------------------ 校验
    def _require_team(self, team_id: str) -> dict:
        team = self.teams.get_team(team_id)
        if team is None:
            raise CrewError("team_not_found", f"团队不存在：{team_id}")
        return team

    def _audit(self, event_type: str, team: dict, actor: str, details: dict,
               outcome: str = "success") -> None:
        if self.governance is None:
            return
        self.governance.audit(event_type, workspace_id=team["workspace_id"],
                              username=str(actor or "crew"),
                              resource_type="workforce", resource_id=team["id"],
                              outcome=outcome, details=details)
