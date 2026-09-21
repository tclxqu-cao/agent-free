"""Flow Studio governance: auth, tenancy, RBAC, releases, policy, and audit."""

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from flow_studio.governance import (GovernanceError, GovernanceStore,
                                    hash_password, sanitize_details,
                                    verify_password)
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


def test_password_hash_and_audit_redaction():
    encoded = hash_password(PASSWORD)
    assert encoded.startswith("scrypt$")
    assert verify_password(PASSWORD, encoded)
    assert not verify_password("wrong-password", encoded)
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
