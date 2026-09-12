"""执行引擎：从 start 节点沿边拓扑执行，条件节点按出边 branch 选路。

OR-join 语义：任一活跃入边到达即触发节点；节点输出（JSON 兼容 dict）写入
namespace 供下游模板/条件引用。RunResult 保留逐节点状态供画布高亮。
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from dataclasses import dataclass, field

from .graph import FlowGraph, Node
from . import bridge as bridge_mod
from .llm import llm_chat
from .registry import AgentRegistry, AgentAction
from .template import ExprError, build_namespace, eval_expr, render, render_deep

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
    ms: int = 0

    def to_dict(self) -> dict:
        return {"node_id": self.node_id, "type": self.type, "label": self.label,
                "status": self.status, "output": self.output,
                "error": self.error, "ms": self.ms}


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
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict:
        return {"run_id": self.run_id, "flow_id": self.flow_id,
                "flow_name": self.flow_name, "status": self.status,
                "input": self.input, "node_runs": [n.to_dict() for n in self.node_runs],
                "output": self.output, "error": self.error,
                "started_at": self.started_at, "finished_at": self.finished_at}

    def node_run(self, node_id: str) -> NodeRun | None:
        return next((n for n in self.node_runs if n.node_id == node_id), None)


class FlowRunner:
    def __init__(self, registry: AgentRegistry | None = None,
                 llm_cfg: dict | None = None, bridge_cfg: dict | None = None,
                 media=None, vmodels=None):
        self.registry = registry or AgentRegistry()
        self.llm_cfg = llm_cfg or {}
        self.bridge_cfg = bridge_cfg or {}
        self.media = media          # AssetStore（视频节点用，可空）
        self.vmodels = vmodels      # VideoModels（视频节点用，可空）

    # ---------------------------------------------------------------- 主流程
    def run(self, graph: FlowGraph, inputs: dict | None = None) -> RunResult:
        start_time = time.time()
        result = RunResult(
            run_id=uuid.uuid4().hex[:12], flow_id=graph.id, flow_name=graph.name,
            input=dict(inputs or {}),
            started_at=dt.datetime.now().isoformat(timespec="seconds"))
        ns = build_namespace(
            {"today": dt.date.today().isoformat(), "run_id": result.run_id,
             "flow_id": graph.id},
            result.input, {})

        start_node = graph.start()
        if start_node is None:  # graph_from_dict 已校验，此处兜底
            result.status = "failed"
            result.error = "缺少开始节点"
            result.finished_at = dt.datetime.now().isoformat(timespec="seconds")
            return result

        queue: list[str] = [start_node.id]
        run_counts: dict[str, int] = {}
        steps = 0
        outputs: dict[str, dict] = {}   # node_id → output dict

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
                break

            queue.extend(self._next_nodes(graph, node, ns))

        result.finished_at = dt.datetime.now().isoformat(timespec="seconds")
        result.started_at = result.started_at  # 保留
        return result

    # ---------------------------------------------------------------- 选路
    def _next_nodes(self, graph: FlowGraph, node: Node, ns: dict) -> list[str]:
        edges = graph.out_edges(node.id)
        if not edges:
            return []
        if node.type == "condition":
            chosen: list[str] = []
            hit = False
            for e in edges:
                branch = (e.get("branch") or "").strip()
                if branch.lower() == "else":
                    continue
                try:
                    if eval_expr(branch, ns):
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

    # ---------------------------------------------------------------- 节点执行
    def _execute_node(self, node: Node, ns: dict, result: RunResult) -> NodeRun:
        nrun = NodeRun(node_id=node.id, type=node.type, label=node.display_label())
        result.node_runs.append(nrun)
        nrun.status = "running"
        t0 = time.time()
        try:
            handler = {
                "start": self._run_start, "end": self._run_end,
                "llm": self._run_llm, "agent": self._run_agent,
                "condition": self._run_condition, "template": self._run_template,
                "http": self._run_http, "intent": self._run_intent,
                "brain": self._run_brain,
                "storyboard": self._run_video_node, "character": self._run_video_node,
                "keyframe": self._run_video_node, "shot_video": self._run_video_node,
                "merge_video": self._run_video_node, "asset": self._run_video_node,
            }[node.type]
            nrun.output = handler(node, ns) or {}
            nrun.status = "success"
        except _SkipNode as e:
            nrun.status = "skipped"
            nrun.output = {"skipped": True, "reason": str(e)}
        except Exception as e:  # noqa: BLE001 节点隔离，错误进 RunResult
            nrun.status = "failed"
            nrun.error = f"{type(e).__name__}: {e}"
        nrun.ms = int((time.time() - t0) * 1000)
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
        return {}  # 分支选择在 _next_nodes，节点本身无副作用

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
