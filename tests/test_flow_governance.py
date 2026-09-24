"""Flow Studio governance: auth, tenancy, RBAC, releases, policy, and audit."""

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from flow_studio.governance import (GovernanceError, GovernanceStore,
                                    hash_password, sanitize_details,
                                    verify_password)
from flow_studio.policy import PolicyEngine
from flow_studio.server import create_app


PASSWORD = "test-password-123"


def setup_owner(app, username="owner"):
    client = TestClient(app)
    response = client.post("/api/setup", json={
        "username": username, "display_name": username.title(),
        "password": PASSWORD,
    })
    assert response.status_code == 200, response.text
    client.headers.update({
        "X-CSRF-Token": response.json()["csrf_token"],
        "X-Workspace-ID": "default",
    })
    return client


def login(app, username, workspace="default"):
    client = TestClient(app)
    response = client.post("/api/auth/login", json={
        "username": username, "password": PASSWORD,
    })
    assert response.status_code == 200, response.text
    client.headers.update({
        "X-CSRF-Token": response.json()["csrf_token"],
        "X-Workspace-ID": workspace,
    })
    return client


def add_member(owner, username, role):
    response = owner.post("/api/workspaces/default/members", json={
        "username": username, "display_name": username.title(),
        "password": PASSWORD, "role": role,
    })
    assert response.status_code == 200, response.text
    return response.json()


def version_action(client, resource_type, resource_id, version, action, reason="ok"):
    return client.post(
        f"/api/governance/resources/{resource_type}/{resource_id}/versions/"
        f"{version}/{action}", json={"reason": reason})


def flow_body(flow_id, *agent_ids):
    nodes = [
        {"id": "start", "type": "start"},
        {"id": "end", "type": "end", "params": {"output": "ok"}},
    ]
    nodes.extend({
        "id": f"agent-{index}", "type": "ai_agent",
        "params": {"ai_agent_id": agent_id, "message": "{{input.message}}"},
    } for index, agent_id in enumerate(agent_ids, 1))
    return {
        "id": flow_id, "name": flow_id, "nodes": nodes,
        "edges": [{"from": "start", "to": "end"}],
    }


def test_external_agent_capabilities_do_not_require_local_workspace_resources():
    context = {
        "skill_ids": ["local-skill"],
        "mcp_ids": ["local-mcp"],
        "kb_ids": [],
        "policy": {},
    }
    result = PolicyEngine().evaluate("submit", "agent", {
        "orchestration": {"mode": "external_agent"},
        "skill_ids": ["customer-agent-skill"],
        "mcp_servers": ["customer-agent-mcp"],
    }, context)

    assert result.allowed


def test_local_agent_capabilities_still_require_workspace_resources():
    context = {
        "skill_ids": ["local-skill"],
        "mcp_ids": ["local-mcp"],
        "kb_ids": [],
        "policy": {},
    }
    result = PolicyEngine().evaluate("submit", "agent", {
        "orchestration": {"mode": "react"},
        "skill_ids": ["missing-skill"],
        "mcp_servers": ["missing-mcp"],
    }, context)

    assert not result.allowed
    assert [(violation.code, violation.path) for violation in result.violations] == [
        ("cross_workspace_reference", "skill_ids"),
        ("cross_workspace_reference", "mcp_servers"),
    ]


def test_external_agent_capabilities_still_honor_policy_denials():
    result = PolicyEngine().evaluate("submit", "agent", {
        "orchestration": {"mode": "external_agent"},
        "tool_ids": ["blocked-tool"],
        "mcp_servers": ["blocked-mcp"],
    }, {
        "skill_ids": [],
        "mcp_ids": [],
        "kb_ids": [],
        "policy": {
            "denied_tools": ["blocked-tool"],
            "denied_mcp_servers": ["blocked-mcp"],
        },
    })

    assert not result.allowed
    assert [(violation.code, violation.path) for violation in result.violations] == [
        ("tool_denied", "tool_ids"),
        ("mcp_denied", "mcp_servers"),
    ]


def test_password_hash_and_audit_redaction():
    encoded = hash_password("123")
    assert encoded.startswith("scrypt$")
    assert verify_password("123", encoded)
    assert not verify_password("wrong-password", encoded)
    with pytest.raises(GovernanceError) as caught:
        hash_password("")
    assert caught.value.code == "invalid_password"
    cleaned = sanitize_details({
        "password": "secret", "nested": {"api_key": "key", "safe": "ok"},
    })
    assert cleaned == {"password": "[REDACTED]",
                       "nested": {"api_key": "[REDACTED]", "safe": "ok"}}


