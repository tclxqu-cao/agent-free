"""Immutable Agent/Flow versions, approval workflow, release, and rollback."""

from __future__ import annotations

from .governance import GovernanceError, Principal
from .graph import graph_from_dict
from .policy import PolicyEngine, PolicyResult


class VersionService:
    def __init__(self, governance, workspaces, policy: PolicyEngine | None = None):
        self.governance = governance
        self.workspaces = workspaces
        self.policy = policy or PolicyEngine()

    def _version(self, principal: Principal, resource_type: str, resource_id: str,
                 version_no: int) -> dict:
        version = self.governance.get_version(
            principal.workspace_id, resource_type, resource_id, int(version_no))
        if not version:
            raise GovernanceError("version_not_found", "版本不存在", 404)
        return version

    def _context(self, workspace_id: str, resource_type: str,
                 resource_id: str, version: dict | None = None) -> dict:
        runtime = self.workspaces.get(workspace_id)
        policy = self.governance.get_policy(workspace_id)
        context = {
            "policy": policy,
            "approved": bool(version and version.get("status") in ("approved", "published")),
            "flow_ids": [v["resource_id"] for v in
                         self.governance.list_releases(workspace_id, "flow")],
            "agent_ids": [v["resource_id"] for v in
                          self.governance.list_releases(workspace_id, "agent")],
            "kb_ids": [k["id"] for k in runtime.kb.list_kbs()],
            "skill_ids": [s["id"] for s in runtime.skills.list()],
            "mcp_ids": list((runtime.mcp.load().get("servers") or {}).keys()),
        }
        suite_id = policy.get("required_eval_suite_id")
        if suite_id:
            prefix = "platform" if resource_type == "agent" else "flow"
            context["eval_pass_rate"] = runtime.evals.latest_pass_rate(
                suite_id, f"{prefix}:{resource_id}")
        return context

    @staticmethod
    def _raise_policy(result: PolicyResult) -> None:
        if result.allowed:
            return
        raise GovernanceError(
            result.primary_code, "策略门禁拒绝了该操作", 403, result.to_dict())

    def evaluate(self, workspace_id: str, stage: str, resource_type: str,
                 resource_id: str, snapshot: dict | None,
                 version: dict | None = None) -> PolicyResult:
        return self.policy.evaluate(
            stage, resource_type, snapshot,
            self._context(workspace_id, resource_type, resource_id, version))

    def save_draft(self, principal: Principal, resource_type: str,
                   resource_id: str, snapshot: dict | None,
                   action: str = "upsert") -> dict:
        if action == "upsert" and not isinstance(snapshot, dict):
            raise GovernanceError("invalid_snapshot", "资源快照必须是对象", 422)
        if snapshot is not None:
            snapshot = dict(snapshot)
            snapshot["id"] = resource_id
            if resource_type == "flow":
                graph_from_dict(snapshot)
        version = self.governance.save_version(
            principal.workspace_id, resource_type, resource_id,
            snapshot, action, principal.user_id)
        self._audit(principal, "version.draft_saved", version,
                    {"action": action, "content_hash": version["content_hash"]})
        return self.decorate(version)

    def submit(self, principal: Principal, resource_type: str,
               resource_id: str, version_no: int, reason: str = "") -> dict:
        version = self._version(principal, resource_type, resource_id, version_no)
        result = self.evaluate(principal.workspace_id, "submit", resource_type,
                               resource_id, version.get("snapshot"), version)
        self._raise_policy(result)
        updated = self.governance.set_version_status(
            version["version_id"], ("draft",), "pending", principal.user_id, reason)
        self._audit(principal, "version.submitted", updated, {"reason": reason})
        return self.decorate(updated)

    def approve(self, principal: Principal, resource_type: str,
                resource_id: str, version_no: int, reason: str = "") -> dict:
        version = self._version(principal, resource_type, resource_id, version_no)
        same_author = version.get("submitted_by") == principal.user_id \
            or version.get("created_by") == principal.user_id
        self_approved = False
        if same_author:
            self_approved = principal.role == "owner" and \
                self.governance.approver_count(principal.workspace_id) == 1
            if not self_approved:
                raise GovernanceError(
                    "self_approval_forbidden", "提交人不能批准自己的版本", 403)
        updated = self.governance.set_version_status(
            version["version_id"], ("pending",), "approved", principal.user_id, reason)
        self._audit(principal, "version.approved", updated,
                    {"reason": reason, "self_approved": self_approved})
        return self.decorate(updated)

    def reject(self, principal: Principal, resource_type: str,
               resource_id: str, version_no: int, reason: str) -> dict:
        if not reason.strip():
            raise GovernanceError("reason_required", "拒绝时必须填写原因", 422)
        version = self._version(principal, resource_type, resource_id, version_no)
        updated = self.governance.set_version_status(
            version["version_id"], ("pending",), "rejected", principal.user_id, reason)
        self._audit(principal, "version.rejected", updated, {"reason": reason})
        return self.decorate(updated)

    def publish(self, principal: Principal, resource_type: str,
                resource_id: str, version_no: int, reason: str = "") -> dict:
        version = self._version(principal, resource_type, resource_id, version_no)
        result = self.evaluate(principal.workspace_id, "publish", resource_type,
                               resource_id, version.get("snapshot"), version)
        self._raise_policy(result)
        require_approval = self.governance.get_policy(
            principal.workspace_id).get("require_approval", True)
        allowed_statuses = ("approved",) if require_approval else ("draft", "pending", "approved")
        if version["status"] == "published":
            return self.decorate(version)
        if version["status"] not in allowed_statuses:
            raise GovernanceError(
                "version_conflict", f"版本当前状态为 {version['status']}，不能发布", 409)
        published, old_release = self.governance.set_release(
            version["version_id"], principal.user_id, reason)
        try:
            self._materialize(published)
        except Exception as exc:
            self.governance.restore_release(version["version_id"], old_release)
            self._audit(principal, "version.publish_failed", version,
                        {"error": f"{type(exc).__name__}: {exc}"}, outcome="failed")
            raise GovernanceError(
                "materialization_failed", f"发布文件写入失败：{exc}", 500) from exc
        self._audit(principal, "version.published", published, {"reason": reason})
        return self.decorate(published)

    def rollback(self, principal: Principal, resource_type: str,
                 resource_id: str, version_no: int, reason: str) -> dict:
        if not reason.strip():
            raise GovernanceError("reason_required", "回滚时必须填写原因", 422)
        source = self._version(principal, resource_type, resource_id, version_no)
        if not source.get("published_at"):
            raise GovernanceError("rollback_source_invalid", "只能回滚到历史发布版本", 409)
        self._raise_policy(self.evaluate(
            principal.workspace_id, "publish", resource_type, resource_id,
            source.get("snapshot"), source))
        created = self.governance.save_version(
            principal.workspace_id, resource_type, resource_id,
            source.get("snapshot"), source["action"], principal.user_id,
            rollback_of=source["version_id"])
        created = self.governance.set_version_status(
            created["version_id"], ("draft",), "pending", principal.user_id, reason)
        created = self.governance.set_version_status(
            created["version_id"], ("pending",), "approved", principal.user_id, reason)
        published, old_release = self.governance.set_release(
            created["version_id"], principal.user_id, reason)
        try:
            self._materialize(published)
        except Exception as exc:
            self.governance.restore_release(created["version_id"], old_release)
            raise GovernanceError(
                "materialization_failed", f"回滚文件写入失败：{exc}", 500) from exc
        self._audit(principal, "version.rolled_back", published,
                    {"reason": reason, "rollback_of": source["version_id"]})
        return self.decorate(published)

    def preview_snapshot(self, principal: Principal, resource_type: str,
                         resource_id: str, version_no: int) -> dict:
        version = self._version(principal, resource_type, resource_id, version_no)
        result = self.evaluate(principal.workspace_id, "preview", resource_type,
                               resource_id, version.get("snapshot"), version)
        self._raise_policy(result)
        return version

    def published_snapshot(self, workspace_id: str, resource_type: str,
                           resource_id: str) -> dict:
        version = self.governance.get_release(workspace_id, resource_type, resource_id)
        if not version or version.get("action") == "delete":
            raise GovernanceError("published_resource_not_found", "资源尚未发布", 404)
        result = self.evaluate(workspace_id, "run", resource_type, resource_id,
                               version.get("snapshot"), version)
        self._raise_policy(result)
        return version

    def effective_version(self, principal: Principal, resource_type: str,
                          resource_id: str) -> dict | None:
        if principal.role != "viewer":
            latest = self.governance.latest_version(
                principal.workspace_id, resource_type, resource_id)
            if latest and latest.get("action") != "delete":
                return latest
        release = self.governance.get_release(
            principal.workspace_id, resource_type, resource_id)
        return release if release and release.get("action") != "delete" else None

    def list_effective(self, principal: Principal, resource_type: str) -> list[dict]:
        ids = {v["resource_id"] for v in self.governance.list_releases(
            principal.workspace_id, resource_type)}
        if principal.role != "viewer":
            runtime = self.workspaces.get(principal.workspace_id)
            materialized = runtime.ai_agents.list() if resource_type == "agent" \
                else [{"id": graph.id} for graph in runtime.flows.list()]
            ids.update(item["id"] for item in materialized)
            rows = self.governance.conn.execute(
                "SELECT DISTINCT resource_id FROM resource_versions "
                "WHERE workspace_id=? AND resource_type=?",
                (principal.workspace_id, resource_type)).fetchall()
            ids.update(row[0] for row in rows)
        return [v for rid in sorted(ids)
                if (v := self.effective_version(principal, resource_type, rid))]

    def decorate(self, version: dict) -> dict:
        release = self.governance.get_release(
            version["workspace_id"], version["resource_type"], version["resource_id"])
        return {**version, "published_version": release.get("version_no") if release else None}

    def _materialize(self, version: dict) -> None:
        runtime = self.workspaces.get(version["workspace_id"])
        resource_id = version["resource_id"]
        if version["action"] == "delete":
            if version["resource_type"] == "flow":
                runtime.flows.delete(resource_id)
            else:
                runtime.ai_agents.delete(resource_id)
            return
        snapshot = version.get("snapshot") or {}
        if version["resource_type"] == "flow":
            runtime.flows.save(graph_from_dict(snapshot))
        else:
            runtime.ai_agents.save(snapshot)

    def _audit(self, principal: Principal, event_type: str, version: dict,
               details: dict, outcome: str = "success") -> None:
        self.governance.audit(
            event_type, workspace_id=principal.workspace_id,
            user_id=principal.user_id, username=principal.username,
            resource_type=version.get("resource_type"),
            resource_id=version.get("resource_id"),
            version_no=version.get("version_no"), request_id=principal.request_id,
            outcome=outcome, details=details)
