"""执行引擎：从 start 节点沿边拓扑执行，条件节点按出边 branch 选路。

OR-join 语义：任一活跃入边到达即触发节点；节点输出（JSON 兼容 dict）写入
namespace 供下游模板/条件引用。RunResult 保留逐节点状态供画布高亮。
"""

from __future__ import annotations

import datetime as dt
import json
import time
import traceback
import uuid
from dataclasses import dataclass, field

from .graph import FlowGraph, Node, graph_to_dict
from . import bridge as bridge_mod
from .llm import llm_chat
from .registry import AgentRegistry, AgentAction
from .run_events import capture_node_logs, redact_log
from .template import (ExprError, build_namespace, compare_value, eval_expr,
                       render, render_deep, resolve, resolve_path)

MAX_NODE_RUNS = 2      # 单节点最多执行次数（OR-join 再次到达只传播不重跑）
MAX_STEPS = 1000       # 全流程步数上限（防环兜底）


@dataclass
class NodeRun:
    node_id: str
    type: str
    label: str
    status: str = "pending"          # pending/running/success/skipped/failed
    output: dict | None = None
    error: str | None = None
    traceback: str | None = None
    ms: int = 0

    def to_dict(self) -> dict:
        return {"node_id": self.node_id, "type": self.type, "label": self.label,
                "status": self.status, "output": self.output,
                "error": self.error, "traceback": self.traceback, "ms": self.ms}


@dataclass
class RunResult:
    run_id: str
    flow_id: str
    flow_name: str
    status: str = "success"          # success / failed
    input: dict = field(default_factory=dict)
    node_runs: list[NodeRun] = field(default_factory=list)
    output: str = ""                 # 最后触达的 end 节点渲染文本
    error: str | None = None
    traceback: str | None = None
    graph: dict | None = None
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict:
        return {"run_id": self.run_id, "flow_id": self.flow_id,
                "flow_name": self.flow_name, "status": self.status,
                "input": self.input, "node_runs": [n.to_dict() for n in self.node_runs],
                "output": self.output, "error": self.error, "traceback": self.traceback,
                "graph": self.graph,
                "started_at": self.started_at, "finished_at": self.finished_at}

    def node_run(self, node_id: str) -> NodeRun | None:
        return next((n for n in self.node_runs if n.node_id == node_id), None)


