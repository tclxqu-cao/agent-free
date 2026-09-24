"""平台智能体：可创建的 Agent 资产 + 自包含 ReAct 执行循环（不依赖外部运行时）。

Agent 是中心资产（Dify 式模型）：知识库 / 记忆 / 技能 / 工具 / MCP 都配对在
agent 身上；编排方式二选一——
- 对话式（默认）：ReAct 工具循环，模型自主决定何时检索 / 调工具；
- 流程编排：绑定一条画布流程作为执行策略，invoke 时按流程逐步执行
  （画布因此也是 agent 的一环；流程里再用 ai_agent 节点即可组装多智能体）。

流程里引用智能体走 ai_agent 节点；嵌套深度有护栏，互相引用不会打穿。
team-agent 等外部运行时仍走 bridge（brain 节点），三者互不影响。
"""

from __future__ import annotations

import datetime as dt
import copy
import json
import logging
import os
import threading
import uuid
from pathlib import Path

from .agent_defaults import load_default_agents
from .llm import llm_messages_raw
from .external_agent import (CustomerAgentProvider, resolve_external_agent_token,
                             validate_provider_url)
from .mcp_client import MCPError
from .tools import ToolRegistry

log = logging.getLogger(__name__)

SNIPPET = 160          # 步骤记录里工具结果的截断长度
SKILL_CHARS = 6000     # 技能注入 system 的总字符上限
MEMORY_ITEMS = 10      # 注入 system 的记忆条数上限
RAG_TOP_K = 4          # RAG 默认检索片段数
RAG_CHARS = 4500       # RAG 片段注入总字符上限
MAX_NESTING = 3        # agent ↔ 流程 互相引用的最大嵌套深度


class AgentStore:
    """智能体资产：JSON 文件存储 + CRUD + 内置示例。"""

    def __init__(self, base: Path):
        self.dir = Path(base)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, agent_id: str) -> Path:
        safe = "".join(c for c in agent_id if c.isalnum() or c in "-_")
        if not safe or safe != agent_id:
            raise ValueError(f"非法智能体 id：{agent_id!r}（只允许字母数字-_）")
        return self.dir / f"{safe}.json"

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                out.append(_meta(data))
            except Exception:  # noqa: BLE001
                continue
        return out

    def get(self, agent_id: str) -> dict | None:
        p = self.dir / f"{agent_id}.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def save(self, data: dict) -> dict:
        merged = normalize_agent(data)
        agent_id = merged["id"]
        path = self._path(agent_id)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return merged

    def delete(self, agent_id: str) -> bool:
        p = self.dir / f"{agent_id}.json"
        if not p.exists():
            return False
        p.unlink()
        return True

    def seed_if_empty(self, agents: list[dict]) -> int:
        if any(self.dir.glob("*.json")):
            return 0
        for a in agents:
            self.save(a)
        return len(agents)

    def seed_missing(self, agents: list[dict]) -> int:
        """Add missing defaults and upgrade legacy external records once."""
        count = 0
        for agent in agents:
            agent_id = str(agent.get("id") or "")
            existing = self.get(agent_id) if agent_id else None
            if agent_id and existing is None:
                self.save(agent)
                count += 1
            elif _needs_external_upgrade(existing, agent):
                self.save(_upgrade_external_agent(existing, agent))
        return count


def _meta(data: dict) -> dict:
    meta = {k: data.get(k) for k in
            ("id", "name", "description", "flow_id", "kb_ids", "skill_ids",
             "tool_ids", "mcp_servers", "memory", "max_steps", "rag_top_k",
             "runtime", "orchestration", "updated_at")}
    meta["profile_id"] = str(
        data.get("profile_id") or data.get("ca_profile_id") or "aihub-deepseek")
    return meta


