"""Flow Studio：FastAPI 服务集成测试（TestClient，不起真实端口）。"""

import pytest

pytest.importorskip("fastapi")

from flow_studio.server import create_app  # noqa: E402
from conftest import make_governed_client  # noqa: E402


@pytest.fixture
def client(config_dir, tmp_path):
    return make_governed_client(create_app(config_dir, tmp_path / "data"))


def test_health_and_static(client):
    assert client.get("/api/health").json() == {"ok": True}
    page = client.get("/")
    assert page.status_code == 200 and "Flow Studio" in page.text


def test_node_types_and_agents(client):
    types = client.get("/api/node-types").json()
    assert {"start", "end", "llm", "agent", "condition", "template", "http"} <= set(types)
    assert any(field["key"] == "source" for field in types["condition"]["form"])
    agents = client.get("/api/agents").json()
    job = next(a for a in agents if a["agent"] == "job_agent")
    assert any(a["action"] == "match_today" for a in job["actions"])


def test_condition_builder_is_served(client):
    script = client.get("/app.js")
    assert script.status_code == 200
    assert "askConditionRule" in script.text
    assert "CONDITION_OPERATORS" in script.text
    assert "data-condition-edge" in script.text


def test_saved_graph_rebuilds_transient_edge_ids(client):
    script = client.get("/app.js").text
    save_block = script.split("async function saveFlow()", 1)[1].split(
        "function graphBody()", 1)[0]
    assert "state.graph = saved;" in save_block
    assert "state.sel = null;\n    renderWorld();\n    renderInspector();" in save_block


def test_external_agent_editor_is_served(client):
    script = client.get("/app.js").text
    style = client.get("/style.css").text
    assert 'data-ag-mode="react"' in script
    assert 'data-ag-mode="external_agent"' in script
    assert 'data-ag-mode="flow"' in script
    assert "/api/ai-agents/external/catalog" in script
    assert "mcpServers" in script and "ag-ca-memory" in script
    assert "toolPolicies" in script and "ag-ca-tool-policy" in script
    assert "ag-ca-include-identity" in script
    assert "include_identity_instructions" in script
    assert "agentOrchestrationMode" in script and "第三方智能体" in script
    assert "agentPendingDelete" in script and "删除草稿" in script
    assert ".ag-cap-option[hidden]" in style


def test_compact_agent_cards_and_theme_toggle_are_served(client):
    page = client.get("/").text
    script = client.get("/app.js").text
    style = client.get("/style.css").text

    assert 'id="btn-theme"' in page
    assert "flow-studio-theme" in page and "flow-studio-theme" in script
    assert 'data-theme="light"' in style
    assert "agentPendingDelete" in script
    home = script.split("function renderAgentsHome()", 1)[1].split(
        "/* ================= 智能体对话面板", 1)[0]
    assert 'class="res-tags"' not in home
    assert 'class="agent-ico"' not in home and 'class="agent-id"' not in home
    assert "grid-template-columns: repeat(auto-fit, minmax(68px, 1fr))" in style


def test_mobile_navigation_and_compact_flow_cards_are_served(client):
    page = client.get("/").text
    script = client.get("/app.js").text
    style = client.get("/style.css").text

    assert 'class="nav-actions"' in page
    assert 'id="btn-logout"' in page and 'aria-label="退出登录"' in page
    assert 'grid-template-areas: "brand tabs" "actions actions"' in style
    assert ".nav-actions .icon-btn" in style
    flow_home = script.split("function renderFlowsHome()", 1)[1].split(
        "function agentOrchestrationMode", 1)[0]
    assert 'class="flow-desc"' not in flow_home


def test_agent_delete_draft_is_visible_until_published(client):
    created = client.post("/api/ai-agents", json={
        "id": "delete-visible", "name": "Delete Visible",
    })
    assert created.status_code == 200
    for action in ("submit", "approve", "publish"):
        response = client.post(
            "/api/governance/resources/agent/delete-visible/versions/1/" + action,
            json={"reason": "prepare deletion test"},
        )
        assert response.status_code == 200, response.text

    deleted = client.delete("/api/ai-agents/delete-visible")
    assert deleted.status_code == 200
    assert deleted.json()["_governance"] == {
        "version": 2,
        "status": "draft",
        "action": "delete",
        "published_version": 1,
        "created_by": deleted.json()["_governance"]["created_by"],
    }

    listed = client.get("/api/ai-agents").json()
    agent = next(item for item in listed if item["id"] == "delete-visible")
    assert agent["_governance"]["version"] == 1
    assert agent["_governance"]["status"] == "published"
    assert agent["_governance"]["pending_delete"] == {
        "version": 2,
        "status": "draft",
        "action": "delete",
        "created_by": deleted.json()["_governance"]["created_by"],
    }


