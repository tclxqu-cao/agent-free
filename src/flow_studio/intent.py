"""意图路由：把用户自然语言路由到最匹配的流程并抽取参数。

LLM（config.llm）优先：把流程目录 + 用户话术交给模型选流程、抽参数；
未启用 / 失败时降级为关键词打分（流程名 + 触发示例话术命中）。
"""

from __future__ import annotations

import json
import logging

from .graph import FlowGraph, start_inputs
from .llm import llm_json

log = logging.getLogger("flow_studio")


class RouteResult:
    def __init__(self, flow_id: str | None, params: dict, confidence: float,
                 reply: str, matched: bool, via: str):
        self.flow_id = flow_id
        self.params = params
        self.confidence = confidence
        self.reply = reply
        self.matched = matched
        self.via = via          # llm / keyword / none

    def to_dict(self) -> dict:
        return {"flow_id": self.flow_id, "params": self.params,
                "confidence": self.confidence, "reply": self.reply,
                "matched": self.matched, "via": self.via}


def _catalog(flows: list[FlowGraph]) -> str:
    lines = []
    for g in flows:
        inputs = ", ".join(i["key"] for i in start_inputs(g)) or "无"
        lines.append(f"- id: {g.id}\n  名称: {g.name}\n  说明: {g.description}\n"
                     f"  触发话术: {'、'.join(g.triggers) or '无'}\n  可填参数: {inputs}")
    return "\n".join(lines)


def _llm_route(utterance: str, flows: list[FlowGraph],
               llm_cfg: dict) -> RouteResult | None:
    system = (
        "你是流程调度器。根据用户话术从候选流程中选出最匹配的一个并抽取参数。"
        "严格输出 JSON（不要 markdown 代码块）："
        '{"flow_id": "流程id或null", "params": {"参数名": "值"}, '
        '"confidence": 0到1的小数, "reply": "一句话说明你为什么选它/没选它"}'
        "。没有合适流程时 flow_id 为 null。")
    data = llm_json(llm_cfg, system,
                    f"候选流程：\n{_catalog(flows)}\n\n用户话术：{utterance}")
    if not data:
        return None
    flow_id = data.get("flow_id")
    flow = next((g for g in flows if g.id == flow_id), None)
    if flow is None:
        return RouteResult(None, {}, 0.0,
                           data.get("reply") or "没有匹配到流程。", False, "llm")
    params = _filter_params(flow, data.get("params") or {})
    return RouteResult(flow.id, params, float(data.get("confidence") or 0.8),
                       data.get("reply") or "", True, "llm")


def _keyword_route(utterance: str, flows: list[FlowGraph]) -> RouteResult:
    text = utterance.strip().lower()
    best, best_score = None, 0
    for g in flows:
        score = 0
        for t in g.triggers:
            t = t.strip().lower()
            if t and t in text:
                score += 3
        if g.name.strip().lower() in text:
            score += 2
        for w in (g.name + g.description):
            if w.strip() and w in utterance:
                score += 0.1  # 单字命中弱信号，仅作并列打破
                break
        if score > best_score:
            best, best_score = g, score
    if best is None or best_score < 3:
        if not flows:
            return RouteResult(None, {}, 0.0, "暂无可用流程。", False, "none")
        listing = "\n".join(f"· {g.name}（{g.id}）：{g.description}" for g in flows)
        try_hint = "；".join(flows[0].triggers[:2])
        return RouteResult(None, {}, 0.0,
                           f"没有听懂这个意图。当前可用的流程：\n{listing}\n"
                           f"可以试试说：{try_hint}",
                           False, "none")
    return RouteResult(best.id, {}, min(0.9, 0.3 + best_score / 10), "", True, "keyword")


def _filter_params(flow: FlowGraph, params: dict) -> dict:
    allowed = {i["key"] for i in start_inputs(flow)}
    return {k: v for k, v in (params or {}).items() if k in allowed}