def test_login_lockout_and_session_expiry(tmp_path):
    store = GovernanceStore(tmp_path / "governance.sqlite")
    user = store.setup_owner("owner", PASSWORD, "Owner")
    for _ in range(5):
        with pytest.raises(GovernanceError) as caught:
            store.authenticate("owner", "bad-password", "127.0.0.1")
        assert caught.value.code == "invalid_credentials"
    with pytest.raises(GovernanceError) as caught:
        store.authenticate("owner", PASSWORD, "127.0.0.1")
    assert caught.value.code == "login_locked"

    session = store.create_session(user["user_id"])
    principal = store.resolve_session(session["token"], "default", "req-1")
    assert principal.role == "owner" and principal.csrf_token == session["csrf_token"]
    store.revoke_session(session["token"])
    with pytest.raises(GovernanceError) as caught:
        store.resolve_session(session["token"], "default", "req-2")
    assert caught.value.code == "invalid_session"


def test_setup_auth_csrf_and_logout(config_dir, tmp_path):
    app = create_app(config_dir, tmp_path / "data")
    anonymous = TestClient(app)
    assert anonymous.get("/api/setup/status").json() == {"initialized": False}
    blocked = anonymous.get("/api/flows")
    assert blocked.status_code == 503 and blocked.json()["code"] == "setup_required"

    owner = setup_owner(app)
    assert owner.get("/api/setup/status").json() == {"initialized": True}
    assert owner.get("/api/me").json()["workspace"]["role"] == "owner"

    no_csrf = TestClient(app)
    login_response = no_csrf.post("/api/auth/login", json={
        "username": "owner", "password": PASSWORD})
    assert login_response.status_code == 200
    denied = no_csrf.post("/api/kb", json={"id": "x", "name": "X"})
    assert denied.status_code == 403 and denied.json()["code"] == "csrf_failed"

    logout = owner.post("/api/auth/logout")
    assert logout.status_code == 200
    assert owner.get("/api/me").status_code == 401


def test_four_role_release_workflow_and_policy(config_dir, tmp_path):
    app = create_app(config_dir, tmp_path / "data")
    owner = setup_owner(app)
    add_member(owner, "admin", "admin")
    add_member(owner, "editor", "editor")
    add_member(owner, "viewer", "viewer")
    admin, editor, viewer = (login(app, name) for name in ("admin", "editor", "viewer"))

    forbidden = viewer.post("/api/ai-agents", json={"id": "blocked", "name": "X"})
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "permission_denied"

    created = editor.post("/api/ai-agents", json={
        "id": "governed", "name": "Governed", "system": "Be precise.",
        "tool_ids": ["calc"], "max_steps": 5,
    })
    assert created.status_code == 200, created.text
    assert created.json()["_governance"] == {
        "version": 1, "status": "draft", "action": "upsert",
        "published_version": None,
        "created_by": created.json()["_governance"]["created_by"],
    }
    assert viewer.get("/api/ai-agents/governed").status_code == 404

    submitted = version_action(editor, "agent", "governed", 1, "submit")
    assert submitted.status_code == 200 and submitted.json()["status"] == "pending"
    assert version_action(editor, "agent", "governed", 1, "approve").status_code == 403

    approved = version_action(admin, "agent", "governed", 1, "approve")
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    published = version_action(admin, "agent", "governed", 1, "publish")
    assert published.status_code == 200 and published.json()["status"] == "published"
    assert viewer.get("/api/ai-agents/governed").json()["name"] == "Governed"
    invoked = viewer.post("/api/ai-agents/governed/invoke", json={"message": "hello"})
    assert invoked.status_code == 200

    rolled = version_action(admin, "agent", "governed", 1, "rollback", "restore v1")
    assert rolled.status_code == 200
    assert rolled.json()["rollback_of"] == published.json()["version_id"]
    assert rolled.json()["version_no"] == 2

    policy = owner.put("/api/governance/policy", json={"denied_tools": ["calc"]})
    assert policy.status_code == 200 and policy.json()["denied_tools"] == ["calc"]
    second = editor.put("/api/ai-agents/governed", json={
        "name": "Governed 2", "tool_ids": ["calc"], "max_steps": 5})
    assert second.status_code == 200
    denied_submit = version_action(editor, "agent", "governed", 3, "submit")
    assert denied_submit.status_code == 403
    assert denied_submit.json()["code"] == "tool_denied"
    assert denied_submit.json()["policy"]["violations"][0]["path"] == "tool_ids"

    audit = admin.get("/api/governance/audit").json()
    event_types = {event["event_type"] for event in audit}
    assert {"version.submitted", "version.approved", "version.published",
            "version.rolled_back", "policy.denied"} <= event_types


def test_workspace_isolation_with_same_resource_id(config_dir, tmp_path):
    app = create_app(config_dir, tmp_path / "data")
    owner = setup_owner(app)
    created = owner.post("/api/workspaces", json={"id": "team-b", "name": "Team B"})
    assert created.status_code == 200

    default_agent = owner.put("/api/ai-agents/kb-assistant", json={
        "name": "Default Assistant", "system": "default"})
    assert default_agent.status_code == 200

    owner.headers["X-Workspace-ID"] = "team-b"
    team_agent = owner.put("/api/ai-agents/kb-assistant", json={
        "name": "Team B Assistant", "system": "team b"})
    assert team_agent.status_code == 200
    assert owner.get("/api/ai-agents/kb-assistant").json()["name"] == "Team B Assistant"

    owner.headers["X-Workspace-ID"] = "default"
    assert owner.get("/api/ai-agents/kb-assistant").json()["name"] == "Default Assistant"
    mismatch = owner.get("/api/workspaces/team-b/members")
    assert mismatch.status_code == 403 and mismatch.json()["code"] == "workspace_mismatch"


