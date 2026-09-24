"""Load built-in Agent assets from package data."""

from __future__ import annotations

import json
from pathlib import Path


DEFAULT_AGENTS_PATH = Path(__file__).with_name("default_agents.json")


def load_default_agents(path: Path = DEFAULT_AGENTS_PATH) -> list[dict]:
    """Return validated default Agent records without normalizing user fields."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("内置智能体配置版本无效")
    agents = payload.get("agents")
    if not isinstance(agents, list) or not agents:
        raise ValueError("内置智能体配置缺少 agents")
    ids: set[str] = set()
    output: list[dict] = []
    for item in agents:
        if not isinstance(item, dict):
            raise ValueError("内置智能体配置条目必须是对象")
        agent_id = str(item.get("id") or "").strip()
        if not agent_id or agent_id in ids:
            raise ValueError(f"内置智能体 id 缺失或重复：{agent_id!r}")
        ids.add(agent_id)
        output.append(item)
    return output