def route(utterance: str, flows: list[FlowGraph], llm_cfg: dict | None = None) -> RouteResult:
    """主入口：LLM 优先，降级关键词。"""
    utterance = (utterance or "").strip()
    if not utterance:
        return RouteResult(None, {}, 0.0, "请描述你想做什么。", False, "none")
    if not flows:
        return RouteResult(None, {}, 0.0, "还没有可用流程，先到画布创建一个。", False, "none")
    if llm_cfg and llm_cfg.get("enabled") and llm_cfg.get("api_key"):
        try:
            r = _llm_route(utterance, flows, llm_cfg)
        except Exception as e:  # noqa: BLE001
            log.warning("LLM 路由失败，降级关键词：%s", e)
            r = None
        if r:
            return r
    return _keyword_route(utterance, flows)


def tools_manifest(flows: list[FlowGraph]) -> list[dict]:
    """OpenAI function-calling 工具清单：每个流程一个工具，供外部 agent 注册。"""
    tools = []
    for g in flows:
        props, required = {}, []
        for i in start_inputs(g):
            props[i["key"]] = {"type": "string", "description": i.get("default", "")}
            if i.get("required"):
                required.append(i["key"])
        tools.append({
            "type": "function",
            "function": {
                "name": g.id,
                "description": f"{g.name}：{g.description} 触发话术：{'、'.join(g.triggers)}",
                "parameters": {"type": "object", "properties": props,
                               "required": required},
            },
        })
    return tools


def parse_tool_call(name: str, arguments: str | dict,
                    flows: list[FlowGraph]) -> tuple[FlowGraph | None, dict]:
    """把外部 agent 的 tool call（name + JSON arguments）解析为 (flow, params)。"""
    flow = next((g for g in flows if g.id == name), None)
    if flow is None:
        return None, {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            arguments = {}
    return flow, _filter_params(flow, arguments or {})


# ---------------------------------------------------------------- 意图节点分类
def classify_intent(utterance: str, intents: list[dict],
                    llm_cfg: dict | None = None) -> dict:
    """把一句话分类到意图清单中的某一个。

    intents: [{name, description, samples:[...]}]；返回
    {intent: 名称或 None, confidence: 0-1, via: llm/keyword/none}。
    LLM 优先；未启用 / 失败 / 返回了清单外的意图时降级关键词打分。
    """
    utterance = (utterance or "").strip()
    names = [str(i.get("name") or "").strip() for i in intents if i.get("name")]
    if not utterance or not names:
        return {"intent": None, "confidence": 0.0, "via": "none"}

    if llm_cfg and llm_cfg.get("enabled") and llm_cfg.get("api_key"):
        catalog = "\n".join(
            f"- {i['name']}：{i.get('description') or ''}（例：{'、'.join(i.get('samples') or [])}）"
            for i in intents if i.get("name"))
        data = llm_json(
            llm_cfg,
            "你是意图分类器。只输出 JSON：{\"intent\": \"意图名或null\", "
            "\"confidence\": 0到1的小数}。意图名必须从候选里选，都不符则为 null。",
            f"候选意图：\n{catalog}\n\n话术：{utterance}")
        if data and data.get("intent") in names:
            try:
                conf = min(1.0, max(0.0, float(data.get("confidence") or 0.8)))
            except (TypeError, ValueError):
                conf = 0.8
            return {"intent": data["intent"], "confidence": conf, "via": "llm"}

    text = utterance.lower()
    best, best_score = None, 0
    for i in intents:
        name = str(i.get("name") or "").strip()
        if not name:
            continue
        score = 0
        if name.lower() in text:
            score += 2
        for s in i.get("samples") or []:
            s = str(s).strip().lower()
            if s and s in text:
                score += 3
        if str(i.get("description") or "").strip() \
                and str(i["description"]).strip() in utterance:
            score += 1
        if score > best_score:
            best, best_score = name, score
    if best is None or best_score < 2:
        return {"intent": None, "confidence": 0.0, "via": "none"}
    return {"intent": best, "confidence": min(0.9, 0.3 + best_score / 10),
            "via": "keyword"}