def test_external_agent_credentials_and_catalog_proxy(client, monkeypatch):
    captured = {}

    class FakeProvider:
        def __init__(self, base_url, token, timeout):
            captured.update(base_url=base_url, token=token, timeout=timeout)

        def catalog(self):
            return {"protocolVersion": "1", "provider": {"id": "customer-agent"},
                    "features": {"streaming": True, "memory": True,
                                 "cancellation": True},
                    "models": [], "skills": [], "tools": [], "mcpServers": []}

    monkeypatch.setattr("flow_studio.server.CustomerAgentProvider", FakeProvider)
    ref = "ca-test"
    status = client.get(f"/api/ai-agents/external/credentials/{ref}").json()
    assert status == {"credential_ref": ref, "configured": False}
    fetched = client.post("/api/ai-agents/external/catalog", json={
        "provider": "customer-agent", "base_url": "http://127.0.0.1:3000",
        "credential_ref": ref, "token": "one-shot",
    })
    assert fetched.status_code == 200 and captured["token"] == "one-shot"
    assert client.get(f"/api/ai-agents/external/credentials/{ref}").json()["configured"] is False
    saved = client.put(f"/api/ai-agents/external/credentials/{ref}",
                       json={"token": "stored"}).json()
    assert saved["configured"] is True
    client.put(f"/api/ai-agents/external/credentials/{ref}", json={"clear": True})
    assert client.get(f"/api/ai-agents/external/credentials/{ref}").json()["configured"] is False


def test_flow_crud_and_validation(client):
    flows = client.get("/api/flows").json()
    assert {f["id"] for f in flows} >= {"job-hunt-demo", "job-hunt-daily",
                                        "job-intent-demo"}

    full = client.get("/api/flows/job-hunt-demo").json()
    assert full["name"].startswith("应聘 Agent")
    assert len(full["nodes"]) >= 8

    intent_flow = client.get("/api/flows/job-intent-demo").json()
    assert any(n["type"] == "intent" for n in intent_flow["nodes"])

    # 新建：缺开始节点 → 422
    bad = client.post("/api/flows", json={
        "id": "x", "name": "X", "nodes": [{"id": "a", "type": "template"}], "edges": []})
    assert bad.status_code == 422

    ok = client.post("/api/flows", json={
        "id": "mini", "name": "最小流程",
        "nodes": [{"id": "start", "type": "start"},
                  {"id": "end", "type": "end", "params": {"output": "你好 {{input.who}}"}}],
        "edges": [{"from": "start", "to": "end"}]})
    assert ok.status_code == 200
    assert client.get("/api/flows/mini").json()["name"] == "最小流程"

    dup = client.post("/api/flows", json={"id": "mini", "name": "重复"})
    assert dup.status_code == 409

    upd = client.put("/api/flows/mini", json={
        "id": "mini", "name": "改名", "triggers": ["打个招呼"],
        "nodes": [{"id": "start", "type": "start"},
                  {"id": "end", "type": "end", "params": {"output": "改名后"}}],
        "edges": [{"from": "start", "to": "end"}]})
    assert upd.status_code == 200 and upd.json()["name"] == "改名"

    deleted = client.delete("/api/flows/mini").json()
    assert deleted["delete_draft"] == "mini"
    assert deleted["_governance"]["action"] == "delete"
    assert client.get("/api/flows/mini").status_code == 404


def test_run_flow_and_runs(client):
    run = client.post("/api/flows/job-hunt-demo/run", json={"inputs": {}})
    assert run.status_code == 200
    data = run.json()
    assert data["status"] == "success"
    assert "应聘 Agent" in data["output"]
    run_id = data["run_id"]

    runs = client.get("/api/runs", params={"flow_id": "job-hunt-demo"}).json()
    assert any(r["run_id"] == run_id for r in runs)
    assert client.get(f"/api/runs/{run_id}").json()["output"] == data["output"]
    assert client.get("/api/runs/ghost").status_code == 404


def test_agent_chat_triggers_flow(client):
    r = client.post("/api/agent/chat", json={"message": "帮我跑一下应聘demo"}).json()
    assert r["matched"] and r["flow_id"] == "job-hunt-demo"
    assert r["run"]["status"] == "success"
    assert "应聘 Agent" in r["reply"]

    miss = client.post("/api/agent/chat", json={"message": "今天天气如何"}).json()
    assert not miss["matched"] and "job-hunt-demo" in miss["reply"]


def test_intent_flow_run_via_api(client):
    """意图分流流程走 HTTP 运行：关键词降级下三条支路都能到 end。"""
    ok_msgs = {"看看今天的岗位": "意图[find_jobs]",
               "分析一下岗位趋势": "意图[trend_analysis]",
               "今天心情不错": "else 兜底"}
    for msg, expect in ok_msgs.items():
        r = client.post("/api/flows/job-intent-demo/run",
                        json={"inputs": {"message": msg}})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "success", data.get("error")
        assert expect in data["output"], (msg, data["output"][:200])


def test_agent_tools_manifest_and_call(client):
    tools = client.get("/api/agent/tools").json()
    names = {t["function"]["name"] for t in tools}
    assert "job-hunt-demo" in names
    r = client.post("/api/agent/tools/job-hunt-demo",
                    json={"arguments": "{}"}).json()
    assert r["status"] == "success"
    assert client.post("/api/agent/tools/ghost", json={}).status_code == 404


def test_llm_test_endpoint_disabled(client):
    body = {"system": "s", "prompt": "p"}
    r = client.post("/api/llm/test", json=body).json()
    assert r["ok"] is False and "LLM" in r["error"]
