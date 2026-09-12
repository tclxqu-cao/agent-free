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