def normalize_agent(data: dict) -> dict:
    """Normalize one Agent without writing it, for governed draft snapshots."""
    agent_id = str(data.get("id") or "").strip()
    if not agent_id:
        raise ValueError("缺少智能体 id")
    raw_orchestration = data.get("orchestration")
    if raw_orchestration is not None and not isinstance(raw_orchestration, dict):
        raise ValueError("orchestration 必须是对象")
    raw_orchestration = raw_orchestration or {}
    mode = str(raw_orchestration.get("mode") or "").strip()
    if not mode:
        mode = ("external_agent" if data.get("runtime") == "customer-agent"
                else "flow" if data.get("flow_id") else "react")
    if mode not in {"react", "external_agent", "flow"}:
        raise ValueError("编排方式必须是 react、external_agent 或 flow")
    skill_ids = _string_ids(data.get("skill_ids"))
    tool_ids = _string_ids(data.get("tool_ids"))
    mcp_servers = _string_ids(data.get("mcp_servers"))
    memory_enabled = bool(data.get("memory"))
    profile_id = str(data.get("profile_id") or data.get("ca_profile_id")
                     or "aihub-deepseek").strip()
    flow_id = str(data.get("flow_id") or "").strip()
    if mode == "external_agent":
        provider = str(raw_orchestration.get("provider") or "customer-agent").strip()
        if provider != "customer-agent":
            raise ValueError(f"暂不支持第三方智能体：{provider}")
        connection = raw_orchestration.get("connection") or {}
        selection = raw_orchestration.get("selection") or {}
        if not isinstance(connection, dict) or not isinstance(selection, dict):
            raise ValueError("第三方智能体 connection/selection 必须是对象")
        base_url = validate_provider_url(
            connection.get("base_url") or data.get("base_url")
            or "http://127.0.0.1:3000")
        credential_ref = str(connection.get("credential_ref")
                             or "customer-agent-default").strip()
        if not credential_ref:
            raise ValueError("第三方智能体 credential_ref 不能为空")
        profile_id = str(selection.get("model_id") or profile_id).strip()
        if not profile_id:
            raise ValueError("第三方智能体必须选择模型")
        skill_ids = _string_ids(selection.get("skill_ids", skill_ids))
        tool_ids = _string_ids(selection.get("tool_ids", tool_ids))
        mcp_servers = _string_ids(
            selection.get("mcp_server_ids", mcp_servers))
        memory_enabled = bool(selection.get("memory_enabled", memory_enabled))
        tool_policy_id = str(selection.get("tool_policy_id") or "").strip()
        orchestration = {
            "mode": "external_agent", "provider": provider,
            "agent_id": str(raw_orchestration.get("agent_id")
                            or data.get("ca_agent_id") or ""),
            "include_identity_instructions": bool(
                raw_orchestration.get("include_identity_instructions", False)),
            "connection": {"base_url": base_url,
                           "credential_ref": credential_ref},
            "selection": {"model_id": profile_id,
                          "skill_ids": skill_ids,
                          "tool_ids": tool_ids,
                          "mcp_server_ids": mcp_servers,
                          "memory_enabled": memory_enabled,
                          **({"tool_policy_id": tool_policy_id}
                             if tool_policy_id else {})},
        }
    elif mode == "flow":
        flow_id = str(raw_orchestration.get("flow_id") or flow_id).strip()
        if not flow_id:
            raise ValueError("流程图编排必须选择流程")
        orchestration = {"mode": "flow", "flow_id": flow_id}
    else:
        orchestration = {"mode": "react"}
    normalized = {**data,
            "id": agent_id,
            "name": str(data.get("name") or agent_id),
            "description": str(data.get("description") or ""),
            "system": str(data.get("system") or "你是一个得力的智能体。"),
            "orchestration": orchestration,
            "runtime": "customer-agent" if mode == "external_agent" else "local",
            "profile_id": profile_id,
            "flow_id": flow_id if mode == "flow" else "",
            "kb_ids": [str(k) for k in (data.get("kb_ids") or [])],
            "skill_ids": skill_ids,
            "tool_ids": tool_ids,
            "mcp_servers": mcp_servers,
            "memory": memory_enabled,
            "max_steps": max(1, min(30, int(data.get("max_steps") or 8))),
            "rag_top_k": max(1, min(20, int(data.get("rag_top_k") or RAG_TOP_K))),
            "updated_at": dt.datetime.now().isoformat(timespec="seconds")}
    normalized.pop("ca_profile_id", None)
    normalized.pop("ca_agent_id", None)
    return normalized