def test_legacy_migration_is_copy_only_and_idempotent(config_dir, tmp_path):
    data = tmp_path / "data"
    flows = data / "flows"
    flows.mkdir(parents=True)
    legacy = {
        "version": 1, "id": "legacy", "name": "Legacy", "description": "",
        "triggers": [],
        "nodes": [{"id": "start", "type": "start", "params": {}},
                  {"id": "end", "type": "end", "params": {"output": "ok"}}],
        "edges": [{"from": "start", "to": "end"}],
    }
    (flows / "legacy.json").write_text(json.dumps(legacy), encoding="utf-8")

    app = create_app(config_dir, data)
    owner = setup_owner(app)
    assert (flows / "legacy.json").exists()
    assert (data / "workspaces" / "default" / "flows" / "legacy.json").exists()
    versions = owner.get("/api/governance/resources/flow/legacy/versions").json()
    assert len(versions) == 1 and versions[0]["status"] == "published"

    app2 = create_app(config_dir, data)
    owner2 = login(app2, "owner")
    versions2 = owner2.get("/api/governance/resources/flow/legacy/versions").json()
    assert len(versions2) == 1


def test_agent_save_rejects_cycle_with_flow_that_references_it(config_dir, tmp_path):
    app = create_app(config_dir, tmp_path / "data")
    owner = setup_owner(app)
    assert owner.post("/api/ai-agents", json={
        "id": "cycle-agent", "name": "Cycle Agent",
    }).status_code == 200
    assert owner.post("/api/flows", json=flow_body(
        "cycle-flow", "cycle-agent")).status_code == 200

    blocked = owner.put("/api/ai-agents/cycle-agent", json={
        "name": "Cycle Agent", "flow_id": "cycle-flow",
    })
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "dependency_cycle"
    assert [item["id"] for item in blocked.json()["policy"]["cycle"]] == [
        "cycle-agent", "cycle-flow", "cycle-agent"]


def test_flow_save_rejects_cycle_with_agent_bound_to_it(config_dir, tmp_path):
    app = create_app(config_dir, tmp_path / "data")
    owner = setup_owner(app)
    assert owner.post("/api/flows", json=flow_body("bound-flow")).status_code == 200
    assert owner.post("/api/ai-agents", json={
        "id": "bound-agent", "name": "Bound Agent", "flow_id": "bound-flow",
    }).status_code == 200

    blocked = owner.put("/api/flows/bound-flow", json=flow_body(
        "bound-flow", "bound-agent"))
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "dependency_cycle"


def test_indirect_dependency_cycle_is_rejected(config_dir, tmp_path):
    app = create_app(config_dir, tmp_path / "data")
    owner = setup_owner(app)
    assert owner.post("/api/flows", json=flow_body("flow-one")).status_code == 200
    assert owner.post("/api/flows", json=flow_body("flow-two")).status_code == 200
    assert owner.post("/api/ai-agents", json={
        "id": "agent-one", "name": "Agent One", "flow_id": "flow-one",
    }).status_code == 200
    assert owner.post("/api/ai-agents", json={
        "id": "agent-two", "name": "Agent Two", "flow_id": "flow-two",
    }).status_code == 200
    assert owner.put("/api/flows/flow-one", json=flow_body(
        "flow-one", "agent-two")).status_code == 200

    blocked = owner.put("/api/flows/flow-two", json=flow_body(
        "flow-two", "agent-one"))
    assert blocked.status_code == 409
    assert [item["id"] for item in blocked.json()["policy"]["cycle"]] == [
        "flow-two", "agent-one", "flow-one", "agent-two", "flow-two"]


def test_removing_dependency_allows_previously_blocked_binding(config_dir, tmp_path):
    app = create_app(config_dir, tmp_path / "data")
    owner = setup_owner(app)
    assert owner.post("/api/ai-agents", json={
        "id": "editable-agent", "name": "Editable Agent",
    }).status_code == 200
    assert owner.post("/api/flows", json=flow_body(
        "editable-flow", "editable-agent")).status_code == 200
    assert owner.put("/api/ai-agents/editable-agent", json={
        "name": "Editable Agent", "flow_id": "editable-flow",
    }).status_code == 409

    assert owner.put("/api/flows/editable-flow", json=flow_body(
        "editable-flow")).status_code == 200
    allowed = owner.put("/api/ai-agents/editable-agent", json={
        "name": "Editable Agent", "flow_id": "editable-flow",
    })
    assert allowed.status_code == 200
