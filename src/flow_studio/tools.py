"""工具注册表：内置工具 + 知识库/记忆/技能绑定工具，供工具节点与智能体使用。

工具签名与 AgentRegistry 一致：入参 / 出参均为 JSON 兼容 dict。
manifest() 输出 OpenAI function-calling 格式，直接喂给 LLM。
"""

from __future__ import annotations

import ast
import datetime as dt
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict = field(default_factory=dict)   # JSON Schema
    fn: Callable[[dict], dict] = None  # noqa: RUF013


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, name: str, description: str, fn: Callable[[dict], dict],
                 parameters: dict | None = None) -> Tool:
        tool = Tool(name=name, description=description, fn=fn,
                    parameters=parameters or {"type": "object", "properties": {}})
        self._tools[name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def manifest(self, only: list[str] | None = None) -> list[dict]:
        """OpenAI tools 格式；only 为空时导出全部。"""
        names = set(only or self._tools)
        return [{"type": "function", "function": {
            "name": t.name, "description": t.description,
            "parameters": t.parameters}}
            for n, t in sorted(self._tools.items()) if n in names]

    def call(self, name: str, args: dict) -> dict:
        tool = self._tools.get(name)
        if tool is None:
            raise ValueError(f"未注册的工具：{name}")
        out = tool.fn(args or {})
        return out if isinstance(out, dict) else {"result": out}


# ---------------------------------------------------------------- 内置工具
def register_builtin_tools(reg: ToolRegistry, kb=None, memory=None,
                           skills=None) -> None:
    """把通用工具与各存储的检索/读写能力注册进去（存储可空，按需绑定）。"""

    reg.register(
        "http_request", "发起一次 HTTP 请求，返回 {status, text, json?}",
        _http_request, {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "完整 URL"},
                "method": {"type": "string",
                           "description": "GET/POST/…，默认 GET"},
                "headers": {"type": "object", "description": "请求头"},
                "body": {"type": "string", "description": "请求体文本"},
                "timeout": {"type": "number", "description": "超时秒，默认 30"}},
            "required": ["url"]})

    reg.register(
        "now", "当前日期时间（本地时区）",
        lambda _: {
            "iso": dt.datetime.now().isoformat(timespec="seconds"),
            "date": dt.date.today().isoformat(),
            "weekday": "一二三四五六日"[dt.date.today().weekday()]},
        {"type": "object", "properties": {}})

    reg.register(
        "calc", "算术表达式求值（只允许数字与 + - * / % ** 和括号）",
        _calc, {"type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"]})

    if kb is not None:
        reg.register(
            "kb_search", "在知识库里检索资料，返回最相关的片段",
            lambda a: {"results": kb.search(
                str(a.get("query") or ""),
                kb_ids=a.get("kb_ids") or None,
                top_k=int(a.get("top_k") or 5))}, {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索问题"},
                    "kb_ids": {"type": "array", "items": {"type": "string"},
                               "description": "限定知识库 id，留空查全部"},
                    "top_k": {"type": "number", "description": "返回条数，默认 5"}},
                "required": ["query"]})

    if memory is not None:
        reg.register(
            "memory_save", "把一条信息存入长期记忆（键值对）",
            lambda a: memory.set(a.get("scope") or "global", a.get("key"),
                                 a.get("value")), {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"description": "任意 JSON 值"},
                    "scope": {"type": "string",
                              "description": "默认 global"}},
                "required": ["key", "value"]})
        reg.register(
            "memory_load", "按键读取长期记忆",
            lambda a: {"value": memory.get(a.get("scope") or "global",
                                           a.get("key"))}, {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "scope": {"type": "string", "description": "默认 global"}},
                "required": ["key"]})
        reg.register(
            "memory_search", "按关键词模糊搜索长期记忆",
            lambda a: {"items": memory.list(
                scope=a.get("scope") or None, q=str(a.get("query") or ""),
                limit=int(a.get("limit") or 10))}, {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "scope": {"type": "string"},
                    "limit": {"type": "number"}},
                "required": ["query"]})

    if skills is not None:
        reg.register(
            "load_skill", "加载一个技能的完整指令（SKILL.md 正文）",
            lambda a: (lambda s: s or (_ for _ in ()).throw(
                ValueError(f"技能不存在：{a.get('skill_id')}")))(
                skills.get(str(a.get("skill_id") or ""))), {
                "type": "object",
                "properties": {"skill_id": {"type": "string"}},
                "required": ["skill_id"]})
        reg.register(
            "list_skills", "列出全部可用技能（名称与描述）",
            lambda _: {"skills": skills.list()},
            {"type": "object", "properties": {}})


def _http_request(args: dict) -> dict:
    import httpx

    method = str(args.get("method") or "GET").upper()
    timeout = float(args.get("timeout") or 30)
    resp = httpx.request(method, str(args.get("url") or ""),
                         headers=args.get("headers") or None,
                         content=args.get("body") or None, timeout=timeout)
    out: dict = {"status": resp.status_code, "text": resp.text[:20000]}
    try:
        out["json"] = resp.json()
    except Exception:  # noqa: BLE001
        pass
    return out


def _calc(args: dict) -> dict:
    expr = str(args.get("expression") or "").strip()
    tree = ast.parse(expr, mode="eval")

    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(
                node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow)):
            a, b = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Add):
                return a + b
            if isinstance(node.op, ast.Sub):
                return a - b
            if isinstance(node.op, ast.Mult):
                return a * b
            if isinstance(node.op, ast.Div):
                return a / b
            if isinstance(node.op, ast.Mod):
                return a % b
            return a ** b
        if isinstance(node, ast.UnaryOp) and isinstance(
                node.op, (ast.UAdd, ast.USub)):
            v = ev(node.operand)
            return v if isinstance(node.op, ast.UAdd) else -v
        raise ValueError(f"表达式不允许：{type(node).__name__}")

    val = ev(tree)
    val = int(val) if isinstance(val, float) and val.is_integer() else val
    return {"expression": expr, "value": val}