def _string_ids(values) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError("能力选择必须是数组")
    out = []
    for value in values:
        item = str(value or "").strip()
        if not item:
            raise ValueError("能力 ID 不能为空")
        if item not in out:
            out.append(item)
    return out


def _external_agent_instructions(agent: dict) -> str:
    """Build CA instructions without creating or binding a named CA Agent."""
    system = str(agent.get("system") or "")
    orchestration = agent.get("orchestration") or {}
    if not orchestration.get("include_identity_instructions"):
        return system

    name = str(agent.get("name") or agent.get("id") or "").strip()
    parts = [f"智能体名称：{name}"]
    description = str(agent.get("description") or "").strip()
    if description:
        parts.append(f"智能体描述：{description}")
    if system.strip():
        parts.append(f"System 提示词：\n{system.strip()}")
    return "\n\n".join(parts)


def _needs_external_upgrade(existing: dict | None, default: dict) -> bool:
    default_mode = (default.get("orchestration") or {}).get("mode")
    return bool(existing and default_mode == "external_agent"
                and existing.get("runtime") == "customer-agent"
                and not existing.get("orchestration"))


def _upgrade_external_agent(existing: dict, default: dict) -> dict:
    """Translate a legacy external record using its configurable default."""
    upgraded = {**existing, "orchestration": copy.deepcopy(default["orchestration"])}
    orchestration = upgraded["orchestration"]
    selection = orchestration["selection"]
    if str(existing.get("profile_id") or existing.get("ca_profile_id") or "").strip():
        selection["model_id"] = str(
            existing.get("profile_id") or existing.get("ca_profile_id")).strip()
    if existing.get("skill_ids"):
        selection["skill_ids"] = _string_ids(existing["skill_ids"])
    if existing.get("tool_ids"):
        selection["tool_ids"] = _string_ids(existing["tool_ids"])
    if existing.get("mcp_servers"):
        selection["mcp_server_ids"] = _string_ids(existing["mcp_servers"])
    if "memory" in existing:
        selection["memory_enabled"] = bool(existing["memory"])
    existing_selection = ((existing.get("orchestration") or {})
                          .get("selection") or {})
    if str(existing_selection.get("tool_policy_id") or "").strip():
        selection["tool_policy_id"] = str(
            existing_selection["tool_policy_id"]).strip()
    if str(existing.get("ca_agent_id") or "").strip():
        orchestration["agent_id"] = str(existing["ca_agent_id"]).strip()
    return upgraded


DEFAULT_AGENTS = load_default_agents()


