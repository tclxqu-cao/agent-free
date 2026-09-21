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
import json
import os
import threading
import uuid
from pathlib import Path

from .llm import llm_messages_raw
from .mcp_client import MCPError
from .tools import ToolRegistry

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


def _meta(data: dict) -> dict:
    return {k: data.get(k) for k in
            ("id", "name", "description", "flow_id", "kb_ids", "skill_ids",
             "tool_ids", "mcp_servers", "memory", "max_steps", "rag_top_k",
             "updated_at")}


def normalize_agent(data: dict) -> dict:
    """Normalize one Agent without writing it, for governed draft snapshots."""
    agent_id = str(data.get("id") or "").strip()
    if not agent_id:
        raise ValueError("缺少智能体 id")
    return {**data,
            "id": agent_id,
            "name": str(data.get("name") or agent_id),
            "description": str(data.get("description") or ""),
            "system": str(data.get("system") or "你是一个得力的智能体。"),
            "flow_id": str(data.get("flow_id") or ""),
            "kb_ids": [str(k) for k in (data.get("kb_ids") or [])],
            "skill_ids": [str(k) for k in (data.get("skill_ids") or [])],
            "tool_ids": [str(k) for k in (data.get("tool_ids") or [])],
            "mcp_servers": [str(k) for k in (data.get("mcp_servers") or [])],
            "memory": bool(data.get("memory")),
            "max_steps": max(1, min(30, int(data.get("max_steps") or 8))),
            "rag_top_k": max(1, min(20, int(data.get("rag_top_k") or RAG_TOP_K))),
            "updated_at": dt.datetime.now().isoformat(timespec="seconds")}


DEFAULT_AGENTS = [
    {
        "id": "kb-assistant",
        "name": "知识助手",
        "description": "绑定知识库与长期记忆的问答智能体（内置示例，可改）",
        "system": ("你是一个严谨的知识助手。优先依据工具检索到的知识库片段回答；"
                   "资料不足时如实说明，不要编造。"),
        "tool_ids": ["kb_search", "memory_save", "memory_load", "now"],
        "memory": True,
        "max_steps": 8,
    },
]


class AgentRuntime:
    """执行智能体：装配工具 → ReAct 循环 → 返回 {text, steps, …}。

    llm_fn 可注入（测试用）；默认 llm.messages_raw（OpenAI 兼容，
    返回 (entries, usage)。测试注入的旧式单条 entry 返回也兼容）。
    obs 为可观测性 observer（None = 不上报）。
    """

    def __init__(self, llm_cfg: dict, base_tools: ToolRegistry,
                 skills=None, memory=None, kb=None, mcp=None, llm_fn=None,
                 obs=None, flow_invoker=None):
        self.llm_cfg = llm_cfg or {}
        self.base_tools = base_tools
        self.skills = skills
        self.memory = memory
        self.kb = kb
        self.mcp = mcp
        self.obs = obs
        # 流程编排：invoke(flow_id, inputs) -> run dict；由 server 注入
        self.flow_invoker = flow_invoker
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
            return self.kb.search(message, kb_ids=list(agent["kb_ids"]),
                                  top_k=int(agent.get("rag_top_k") or RAG_TOP_K))
        except Exception:  # noqa: BLE001 检索故障不阻塞对话
            return []

    def _run_via_flow(self, agent: dict, message: str,
                      session_id: str) -> dict:
        """流程编排：把消息交给绑定的画布流程执行，返回流程输出。

        深度护栏：流程里可再用 ai_agent 节点（多智能体组装），但互相引用
        不能超过 MAX_NESTING 层，防止 agent ↔ 流程 打穿。
        """
        depth = getattr(self._tls, "depth", 0)
        if depth >= MAX_NESTING:
            return {"text": "", "steps": [], "session_id": session_id,
                    "error": f"流程嵌套超过 {MAX_NESTING} 层"
                             f"（agent ↔ 流程互相引用？）"}
        self._tls.depth = depth + 1
        try:
            run = self.flow_invoker(agent["flow_id"],
                                    {"message": message,
                                     "session_id": session_id})
        except Exception as e:  # noqa: BLE001
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
        out = {"text": text, "steps": steps, "session_id": session_id,
               "tool_calls": 0, "mode": "flow"}
        if error:
            out["error"] = error
        if self.obs is not None:
            try:
                self.obs.agent_run(agent, message, out, session_id=session_id,
                                   llm_calls=[])
            except Exception:  # noqa: BLE001
                pass
        return out

    def run(self, agent: dict, message: str,
            session_id: str | None = None,
            flow_run_id: str | None = None) -> dict:
        session_id = session_id or uuid4_hex()

        # 编排方式 = 流程绑定：按画布流程执行（画布是 agent 的一环）
        if agent.get("flow_id") and self.flow_invoker:
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
        for _ in range(max_steps):
            t0 = dt.datetime.now()
            res = self._llm_fn(self.llm_cfg, messages, manifest or None)
            if isinstance(res, tuple):        # (entries, usage) 新契约
                entries, usage = res[0], res[1]
            else:                             # 测试注入的旧式单条 entry
                entries, usage = res, None
            if entries is None:
                if text:
                    break
                return {"text": "", "steps": steps, "session_id": session_id,
                        "error": "LLM 未启用或调用失败"}
            entry = entries[0] if isinstance(entries, list) else entries
            messages.append(entry)
            calls = entry.get("tool_calls") or []
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
                    args = {}
                t0 = dt.datetime.now()
                try:
                    if name.startswith("mcp__"):
                        out = self._call_mcp(agent, name, args)
                    else:
                        out = tools.call(name, args)
                    result = json.dumps(out, ensure_ascii=False)[:8000] or "{}"
                    ok = True
                except Exception as e:  # noqa: BLE001 工具失败回喂模型自行调整
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
            text = entry.get("content") or text   # 步数耗尽用最后一次模型输出
        if self.memory is not None and agent.get("memory"):
            try:
                self.memory.set(f"session:{session_id}", "_last",
                                {"message": message[:2000], "reply": text[:2000]})
                self.memory.set(f"agent:{agent['id']}", "_last_session",
                                session_id)
            except Exception:  # noqa: BLE001 记忆写失败不影响主流程
                pass
        out = {"text": text, "steps": steps, "session_id": session_id,
               "tool_calls": len(steps)}
        if self.obs is not None:
            try:
                self.obs.agent_run(agent, message, out, session_id=session_id,
                                   flow_run_id=flow_run_id, llm_calls=llm_calls)
            except Exception:  # noqa: BLE001 可观测性故障不影响执行
                pass
        return out


def uuid4_hex() -> str:
    return uuid.uuid4().hex[:12]


def _since(t0: dt.datetime) -> int:
    return int((dt.datetime.now() - t0).total_seconds() * 1000)