class FlowRunner:
    def __init__(self, registry: AgentRegistry | None = None,
                 llm_cfg: dict | None = None, bridge_cfg: dict | None = None,
                 media=None, vmodels=None, kb=None, memory=None, skills=None,
                 mcp=None, tools=None, ai_agents=None, agent_rt=None, obs=None,
                 subflow_invoker=None):
        self.registry = registry or AgentRegistry()
        self.llm_cfg = llm_cfg or {}
        self.bridge_cfg = bridge_cfg or {}
        self.media = media          # AssetStore（视频节点用，可空）
        self.vmodels = vmodels      # VideoModels（视频节点用，可空）
        self.kb = kb                # KBStore（知识库节点，可空）
        self.memory = memory        # MemoryStore（记忆节点，可空）
        self.skills = skills        # SkillStore（技能节点，可空）
        self.mcp = mcp              # MCPManager（MCP 节点，可空）
        self.tools = tools          # ToolRegistry（工具节点，可空）
        self.ai_agents = ai_agents  # AgentStore（智能体节点，可空）
        self.agent_rt = agent_rt    # AgentRuntime（智能体节点执行器，可空）
        self.obs = obs              # 可观测性 observer（None = 不上报）
        self.subflow_invoker = subflow_invoker

    # ---------------------------------------------------------------- 主流程
    def run(self, graph: FlowGraph, inputs: dict | None = None, *,
            run_id: str | None = None, event_sink=None) -> RunResult:
        self._event_sink = event_sink
        result = RunResult(
            run_id=run_id or uuid.uuid4().hex[:12], flow_id=graph.id, flow_name=graph.name,
            input=dict(inputs or {}), status="running", graph=graph_to_dict(graph),
            started_at=dt.datetime.now().isoformat(timespec="seconds"))
        self._result = result
        self._emit("run.started", "流程开始执行")
        try:
            self._walk(graph, result)
        except Exception as exc:
            result.status = "failed"
            result.error = redact_log(f"{type(exc).__name__}: {exc}")
            result.traceback = redact_log(traceback.format_exc())
        if result.status == "running":
            result.status = "success"
        result.finished_at = dt.datetime.now().isoformat(timespec="seconds")
        self._emit("run.finished", result.error or "流程执行完成",
                   level="error" if result.status == "failed" else "info",
                   traceback=result.traceback)
        self._observe(result)
        return result

    def _walk(self, graph: FlowGraph, result: RunResult) -> None:
        ns = build_namespace(
            {"today": dt.date.today().isoformat(), "run_id": result.run_id,
             "flow_id": graph.id},
            result.input, {})

        start_node = graph.start()
        if start_node is None:  # graph_from_dict 已校验，此处兜底
            result.status = "failed"
            result.error = "缺少开始节点"
            result.finished_at = dt.datetime.now().isoformat(timespec="seconds")
            return

        queue: list[str] = [start_node.id]
        run_counts: dict[str, int] = {}
        steps = 0
        outputs: dict[str, dict] = {}   # node_id → output dict
        self._result = result           # 供节点处理器读取 run 上下文（如 run_id）

        while queue:
            steps += 1
            if steps > MAX_STEPS:
                result.status = "failed"
                result.error = f"执行步数超过上限 {MAX_STEPS}（可能存在循环）"
                break
            node_id = queue.pop(0)
            node = graph.node(node_id)
            if node is None:
                continue
            if run_counts.get(node_id, 0) >= MAX_NODE_RUNS:
                continue
            run_counts[node_id] = run_counts.get(node_id, 0) + 1

            nrun = self._execute_node(node, ns, result)
            if node.type == "end":
                if nrun.status == "success":
                    result.output = (nrun.output or {}).get("text", "")
            else:
                outputs[node_id] = nrun.output or {}
                # 刷新命名空间中该节点的输出（input 已由 start 节点合并默认值）
                ns.update(outputs)

            if nrun.status == "failed":
                result.status = "failed"
                result.error = f"节点「{node.display_label()}」失败：{nrun.error}"
                result.traceback = nrun.traceback
                break

            queue.extend(self._next_nodes(graph, node, ns))

    def _emit(self, event_type: str, message: str, node: NodeRun | None = None,
              level: str = "info", traceback: str | None = None) -> None:
        sink = getattr(self, "_event_sink", None)
        if sink is None:
            return
        event = {"type": event_type, "level": level, "message": redact_log(message)}
        if node is not None:
            event.update(node_id=node.node_id, node_label=node.label)
        if traceback:
            event["traceback"] = redact_log(traceback)
        sink(self._result.to_dict(), event)

    def _observe(self, result: RunResult) -> None:
        """可观测性上报（Langfuse 等），任何故障不影响流程结果。"""
        if self.obs is None:
            return
        try:
            self.obs.flow_run(result.to_dict())
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- 选路
    def _next_nodes(self, graph: FlowGraph, node: Node, ns: dict) -> list[str]:
        edges = graph.out_edges(node.id)
        if not edges:
            return []
        if node.type == "condition":
            if "outputs" in node.params:
                return [edges[0]["to"]]
            chosen: list[str] = []
            hit = False
            for e in edges:
                branch = (e.get("branch") or "").strip()
                if branch.lower() == "else":
                    continue
                try:
                    if self._condition_matches(node, e, ns):
                        chosen.append(e["to"])
                        hit = True
                        break
                except ExprError:
                    # 表达式坏 → 该边视为假，继续后面的边（错误留到兜底）
                    continue
            if not hit:
                for e in edges:
                    if (e.get("branch") or "").strip().lower() == "else":
                        chosen.append(e["to"])
                        break
            return chosen
        if node.type == "intent":
            picked = str((ns.get(node.id) or {}).get("intent") or "").strip()
            if picked:
                for e in edges:
                    if (e.get("branch") or "").strip() == picked:
                        return [e["to"]]
            for e in edges:
                if (e.get("branch") or "").strip().lower() == "else":
                    return [e["to"]]
            return []
        return [e["to"] for e in edges]

    @staticmethod
    def _condition_matches(node: Node, edge: dict, ns: dict) -> bool:
        operator = str(edge.get("operator") or "").strip()
        if not operator:
            return eval_expr(str(edge.get("branch") or ""), ns)
        found, actual = resolve_path(node.params.get("source") or "input.message", ns)
        if not found:
            return False
        value_type = str(edge.get("value_type") or "string").strip()
        raw = edge.get("value")
        if value_type == "string":
            expected = str(raw if raw is not None else "")
        elif value_type == "number":
            try:
                expected = float(raw)
                if expected.is_integer():
                    expected = int(expected)
            except (TypeError, ValueError) as exc:
                raise ExprError("比较值不是有效数字") from exc
        elif value_type == "boolean":
            if isinstance(raw, bool):
                expected = raw
            elif str(raw).strip().lower() in {"true", "1"}:
                expected = True
            elif str(raw).strip().lower() in {"false", "0"}:
                expected = False
            else:
                raise ExprError("比较值不是有效布尔值")
        elif value_type == "null":
            expected = None
        else:
            raise ExprError(f"不支持比较值类型：{value_type}")
        return compare_value(actual, operator, expected)

    # ---------------------------------------------------------------- 节点执行
    def _execute_node(self, node: Node, ns: dict, result: RunResult) -> NodeRun:
        nrun = NodeRun(node_id=node.id, type=node.type, label=node.display_label())
        result.node_runs.append(nrun)
        nrun.status = "running"
        self._emit("node.started", f"开始：{nrun.label}", nrun)
        t0 = time.time()
        try:
            handler = {
                "start": self._run_start, "end": self._run_end,
                "llm": self._run_llm, "agent": self._run_agent,
                "subflow": self._run_subflow,
                "condition": self._run_condition, "template": self._run_template,
                "http": self._run_http, "intent": self._run_intent,
                "brain": self._run_brain,
                "ai_agent": self._run_ai_agent, "kb": self._run_kb,
                "memory": self._run_memory, "skill": self._run_skill,
                "mcp": self._run_mcp, "tool": self._run_tool,
                "storyboard": self._run_video_node, "character": self._run_video_node,
                "keyframe": self._run_video_node, "shot_video": self._run_video_node,
                "merge_video": self._run_video_node, "voiceover": self._run_video_node,
                "video_compose": self._run_video_node, "asset": self._run_video_node,
            }[node.type]
            def on_log(level, message, trace):
                nrun.ms = int((time.time() - t0) * 1000)
                self._emit("node.log", message, nrun, level, trace)

            self._active_node_run = nrun
            try:
                with capture_node_logs(on_log):
                    nrun.output = handler(node, ns) or {}
            finally:
                self._active_node_run = None
            nrun.status = "success"
        except _SkipNode as e:
            nrun.status = "skipped"
            nrun.output = {"skipped": True, "reason": redact_log(str(e))}
            if e.__context__ is not None or e.__cause__ is not None:
                nrun.traceback = redact_log(traceback.format_exc())
        except Exception as e:  # noqa: BLE001 节点隔离，错误进 RunResult
            nrun.status = "failed"
            nrun.error = redact_log(f"{type(e).__name__}: {e}")
            nrun.traceback = redact_log(traceback.format_exc())
        nrun.ms = int((time.time() - t0) * 1000)
        description = {"success": "完成", "failed": "失败", "skipped": "降级"}[nrun.status]
        detail = nrun.error or (nrun.output or {}).get("reason") or ""
        self._emit("node.finished", f"{description}：{nrun.label}（{nrun.ms}ms）" +
                   (f"：{detail}" if detail else ""), nrun,
                   "error" if nrun.status == "failed" else
                   "warning" if nrun.status == "skipped" else "info", nrun.traceback)
        return nrun

    def _run_start(self, node: Node, ns: dict) -> dict:
        declared = node.params.get("inputs") or []
        defaults = {}
        for item in declared:
            if isinstance(item, dict) and item.get("key"):
                defaults[item["key"]] = item.get("default", "")
        merged = {**defaults, **ns.get("input", {})}
        ns["input"] = merged
        return {"inputs": merged}

    def _run_end(self, node: Node, ns: dict) -> dict:
        return {"text": render(node.params.get("output") or "", ns)}

    def _run_llm(self, node: Node, ns: dict) -> dict:
        system = render(node.params.get("system") or "你是得力助手。", ns)
        prompt = render(node.params.get("prompt") or "", ns)
        text = llm_chat(self.llm_cfg, system, prompt)
        if text is None:
            if node.params.get("required"):
                raise RuntimeError("LLM 未启用或调用失败（required=true）")
            raise _SkipNode("LLM 未启用或调用失败，已降级跳过")
        return {"text": text}

    def _run_agent(self, node: Node, ns: dict) -> dict:
        agent = str(node.params.get("agent") or "").strip()
        action = str(node.params.get("action") or "").strip()
        entry: AgentAction | None = self.registry.get(agent, action)
        if entry is None:
            raise ValueError(f"未注册的 agent 能力：{agent}.{action}")
        raw_args = node.params.get("args") or {}
        args = _coerce_args(entry, render_deep(raw_args, ns))
        missing = [p["key"] for p in entry.params
                   if p.get("required") and p["key"] not in args]
        if missing:
            raise ValueError(f"缺少必填参数：{'、'.join(missing)}")
        try:
            out = entry.fn(args)
        except Exception as e:
            if node.params.get("optional"):
                raise _SkipNode(f"agent 执行失败已降级：{e}")
            raise
        if not isinstance(out, dict):
            out = {"result": out}
        return out

    def _run_subflow(self, node: Node, ns: dict) -> dict:
        if self.subflow_invoker is None:
            raise ValueError("子流程运行时未初始化")
        flow_id = render(str(node.params.get("flow_id") or ""), ns).strip()
        if not flow_id:
            raise ValueError("子流程 ID 为空")
        inputs = render_deep(node.params.get("inputs") or {}, ns)
        if not isinstance(inputs, dict):
            raise ValueError("子流程 inputs 必须是对象")

        def forward(event: dict) -> None:
            event_type = str(event.get("type") or "")
            if event_type not in {"node.started", "node.log", "node.finished",
                                  "run.started", "run.finished"}:
                return
            label = str(event.get("node_label") or flow_id)
            message = str(event.get("message") or event_type)
            self._emit(
                "node.log", f"{label}：{message}",
                getattr(self, "_active_node_run", None),
                str(event.get("level") or "info"), event.get("traceback"))

        run = self.subflow_invoker(flow_id, inputs, event_sink=forward)
        if not isinstance(run, dict):
            raise RuntimeError("子流程返回值无效")
        if run.get("status") != "success":
            message = str(run.get("error") or "子流程执行失败")
            if node.params.get("required", True):
                raise RuntimeError(message)
            raise _SkipNode(message)
        nodes = {str(item.get("node_id") or ""): item.get("output") or {}
                 for item in run.get("node_runs") or [] if item.get("node_id")}
        return {"text": str(run.get("output") or ""),
                "run_id": str(run.get("run_id") or ""),
                "flow_id": str(run.get("flow_id") or flow_id),
                "status": str(run.get("status") or ""), "nodes": nodes}

    def _run_brain(self, node: Node, ns: dict) -> dict:
        prompt = render(node.params.get("prompt") or "{{input.message}}", ns).strip()
        if not prompt:
            raise ValueError("我的 Agent 节点话术为空")
        session_id = render(str(node.params.get("session_id") or ""), ns).strip()
        timeout = float(node.params.get("timeout")
                        or self.bridge_cfg.get("timeout") or 300)
        try:
            out = bridge_mod.agent_reason(self.bridge_cfg, prompt,
                                          session_id=session_id or None,
                                          timeout=timeout)
        except Exception as e:  # noqa: BLE001
            if node.params.get("required"):
                raise RuntimeError(f"我的 Agent 推理失败：{e}")
            raise _SkipNode(f"我的 Agent 推理失败，已降级：{e}")
        return {**out, "text": out.get("text", "")}

    # ---------------- 智能体平台节点 ----------------
    def _run_ai_agent(self, node: Node, ns: dict) -> dict:
        if self.ai_agents is None or self.agent_rt is None:
            raise ValueError("智能体运行时未初始化")
        agent_id = str(node.params.get("ai_agent_id") or "").strip()
        agent = self.ai_agents.get(agent_id)
        if agent is None:
            raise ValueError(f"智能体不存在：{agent_id}（资源库中创建后可选择）")
        message = render(node.params.get("message") or "{{input.message}}", ns).strip()
        if not message:
            raise ValueError("智能体节点消息为空")
        skill_id = render(str(node.params.get("skill_id") or ""), ns).strip()
        if skill_id and skill_id not in set(agent.get("skill_ids") or []):
            raise ValueError(f"智能体未绑定技能：{skill_id}")
        session_id = render(str(node.params.get("session_id") or ""), ns).strip()
        def resolve_context(value):
            if isinstance(value, str):
                return resolve(value, ns)
            if isinstance(value, dict):
                return {str(key): resolve_context(item) for key, item in value.items()}
            if isinstance(value, list):
                return [resolve_context(item) for item in value]
            return value
        context = resolve_context(node.params.get("context") or {})
        if not isinstance(context, dict):
            raise ValueError("智能体节点 context 必须是对象")
        run_id = getattr(self, "_result", None) and self._result.run_id
        out = self.agent_rt.run(agent, message, session_id=session_id or None,
                                flow_run_id=run_id, skill_id=skill_id or None,
                                context=context)
        if out.get("error"):
            if node.params.get("required"):
                raise RuntimeError(f"智能体执行失败：{out['error']}")
            raise _SkipNode(f"智能体执行失败，已降级：{out['error']}")
        return {"text": out.get("text", ""),
                **({"artifact": out["artifact"]} if isinstance(out.get("artifact"), dict) else {}),
                "steps": out.get("steps", [])[:20],
                "tool_calls": out.get("tool_calls", 0),
                "session_id": out.get("session_id", "")}

    def _run_kb(self, node: Node, ns: dict) -> dict:
        if self.kb is None:
            raise ValueError("知识库未初始化")
        kb_ids = node.params.get("kb_ids") or []
        query = render(node.params.get("query") or "{{input.message}}", ns).strip()
        if not query:
            raise ValueError("知识库节点的检索问题为空")
        chunks = self.kb.search(query, kb_ids=[str(k) for k in kb_ids] or None,
                                top_k=int(node.params.get("top_k") or 5))
        text = "\n\n".join(f"【{c['name']}】{c['text']}" for c in chunks)
        return {"text": text, "count": len(chunks), "chunks": chunks}

    def _run_memory(self, node: Node, ns: dict) -> dict:
        if self.memory is None:
            raise ValueError("记忆存储未初始化")
        op = str(node.params.get("op") or "get")
        kind = str(node.params.get("scope") or "session")
        if kind == "session":
            scope = "session:" + (render(str(node.params.get("session_id")
                                                or "{{vars.run_id}}"), ns).strip()
                                  or ns.get("vars", {}).get("run_id", ""))
        else:
            scope = "global"
        if op == "set":
            raw = node.params.get("value")
            value = render(str(raw or ""), ns)
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                pass
            out = self.memory.set(scope, render(str(node.params.get("key") or ""), ns),
                                  value)
            return {**out, "text": json.dumps(out.get("value"), ensure_ascii=False)}
        if op == "get":
            val = self.memory.get(scope, render(str(node.params.get("key") or ""), ns))
            return {"value": val,
                    "text": "" if val is None else json.dumps(val, ensure_ascii=False)}
        if op == "search":
            items = self.memory.list(scope=None if kind == "global" else scope,
                                     q=render(str(node.params.get("query") or ""), ns))
            return {"items": items, "text": "\n".join(
                f"{it['key']}: {json.dumps(it['value'], ensure_ascii=False)}"
                for it in items)}
        if op == "list":
            items = self.memory.list(scope=None if kind == "global" else scope)
            return {"items": items, "text": "\n".join(
                f"{it['key']}: {json.dumps(it['value'], ensure_ascii=False)}"
                for it in items)}
        if op == "delete":
            ok = self.memory.delete(scope,
                                    render(str(node.params.get("key") or ""), ns))
            return {"ok": ok, "text": "已删除" if ok else "键不存在"}
        raise ValueError(f"未知记忆操作：{op}")

    def _run_skill(self, node: Node, ns: dict) -> dict:
        if self.skills is None:
            raise ValueError("技能库未初始化")
        skill_id = str(node.params.get("skill_id") or "").strip()
        skill = self.skills.get(skill_id)
        if skill is None:
            raise ValueError(f"技能不存在：{skill_id}（资源库中创建后可选择）")
        out: dict = {"name": skill["name"], "instructions": skill["content"]}
        prompt = render(node.params.get("prompt") or "", ns).strip()
        if not prompt:
            return {**out, "text": skill["content"]}
        text = llm_chat(self.llm_cfg, skill["content"], prompt)
        if text is None:
            if node.params.get("required"):
                raise RuntimeError("技能 LLM 调用失败（required=true）")
            raise _SkipNode("技能 LLM 未启用或调用失败，已降级跳过")
        return {**out, "text": text}

    def _run_mcp(self, node: Node, ns: dict) -> dict:
        if self.mcp is None:
            raise ValueError("MCP 未配置")
        server = str(node.params.get("server") or "").strip()
        tool = str(node.params.get("tool") or "").strip()
        if not server or not tool:
            raise ValueError("MCP 节点未选择服务器或工具")
        args = render_deep(node.params.get("arguments") or {}, ns)
        try:
            out = self.mcp.call(server, tool, args,
                                timeout=float(node.params.get("timeout") or 120))
        except Exception as e:
            if node.params.get("optional"):
                raise _SkipNode(f"MCP 调用失败已降级：{e}") from e
            raise
        return {**out, "arguments": args}

    def _run_tool(self, node: Node, ns: dict) -> dict:
        if self.tools is None:
            raise ValueError("工具注册表未初始化")
        name = str(node.params.get("tool") or "").strip()
        if not name:
            raise ValueError("工具节点未选择工具")
        args = render_deep(node.params.get("arguments") or {}, ns)
        try:
            return self.tools.call(name, args)
        except Exception as e:
            if node.params.get("optional"):
                raise _SkipNode(f"工具调用失败已降级：{e}") from e
            raise

    def _run_video_node(self, node: Node, ns: dict) -> dict:
        from . import video_nodes

        if self.media is None:
            raise ValueError("素材库未初始化（video 节点需要 Studio 组装 AssetStore）")
        try:
            return video_nodes.execute_video_node(
                node, ns, self.media, self.vmodels, self.llm_cfg)
        except video_nodes.SkipNode as e:
            raise _SkipNode(str(e)) from e

    def _run_condition(self, node: Node, ns: dict) -> dict:
        if "outputs" not in node.params:
            return {}  # 兼容旧流程：分支选择仍在 _next_nodes
        for rule in node.params.get("outputs") or []:
            try:
                matched = eval_expr(str(rule.get("expression") or ""), ns)
            except ExprError:
                matched = False
            if matched:
                return {"matched": True, "rule": str(rule.get("name") or ""),
                        "text": render(str(rule.get("output") or ""), ns)}
        return {"matched": False, "rule": "default",
                "text": render(str(node.params.get("default_output") or ""), ns)}

    def _run_intent(self, node: Node, ns: dict) -> dict:
        from .intent import classify_intent

        utterance = render(node.params.get("source") or "{{input.message}}", ns).strip()
        intents = node.params.get("intents") or []
        if not intents:
            raise ValueError("意图节点未配置意图清单")
        if not utterance:
            raise ValueError("意图节点的话术为空")
        picked = classify_intent(utterance, intents, self.llm_cfg)
        if not picked.get("intent") and node.params.get("required"):
            raise RuntimeError(f"意图未命中（话术：{utterance[:50]}）且 required=true")
        return {"text": utterance, "matched": bool(picked.get("intent")), **picked}

    def _run_template(self, node: Node, ns: dict) -> dict:
        return {"text": render(node.params.get("template") or "", ns)}

    def _run_http(self, node: Node, ns: dict) -> dict:
        import httpx

        method = str(node.params.get("method") or "GET").upper()
        url = render(node.params.get("url") or "", ns)
        if not url:
            raise ValueError("HTTP 节点缺少 URL")
        headers = render_deep(node.params.get("headers") or {}, ns)
        body = node.params.get("body")
        timeout = float(node.params.get("timeout") or 30)
        try:
            resp = httpx.request(method, url, headers=headers or None,
                                 content=render(body, ns) if body else None,
                                 timeout=timeout)
        except Exception as e:
            if node.params.get("optional"):
                raise _SkipNode(f"请求失败已降级：{e}")
            raise
        out: dict = {"status": resp.status_code,
                     "text": resp.text[:20000]}
        try:
            out["json"] = resp.json()
        except Exception:
            pass
        return out


class _SkipNode(Exception):
    """节点降级跳过（流程继续）。"""


def _coerce_args(entry: AgentAction, args: dict) -> dict:
    """按动作声明的参数类型纠正模板渲染产生的字符串（如 "False"→False）。"""
    types = {p["key"]: p.get("type") for p in entry.params}
    out = {}
    for key, val in args.items():
        typ = types.get(key)
        if typ == "bool" and not isinstance(val, bool):
            out[key] = str(val).strip().lower() in ("1", "true", "yes", "on", "是")
        elif typ == "number" and isinstance(val, str):
            try:
                f = float(val)
                out[key] = int(f) if f.is_integer() else f
            except ValueError:
                out[key] = val
        else:
            out[key] = val
    return out