class AgentRuntime:
    """执行智能体：装配工具 → ReAct 循环 → 返回 {text, steps, …}。

    llm_fn 可注入（测试用）；默认 llm.messages_raw（OpenAI 兼容，
    返回 (entries, usage)。测试注入的旧式单条 entry 返回也兼容）。
    obs 为可观测性 observer（None = 不上报）。
    """

    def __init__(self, llm_cfg: dict, base_tools: ToolRegistry,
                 skills=None, memory=None, kb=None, mcp=None, llm_fn=None,
                 obs=None, flow_invoker=None, bridge_cfg=None,
                 credential_resolver=None, provider_factory=None):
        self.llm_cfg = llm_cfg or {}
        self.base_tools = base_tools
        self.skills = skills
        self.memory = memory
        self.kb = kb
        self.mcp = mcp
        self.obs = obs
        # 流程编排：invoke(flow_id, inputs) -> run dict；由 server 注入
        self.flow_invoker = flow_invoker
        self.bridge_cfg = bridge_cfg or {}
        self.credential_resolver = credential_resolver or (lambda _ref: "")
        self.provider_factory = provider_factory or CustomerAgentProvider
        self._tls = threading.local()   # 嵌套深度护栏（线程相关）
        self._llm_fn = llm_fn or (lambda cfg, msgs, tools:
                                  llm_messages_raw(cfg, msgs, tools))

    # ---------------------------------------------------------------- 工具装配
    def _toolset(self, agent: dict) -> ToolRegistry:
        reg = ToolRegistry()
        for name in agent.get("tool_ids") or []:
            tool = self.base_tools.get(name)
            if tool:
                reg.register(tool.name, tool.description, tool.fn, tool.parameters)
        if self.kb is not None and agent.get("kb_ids"):
            kbs = list(agent["kb_ids"])
            reg.register(
                "kb_search", "在绑定的知识库里检索资料片段",
                lambda a: {"results": self.kb.search(
                    str(a.get("query") or ""), kb_ids=kbs,
                    top_k=int(a.get("top_k") or 5))},
                {"type": "object",
                 "properties": {"query": {"type": "string"},
                                "top_k": {"type": "number"}},
                 "required": ["query"]})
        return reg

    def _mcp_manifest(self, agent: dict) -> list[dict]:
        """把 MCP 服务的工具包成 manifest 条目，名称加服务前缀防冲突。"""
        out = []
        for server in agent.get("mcp_servers") or []:
            if not self.mcp:
                continue
            try:
                for t in self.mcp.list_tools(server):
                    out.append({"type": "function", "function": {
                        "name": f"mcp__{server}__{t.get('name')}",
                        "description": (t.get("description")
                                        or f"MCP {server} 的 {t.get('name')}"),
                        "parameters": t.get("inputSchema") or {
                            "type": "object", "properties": {}}}})
            except MCPError:
                log.exception("MCP 工具发现失败，跳过服务：%s", server)
                continue     # 服务不可用不阻塞智能体启动
        return out

    def _call_mcp(self, agent: dict, full_name: str, args: dict) -> dict:
        _, server, tool = full_name.split("__", 2)
        if not self.mcp:
            raise MCPError("MCP 未配置")
        return self.mcp.call(server, tool, args)

    # ---------------------------------------------------------------- 提示词
    def _system_prompt(self, agent: dict) -> str:
        parts = [agent.get("system") or "你是一个得力的智能体。",
                 f"今天是 {dt.date.today().isoformat()}。"]
        if self.skills:
            budget, used = SKILL_CHARS, []
            for sid in agent.get("skill_ids") or []:
                skill = self.skills.get(sid)
                if not skill:
                    continue
                block = f"### 技能：{skill['name']}\n{skill['content']}"
                if len(block) > budget:
                    break
                used.append(block)
                budget -= len(block)
            if used:
                parts.append("以下是已装配的技能指令，回答相关问题时遵循：\n\n"
                             + "\n\n".join(used))
        if self.memory is not None and agent.get("memory"):
            items = self.memory.list(scope=f"agent:{agent['id']}",
                                     limit=MEMORY_ITEMS)
            if items:
                lines = [f"- {it['key']}: {json.dumps(it['value'], ensure_ascii=False)[:200]}"
                         for it in items]
                parts.append("已知的长期记忆（agent 作用域）：\n" + "\n".join(lines))
        parts.append("需要资料 / 计算 / 存取记忆时调用工具；"
                     "信息足够后直接给出最终回答（不要再调用工具）。")
        return "\n\n".join(parts)

    # ---------------------------------------------------------------- 主循环
    def _rag_context(self, agent: dict, message: str) -> list[dict]:
        """RAG：按用户消息检索绑定知识库，返回 top-k 片段（检索失败返回空）。"""
        if self.kb is None or not agent.get("kb_ids"):
            return []
        try:
            log.info("智能体开始检索知识库：%s 个知识库", len(agent["kb_ids"]))
            chunks = self.kb.search(message, kb_ids=list(agent["kb_ids"]),
                                    top_k=int(agent.get("rag_top_k") or RAG_TOP_K))
            log.info("知识库检索完成：命中 %s 个片段", len(chunks))
            return chunks
        except Exception:  # noqa: BLE001 检索故障不阻塞对话
            log.exception("知识库检索失败，继续执行智能体")
            return []

    def _run_via_flow(self, agent: dict, message: str,
                      session_id: str) -> dict:
        """流程编排：把消息交给绑定的画布流程执行，返回流程输出。

        深度护栏：流程里可再用 ai_agent 节点（多智能体组装），但互相引用
        不能超过 MAX_NESTING 层，防止 agent ↔ 流程 打穿。
        """
        depth = getattr(self._tls, "depth", 0)
        if depth >= MAX_NESTING:
            log.error("智能体流程嵌套超过 %s 层，停止执行", MAX_NESTING)
            return {"text": "", "steps": [], "session_id": session_id,
                    "error": f"流程嵌套超过 {MAX_NESTING} 层"
                             f"（agent ↔ 流程互相引用？）"}
        self._tls.depth = depth + 1
        try:
            log.info("智能体开始执行绑定流程：%s", agent["flow_id"])
            run = self.flow_invoker(agent["flow_id"],
                                    {"message": message,
                                     "session_id": session_id})
        except Exception as e:  # noqa: BLE001
            log.exception("智能体绑定流程执行失败：%s", agent["flow_id"])
            return {"text": "", "steps": [], "session_id": session_id,
                    "error": f"流程执行失败：{e}"}
        finally:
            self._tls.depth = depth
        steps = [{"type": "flow", "name": f"flow:{n.get('label')}",
                  "args": {"node_id": n.get("node_id")},
                  "ok": n.get("status") == "success",
                  "ms": n.get("ms", 0),
                  "result": n.get("error") or
                            json.dumps(n.get("output"), ensure_ascii=False)[:SNIPPET]}
                 for n in run.get("node_runs") or []]
        text = run.get("output") or ""
        error = None
        if run.get("status") != "success":
            error = run.get("error") or "流程执行失败"
            log.warning("智能体绑定流程结束：%s，状态=%s",
                        agent["flow_id"], run.get("status"))
        else:
            log.info("智能体绑定流程完成：%s", agent["flow_id"])
        out = {"text": text, "steps": steps, "session_id": session_id,
               "tool_calls": 0, "mode": "flow"}
        if error:
            out["error"] = error
        if self.obs is not None:
            try:
                self.obs.agent_run(agent, message, out, session_id=session_id,
                                   llm_calls=[])
            except Exception:  # noqa: BLE001
                log.exception("智能体观测记录写入失败")
        return out

    def run(self, agent: dict, message: str,
            session_id: str | None = None,
            flow_run_id: str | None = None,
            skill_id: str | None = None,
            context: dict | None = None) -> dict:
        explicit_session_id = str(session_id or "").strip()
        session_id = explicit_session_id or uuid4_hex()
        log.info("智能体开始：%s，session_id=%s", agent.get("id"), session_id)

        orchestration = agent.get("orchestration") or {}
        mode = orchestration.get("mode") or (
            "external_agent" if agent.get("runtime") == "customer-agent"
            else "flow" if agent.get("flow_id") else "react")
        if mode == "external_agent":
            requested_skill = str(skill_id or "").strip().lower()
            connection = orchestration.get("connection") or {}
            selection = orchestration.get("selection") or {}
            allowed = set(selection.get("skill_ids") or agent.get("skill_ids") or [])
            if requested_skill and requested_skill not in allowed:
                return {"text": "", "steps": [], "session_id": session_id,
                        "error": f"Skill 未绑定到当前智能体：{requested_skill}"}
            profile_id = str(selection.get("model_id") or agent.get("profile_id")
                             or "aihub-deepseek")
            skill_ids = list(selection.get("skill_ids") or agent.get("skill_ids") or [])
            if requested_skill and requested_skill not in skill_ids:
                skill_ids.append(requested_skill)
            credential_ref = str(connection.get("credential_ref")
                                 or "customer-agent-default")
            token = str(self.credential_resolver(credential_ref) or "")
            if not token:
                token = resolve_external_agent_token(self.bridge_cfg)
            provider = self.provider_factory(
                connection.get("base_url") or self.bridge_cfg.get("base_url")
                or "http://127.0.0.1:3000", token,
                timeout=float(self.bridge_cfg.get("timeout") or 300))
            steps = []

            def observe(event_type, event):
                if event_type == "tool.started":
                    name = str(event.get("name") or "unknown")
                    log.info("Customer Agent 工具开始：%s", name)
                    steps.append({"type": "tool", "name": name,
                                  "args": event.get("arguments") or {},
                                  "ok": True, "ms": 0, "result": "running"})
                elif event_type == "tool.completed":
                    name = next((item["name"] for item in reversed(steps)
                                 if item.get("type") == "tool"), "unknown")
                    failed = event.get("isError") is True
                    if failed:
                        log.warning("Customer Agent 工具失败：%s", name)
                    else:
                        log.info("Customer Agent 工具完成：%s", name)
                    if steps:
                        steps[-1].update(ok=not failed,
                                         result=str(event.get("content") or "")[:SNIPPET])
                elif event_type == "run.started":
                    log.info("Customer Agent 已开始运行")
                elif event_type == "run.failed":
                    log.error("Customer Agent 运行失败：%s", event.get("code"))

            external = provider.run(
                message,
                instructions=_external_agent_instructions(agent),
                agent_id=str(orchestration.get("agent_id") or ""),
                session_id=(session_id if explicit_session_id
                            or selection.get("memory_enabled") else ""),
                context={**(context or {}), "flowRunId": flow_run_id,
                         "agentId": agent.get("id")},
                selection={
                    "modelId": profile_id,
                    "skillIds": skill_ids,
                    "activatedSkillIds": [requested_skill] if requested_skill else [],
                    "toolIds": list(selection.get("tool_ids") or []),
                    "mcpServerIds": list(selection.get("mcp_server_ids") or []),
                    "memoryEnabled": bool(selection.get("memory_enabled")),
                    **({"toolPolicyId": str(selection["tool_policy_id"])}
                       if str(selection.get("tool_policy_id") or "").strip()
                       else {}),
                }, on_event=observe)
            if (requested_skill.startswith("portfolio-")
                    or requested_skill == "homepage-orchestrator"):
                from .homepage_artifacts import validate_artifact
                try:
                    artifact = validate_artifact(
                        json.loads(external.get("text") or "", strict=False),
                        (requested_skill
                         if requested_skill.startswith("portfolio-")
                         else None))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise RuntimeError(
                        "主页智能体没有返回有效内容对象") from exc
                external["artifact"] = artifact
                external["text"] = json.dumps(artifact, ensure_ascii=False)
            log.info("Customer Agent 运行完成：run_id=%s", external.get("run_id"))
            return {**external, "flow_session_id": session_id,
                    "steps": steps, "mode": "customer-agent"}

        # 编排方式 = 流程绑定：按画布流程执行（画布是 agent 的一环）
        if mode == "flow" and agent.get("flow_id") and self.flow_invoker:
            return self._run_via_flow(agent, message, session_id)

        tools = self._toolset(agent)
        manifest = tools.manifest() + self._mcp_manifest(agent)
        system = self._system_prompt(agent)
        steps: list[dict] = []
        llm_calls: list[dict] = []

        # RAG：检索片段直接注入 system（不依赖模型是否主动调 kb_search 工具）
        rag_chunks = self._rag_context(agent, message)
        if rag_chunks:
            budget, blocks = RAG_CHARS, []
            for c in rag_chunks:
                block = f"【{c['name']}】{c['text']}"
                if len(block) > budget:
                    break
                blocks.append(block)
                budget -= len(block)
            if blocks:
                system += ("\n\n以下是知识库中与本问题最相关的资料片段，回答时优先依据：\n"
                           + "\n".join(blocks))
            steps.append({"type": "rag", "name": "kb_rag",
                          "args": {"query": message[:80], "top_k": len(rag_chunks)},
                          "ok": True, "ms": 0,
                          "result": f"命中 {len(rag_chunks)} 个片段："
                                    + "、".join(c["name"] for c in rag_chunks)})

        messages: list[dict] = [{"role": "system", "content": system},
                                {"role": "user", "content": message}]
        text = ""
        max_steps = int(agent.get("max_steps") or 8)
        for step in range(max_steps):
            t0 = dt.datetime.now()
            log.info("智能体 LLM 第 %s/%s 轮开始", step + 1, max_steps)
            try:
                res = self._llm_fn(self.llm_cfg, messages, manifest or None)
            except Exception:
                log.exception("智能体 LLM 第 %s 轮执行异常", step + 1)
                raise
            if isinstance(res, tuple):        # (entries, usage) 新契约
                entries, usage = res[0], res[1]
            else:                             # 测试注入的旧式单条 entry
                entries, usage = res, None
            if entries is None:
                log.warning("智能体 LLM 第 %s 轮无可用回复，耗时 %s ms",
                            step + 1, _since(t0))
                if text:
                    break
                return {"text": "", "steps": steps, "session_id": session_id,
                        "error": "LLM 未启用或调用失败"}
            entry = entries[0] if isinstance(entries, list) else entries
            messages.append(entry)
            calls = entry.get("tool_calls") or []
            log.info("智能体 LLM 第 %s 轮完成：耗时 %s ms，工具调用 %s 个",
                     step + 1, _since(t0), len(calls))
            llm_calls.append({"type": "llm", "ms": _since(t0),
                              "model": self.llm_cfg.get("model"),
                              "usage": usage,
                              "messages": list(messages),
                              "output": entry.get("content"),
                              "ok": True})
            if not calls:
                text = entry.get("content") or ""
                break
            for tc in calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    log.exception("工具参数 JSON 解析失败，使用空参数：%s", name)
                    args = {}
                t0 = dt.datetime.now()
                log.info("智能体工具开始：%s", name)
                try:
                    if name.startswith("mcp__"):
                        out = self._call_mcp(agent, name, args)
                    else:
                        out = tools.call(name, args)
                    result = json.dumps(out, ensure_ascii=False)[:8000] or "{}"
                    ok = True
                    log.info("智能体工具完成：%s，耗时 %s ms", name, _since(t0))
                except Exception as e:  # noqa: BLE001 工具失败回喂模型自行调整
                    log.exception("智能体工具失败：%s，耗时 %s ms", name, _since(t0))
                    out = {}
                    result = json.dumps({"error": str(e)}, ensure_ascii=False)
                    ok = False
                steps.append({"type": "tool", "name": name, "args": args,
                              "ok": ok, "ms": _since(t0),
                              "result": str(out.get("text") or result)[:SNIPPET]})
                messages.append({"role": "tool",
                                 "tool_call_id": tc.get("id") or name,
                                 "content": result})
        else:
            log.warning("智能体达到最大轮数 %s，使用最后一轮输出", max_steps)
            text = entry.get("content") or text   # 步数耗尽用最后一次模型输出
        if self.memory is not None and agent.get("memory"):
            try:
                self.memory.set(f"session:{session_id}", "_last",
                                {"message": message[:2000], "reply": text[:2000]})
                self.memory.set(f"agent:{agent['id']}", "_last_session",
                                session_id)
            except Exception:  # noqa: BLE001 记忆写失败不影响主流程
                log.exception("智能体记忆写入失败，保留本轮执行结果")
        out = {"text": text, "steps": steps, "session_id": session_id,
               "tool_calls": len(steps)}
        if self.obs is not None:
            try:
                self.obs.agent_run(agent, message, out, session_id=session_id,
                                   flow_run_id=flow_run_id, llm_calls=llm_calls)
            except Exception:  # noqa: BLE001 可观测性故障不影响执行
                log.exception("智能体观测记录写入失败")
        log.info("智能体执行完成：%s，LLM 调用 %s 次，工具调用 %s 次",
                 agent.get("id"), len(llm_calls),
                 sum(s.get("type") == "tool" for s in steps))
        return out


def uuid4_hex() -> str:
    return uuid.uuid4().hex[:12]


def _since(t0: dt.datetime) -> int:
    return int((dt.datetime.now() - t0).total_seconds() * 1000)
