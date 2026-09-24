"""Dependency graph helpers for Agent <-> Flow references."""

from __future__ import annotations

from collections.abc import Mapping

ResourceKey = tuple[str, str]


def resource_key(resource_type: str, resource_id: str) -> ResourceKey:
    return resource_type, resource_id


def referenced_resources(resource_type: str, snapshot: dict | None) -> set[ResourceKey]:
    """Return direct Agent/Flow dependencies declared by one snapshot."""
    if not snapshot:
        return set()
    if resource_type == "agent":
        flow_id = str(snapshot.get("flow_id") or "").strip()
        return {resource_key("flow", flow_id)} if flow_id else set()
    if resource_type == "flow":
        return {
            resource_key("agent", agent_id)
            for node in snapshot.get("nodes") or []
            if node.get("type") == "ai_agent"
            and (agent_id := str((node.get("params") or {}).get("ai_agent_id") or "").strip())
        }
    return set()


def dependency_graph(resources: Mapping[ResourceKey, dict]) -> dict[ResourceKey, set[ResourceKey]]:
    graph: dict[ResourceKey, set[ResourceKey]] = {}
    for key, snapshot in resources.items():
        graph[key] = referenced_resources(key[0], snapshot)
        for target in graph[key]:
            graph.setdefault(target, set())
    return graph


def _path(graph: Mapping[ResourceKey, set[ResourceKey]], start: ResourceKey,
          target: ResourceKey) -> list[ResourceKey] | None:
    stack: list[tuple[ResourceKey, list[ResourceKey]]] = [(start, [start])]
    visited: set[ResourceKey] = set()
    while stack:
        current, path = stack.pop()
        if current == target:
            return path
        if current in visited:
            continue
        visited.add(current)
        for next_key in sorted(graph.get(current, set()), reverse=True):
            if next_key not in visited or next_key == target:
                stack.append((next_key, [*path, next_key]))
    return None


def find_cycle_involving(resources: Mapping[ResourceKey, dict],
                         focus: ResourceKey) -> list[ResourceKey] | None:
    """Find a directed cycle that includes the resource currently being changed."""
    graph = dependency_graph(resources)
    for target in sorted(graph.get(focus, set())):
        path = _path(graph, target, focus)
        if path:
            return [focus, *path]
    return None


def describe_cycle(cycle: list[ResourceKey],
                   resources: Mapping[ResourceKey, dict]) -> tuple[str, list[dict]]:
    parts: list[str] = []
    details: list[dict] = []
    for resource_type, resource_id in cycle:
        snapshot = resources.get((resource_type, resource_id)) or {}
        name = str(snapshot.get("name") or resource_id)
        label = "智能体" if resource_type == "agent" else "流程"
        parts.append(f"{label}「{name}」")
        details.append({"type": resource_type, "id": resource_id, "name": name})
    return " -> ".join(parts), details
