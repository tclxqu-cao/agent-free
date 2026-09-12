"""Agent 注册表：把本地 agent（如 job_agent）的能力注册为可编排节点动作。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class AgentAction:
    agent: str                       # agent id，如 "job_agent"
    action: str                      # 动作 id，如 "match_today"
    description: str = ""
    params: list[dict] = field(default_factory=list)  # [{key,type,required,description}]
    fn: Callable[[dict], dict] = None  # noqa: RUF013 入参/出参均为 JSON 兼容 dict

    @property
    def full_id(self) -> str:
        return f"{self.agent}.{self.action}"


class AgentRegistry:
    def __init__(self) -> None:
        self._actions: dict[str, AgentAction] = {}

    def register(self, agent: str, action: str, fn: Callable[[dict], dict],
                 description: str = "",
                 params: list[dict] | None = None) -> AgentAction:
        entry = AgentAction(agent=agent, action=action, fn=fn,
                            description=description, params=params or [])
        self._actions[entry.full_id] = entry
        return entry

    def get(self, agent: str, action: str) -> AgentAction | None:
        return self._actions.get(f"{agent}.{action}")

    def agents(self) -> list[dict]:
        """能力目录：[{agent, description, actions:[{action, description, params}]}]"""
        grouped: dict[str, dict] = {}
        for e in self._actions.values():
            g = grouped.setdefault(e.agent, {"agent": e.agent, "actions": []})
            g["actions"].append({"action": e.action, "description": e.description,
                                 "params": e.params})
        return sorted(grouped.values(), key=lambda g: g["agent"])
