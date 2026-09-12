"""Flow Studio：FastAPI 服务集成测试（TestClient，不起真实端口）。"""

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from flow_studio.server import create_app  # noqa: E402


@pytest.fixture
def client(config_dir, tmp_path):
    return TestClient(create_app(config_dir, tmp_path / "data"))


def test_health_and_static(client):
    assert client.get("/api/health").json() == {"ok": True}
    page = client.get("/")
    assert page.status_code == 200 and "Flow Studio" in page.text


def test_node_types_and_agents(client):
    types = client.get("/api/node-types").json()
    assert {"start", "end", "llm", "agent", "condition", "template", "http"} <= set(types)
    agents = client.get("/api/agents").json()
    job = next(a for a in agents if a["agent"] == "job_agent")
    assert any(a["action"] == "match_today" for a in job["actions"])


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

    assert client.delete("/api/flows/mini").json() == {"deleted": "mini"}
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
