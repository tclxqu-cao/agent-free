"""OpenAI 兼容 chat 调用（与 job_agent.advisor.llm_chat 同款，保持模块独立可复用）。"""

from __future__ import annotations

import httpx


def llm_chat(llm_cfg: dict, system: str, user: str,
             temperature: float = 0.3) -> str | None:
    """返回模型回复文本；未启用 / 无 key / 任何失败返回 None（调用方降级）。"""
    if not llm_cfg or not llm_cfg.get("enabled") or not llm_cfg.get("api_key"):
        return None
    try:
        r = httpx.post(
            f"{llm_cfg['base_url'].rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {llm_cfg['api_key']}"},
            json={
                "model": llm_cfg.get("model", "deepseek-chat"),
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "temperature": temperature,
            },
            timeout=float(llm_cfg.get("timeout", 90)),
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception:
        return None


def llm_json(llm_cfg: dict, system: str, user: str) -> dict | None:
    """要求模型输出 JSON 并解析；失败返回 None。"""
    import json

    raw = llm_chat(llm_cfg, system, user)
    if not raw:
        return None
    try:
        return json.loads(raw.strip().removeprefix("```json")
                          .removesuffix("```").strip())
    except Exception:
        return None


def llm_messages(llm_cfg: dict, messages: list[dict],
                 tools: list[dict] | None = None,
                 temperature: float = 0.3) -> dict | None:
    """OpenAI 兼容多轮调用（支持 function calling），返回 assistant 消息。

    返回 dict：{content, tool_calls, usage}；usage 为 token 用量
    {input, output, total, unit}（API 未返回时为 None）。
    未启用 / 无 key / 任何失败返回 None（调用方降级）。
    """
    if not llm_cfg or not llm_cfg.get("enabled") or not llm_cfg.get("api_key"):
        return None
    body: dict = {"model": llm_cfg.get("model", "deepseek-chat"),
                  "messages": messages, "temperature": temperature}
    if tools:
        body["tools"] = tools
    try:
        r = httpx.post(
            f"{llm_cfg['base_url'].rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {llm_cfg['api_key']}"},
            json=body, timeout=float(llm_cfg.get("timeout", 90)))
        r.raise_for_status()
        data = r.json()
        msg = data["choices"][0]["message"]
    except Exception:
        return None
    usage_raw = data.get("usage") or {}
    usage = None
    if usage_raw:
        usage = {"input": usage_raw.get("prompt_tokens"),
                 "output": usage_raw.get("completion_tokens"),
                 "total": usage_raw.get("total_tokens"), "unit": "TOKENS"}
    return {"content": msg.get("content"),
            "tool_calls": msg.get("tool_calls") or [],
            "usage": usage}


def llm_messages_raw(llm_cfg: dict, messages: list[dict],
                     tools: list[dict] | None = None,
                     temperature: float = 0.3):
    """同 llm_messages，但返回 (entries, usage)：entries 可直接追加进
    messages（保留 tool_calls 结构，OpenAI 协议要求 tool 回复前必须有它）。
    失败返回 (None, None)。"""
    msg = llm_messages(llm_cfg, messages, tools, temperature)
    if msg is None:
        return None, None
    entry: dict = {"role": "assistant", "content": msg.get("content")}
    if msg["tool_calls"]:
        entry["tool_calls"] = [
            {"id": tc.get("id") or f"call_{i}", "type": "function",
             "function": {"name": tc["function"]["name"],
                          "arguments": tc["function"].get("arguments") or "{}"}}
            for i, tc in enumerate(msg["tool_calls"])]
    return [entry], msg.get("usage")
