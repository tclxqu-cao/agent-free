"""Workspace policy normalization and deterministic governance gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from .governance import DEFAULT_POLICY


@dataclass(frozen=True)
class PolicyViolation:
    code: str
    message: str
    path: str = ""

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "path": self.path}


@dataclass
class PolicyResult:
    decision: str = "allow"
    violations: list[PolicyViolation] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.decision != "deny"

    @property
    def primary_code(self) -> str:
        return self.violations[0].code if self.violations else "policy_allowed"

    def to_dict(self) -> dict:
        return {"decision": self.decision,
                "violations": [v.to_dict() for v in self.violations]}


class PolicyEngine:
    """Evaluate Agent/Flow snapshots at submit, publish, preview, or run."""

    def normalize(self, policy: dict | None) -> dict:
        merged = {**DEFAULT_POLICY, **(policy or {})}
        merged["max_agent_steps"] = max(1, min(30, int(merged["max_agent_steps"])))
        merged["min_eval_pass_rate"] = max(
            0.0, min(1.0, float(merged["min_eval_pass_rate"])))
        for key in ("allowed_models", "denied_tools", "denied_mcp_servers",
                    "allowed_flow_node_types", "allowed_http_hosts"):
            merged[key] = sorted({str(v).strip() for v in merged.get(key, [])
                                  if str(v).strip()})
        merged["require_approval"] = bool(merged["require_approval"])
        merged["required_eval_suite_id"] = str(
            merged.get("required_eval_suite_id") or "").strip()
        return merged

    def evaluate(self, stage: str, resource_type: str, snapshot: dict | None,
                 context: dict | None = None) -> PolicyResult:
        context = context or {}
        policy = self.normalize(context.get("policy"))
        violations: list[PolicyViolation] = []
        if not snapshot:
            return PolicyResult("allow", [])
        if resource_type == "agent":
            self._agent(snapshot, policy, violations, context)
        elif resource_type == "flow":
            self._flow(snapshot, policy, violations, context)
        else:
            violations.append(PolicyViolation(
                "invalid_resource_type", "策略不支持该资源类型", "resource_type"))

        if stage == "publish":
            if policy["require_approval"] and not context.get("approved"):
                violations.append(PolicyViolation(
                    "approval_required", "发布前必须完成审批", "status"))
            suite_id = policy["required_eval_suite_id"]
            if suite_id:
                rate = context.get("eval_pass_rate")
                if rate is None or float(rate) < policy["min_eval_pass_rate"]:
                    violations.append(PolicyViolation(
                        "evaluation_required",
                        f"评测集 {suite_id} 通过率需达到 {policy['min_eval_pass_rate']:.0%}",
                        "evaluation"))
        return PolicyResult("deny" if violations else "allow", violations)

    @staticmethod
    def _agent(snapshot: dict, policy: dict, out: list[PolicyViolation],
               context: dict) -> None:
        allowed_models = set(policy["allowed_models"])
        model = str(snapshot.get("model") or "").strip()
        if allowed_models and model and model not in allowed_models:
            out.append(PolicyViolation(
                "model_denied", f"模型 {model} 不在允许清单中", "model"))
        if int(snapshot.get("max_steps") or 8) > policy["max_agent_steps"]:
            out.append(PolicyViolation(
                "max_steps_exceeded",
                f"最大工具步数不能超过 {policy['max_agent_steps']}", "max_steps"))
        denied_tools = set(policy["denied_tools"])
        for tool in snapshot.get("tool_ids") or []:
            if str(tool) in denied_tools:
                out.append(PolicyViolation(
                    "tool_denied", f"工具 {tool} 已被策略禁用", "tool_ids"))
        denied_mcp = set(policy["denied_mcp_servers"])
        for server in snapshot.get("mcp_servers") or []:
            if str(server) in denied_mcp:
                out.append(PolicyViolation(
                    "mcp_denied", f"MCP {server} 已被策略禁用", "mcp_servers"))
        refs = {
            "flow_id": context.get("flow_ids"),
        }
        for key, known in refs.items():
            value = str(snapshot.get(key) or "")
            if value and known is not None and value not in set(known):
                out.append(PolicyViolation(
                    "cross_workspace_reference",
                    f"引用的资源 {value} 不属于当前 Workspace", key))
        external_agent = (
            (snapshot.get("orchestration") or {}).get("mode") == "external_agent"
        )
        local_references = [("kb_ids", "kb_ids")]
        if not external_agent:
            local_references.extend((
                ("skill_ids", "skill_ids"),
                ("mcp_servers", "mcp_ids"),
            ))
        for key, known_key in local_references:
            known = context.get(known_key)
            if known is None:
                continue
            for value in snapshot.get(key) or []:
                if str(value) not in set(known):
                    out.append(PolicyViolation(
                        "cross_workspace_reference",
                        f"引用的资源 {value} 不属于当前 Workspace", key))

    @staticmethod
    def _flow(snapshot: dict, policy: dict, out: list[PolicyViolation],
              context: dict) -> None:
        allowed_types = set(policy["allowed_flow_node_types"])
        allowed_hosts = {host.lower() for host in policy["allowed_http_hosts"]}
        known_agents = context.get("agent_ids")
        known_flows = context.get("flow_ids")
        for index, node in enumerate(snapshot.get("nodes") or []):
            ntype = str(node.get("type") or "")
            path = f"nodes[{index}]"
            if allowed_types and ntype not in allowed_types:
                out.append(PolicyViolation(
                    "node_type_denied", f"节点类型 {ntype} 不在允许清单中", f"{path}.type"))
            params = node.get("params") or {}
            if ntype == "http" and allowed_hosts:
                rendered = str(params.get("url") or "")
                host = (urlparse(rendered).hostname or "").lower()
                if not host or host not in allowed_hosts:
                    out.append(PolicyViolation(
                        "http_host_denied", f"HTTP 主机 {host or rendered} 不在允许清单中",
                        f"{path}.params.url"))
            if ntype == "ai_agent" and known_agents is not None:
                agent_id = str(params.get("ai_agent_id") or params.get("agent_id")
                               or params.get("id") or "")
                if agent_id and agent_id not in set(known_agents):
                    out.append(PolicyViolation(
                        "cross_workspace_reference",
                        f"引用的智能体 {agent_id} 不属于当前 Workspace",
                        f"{path}.params.agent_id"))
            if ntype == "subflow" and known_flows is not None:
                flow_id = str(params.get("flow_id") or "")
                if flow_id and flow_id not in set(known_flows):
                    out.append(PolicyViolation(
                        "cross_workspace_reference",
                        f"引用的流程 {flow_id} 不属于当前 Workspace",
                        f"{path}.params.flow_id"))
