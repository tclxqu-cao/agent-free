"""Homepage prompt translation, single-Agent execution, and artifact fallback."""

import html
import datetime as dt
import json
import re

import pytest
from fastapi.testclient import TestClient

from conftest import make_governed_client
from flow_studio.homepage_artifacts import validate_artifact
from flow_studio.homepage_flows import HOMEPAGE_FLOW_REVISION
from flow_studio.server import create_app
from job_agent.db import DB
from job_agent.models import Job


class FakeHomepageProvider:
    fail = False
    requests = []
    raw_reply = None

    def __init__(self, *_args, **_kwargs):
        pass

    @staticmethod
    def _artifact_kind(message, context):
        rule = str((context or {}).get("promptRule") or "")
        if isinstance((context or {}).get("jobResult"), dict):
            return "portfolio-jobs"
        explicit = {
            "help": "portfolio-help", "whoami": "portfolio-whoami",
            "works": "portfolio-works", "timeline": "portfolio-timeline",
            "contact": "portfolio-contact", "jobs": "portfolio-jobs",
            "unknown-command": "portfolio-help",
        }
        if rule in explicit:
            return explicit[rule]
        original = str((context or {}).get("originalMessage") or "")
        if rule == "project":
            project = original.lower().removeprefix("/project ").strip().replace(" ", "-")
            return f"portfolio-project-{project}"
        if re.search(r"项目|作品|agentroam|flow", original, re.I):
            return "portfolio-works"
        return "portfolio-chat"

    @staticmethod
    def _blocks(kind, message, context):
        if kind != "portfolio-jobs":
            return [{"type": "text", "text": str((context or {}).get("originalMessage") or message)}]
        job_result = (context or {}).get("jobResult") or {}
        snapshot = (context or {}).get("jobSnapshot") or {}
        source = job_result if isinstance(job_result, dict) and job_result else snapshot
        jobs = source.get("jobs") if isinstance(source, dict) else []
        rows = "".join(
            f"<li><strong>{html.escape(str(job.get('title') or ''))}</strong> "
            f"{html.escape(str(job.get('company') or ''))} "
            f"{'；'.join(html.escape(str(item)) for item in job.get('requirements') or [])}</li>"
            for job in jobs or [] if isinstance(job, dict)
        )
        warning = html.escape(str(source.get("warning") or "")) if isinstance(source, dict) else ""
        day = source.get("day") or source.get("snapshotDate") if isinstance(source, dict) else ""
        return [{"type": "html", "html": (
            f"<section><h2>岗位快照 {html.escape(str(day or ''))}</h2>"
            f"<p>{warning}</p><ul>{rows}</ul></section>"
        )}]

    def run(self, message, *, selection=None, context=None, on_event=None, **kwargs):
        request = {"message": message, "selection": selection or {},
                   "context": context or {}, "session_id": kwargs.get("session_id") or ""}
        type(self).requests.append(request)
        if type(self).fail:
            raise RuntimeError("offline")
        if on_event:
            on_event("run.started", {})
        rule = str((context or {}).get("promptRule") or "")
        is_jobs = isinstance((context or {}).get("jobResult"), dict)
        stages = (["wiki-query", "homepage-content-polish", "homepage-content-render"]
                  if rule not in {"default", "jobs", "unknown-command"} and not is_jobs
                  else ["homepage-content-polish", "homepage-content-render"]
                  if rule == "jobs" or is_jobs
                  else ["homepage-content-render"])
        for skill in stages:
            if on_event:
                on_event("tool.started", {"name": "skill_load", "arguments": {"name": skill}})
                on_event("tool.completed", {"content": skill, "isError": False})
        kind = self._artifact_kind(message, context)
        artifact = {
            "schemaVersion": 1,
            "skill": kind,
            "title": kind,
            "blocks": self._blocks(kind, message, context),
            "suggestions": ["/works"],
            "sources": ["projects/portfolio-public/portfolio-public.md"] if "wiki-query" in stages else [],
            "generatedAt": "2026-09-24T00:00:00Z",
        }
        reply = type(self).raw_reply or json.dumps(artifact, ensure_ascii=False)
        if on_event:
            on_event("run.completed", {"text": reply})
        return {"text": reply, "run_id": "homepage-run",
                "session_id": "homepage-session", "tool_calls": len(stages)}


@pytest.fixture
def homepage(config_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("FLOW_HOMEPAGE_TOKEN", "test-homepage-service-token")
    monkeypatch.setenv("PORTFOLIO_SKILL_TOKEN", "test-portfolio-skill-token")
    FakeHomepageProvider.fail = False
    FakeHomepageProvider.requests = []
    FakeHomepageProvider.raw_reply = None
    monkeypatch.setattr("flow_studio.agentrt.CustomerAgentProvider", FakeHomepageProvider)
    app = create_app(config_dir, tmp_path / "runtime")
    app.state.studio.registry.get("job_agent", "scrape").fn = lambda args: {
        "city": args.get("city") or "苏州", "total_jobs": 0,
        "errors": [], "text": "测试中跳过外部抓取",
    }
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer test-homepage-service-token"
    return client, app.state.studio.homepage


def _run_for(service, payload):
    return service._resources()[1].get(payload["run_id"])


def test_auth_only_grants_fixed_homepage_entry(homepage):
    client, service = homepage
    assert client.post("/api/homepage/command", json={"message": "/works"},
                       headers={"Authorization": ""}).status_code == 401
    assert client.get("/api/flows").status_code == 503
    response = client.post("/api/homepage/command", json={
        "message": "/works", "flow_id": "job-hunt-demo"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["flow_id"] == "homepage-main"
    assert payload["skill"] == "portfolio-works"
    assert [node["node_id"] for node in _run_for(service, payload)["node_runs"]] == [
        "start", "request_route", "prompt_decision", "homepage_agent", "end"]


def test_homepage_accepts_ca_selected_capability_ids_without_local_allowlist(homepage):
    client, service = homepage
    agents = service._resources()[2]
    agent = agents.get("homepage-agent")
    selection = agent["orchestration"]["selection"]
    selection["skill_ids"] = ["homepage-orchestrator", "ca-skill-from-catalog"]
    selection["tool_ids"] = ["ca-tool-from-catalog"]
    selection["mcp_server_ids"] = ["ca-mcp-from-catalog"]
    agents.save(agent)

    response = client.post("/api/homepage/command", json={"message": "你好"})

    assert response.status_code == 200
    assert FakeHomepageProvider.requests[-1]["selection"] == {
        "modelId": "aihub-deepseek",
        "skillIds": ["homepage-orchestrator", "ca-skill-from-catalog"],
        "activatedSkillIds": ["homepage-orchestrator"],
        "toolIds": ["ca-tool-from-catalog"],
        "mcpServerIds": ["ca-mcp-from-catalog"],
        "memoryEnabled": False,
        "toolPolicyId": "local-readonly",
    }


@pytest.mark.parametrize("message,rule,expected", [
    ("/contact", "contact", "portfolio-contact"),
    ("/project agentroam", "project", "portfolio-project-agentroam"),
    ("/not-a-command", "unknown-command", "portfolio-help"),
    ("介绍一下 Flow Studio", "default", "portfolio-works"),
    ("你好", "default", "portfolio-chat"),
])
def test_every_request_uses_prompt_decision_and_one_agent(homepage, message, rule, expected):
    client, service = homepage
    payload = client.post("/api/homepage/command", json={"message": message}).json()
    assert payload["skill"] == expected
    run = _run_for(service, payload)
    assert [node["node_id"] for node in run["node_runs"]] == [
        "start", "request_route", "prompt_decision", "homepage_agent", "end"]
    assert next(node for node in run["node_runs"]
                if node["node_id"] == "prompt_decision")["output"]["rule"] == rule
    request = FakeHomepageProvider.requests[-1]
    assert request["selection"]["activatedSkillIds"] == ["homepage-orchestrator"]
    assert request["selection"]["skillIds"] == [
        "homepage-orchestrator", "wiki-query", "llm-wiki",
        "homepage-content-polish", "homepage-content-render",
    ]
    assert request["selection"]["toolIds"] == [
        "skill_load", "bash", "read_file", "grep", "glob",
    ]
    assert request["selection"]["toolPolicyId"] == "local-readonly"
    assert request["context"]["promptRule"] == rule


def test_fact_request_exposes_skill_stage_activity(homepage):
    client, service = homepage
    payload = client.post("/api/homepage/command", json={"message": "/contact"}).json()
    agent_output = next(
        node["output"] for node in _run_for(service, payload)["node_runs"]
        if node["node_id"] == "homepage_agent")

    assert [step["args"]["name"] for step in agent_output["steps"]] == [
        "wiki-query", "homepage-content-polish", "homepage-content-render",
    ]
    assert payload["sources"] == ["projects/portfolio-public/portfolio-public.md"]


def test_page_session_reuses_customer_agent_until_browser_refresh(homepage):
    client, _service = homepage
    first_page = "1234567890abcdef1234567890abcdef"
    refreshed_page = "fedcba0987654321fedcba0987654321"
    for message in ("记住这个页面", "继续刚才的话题"):
        assert client.post("/api/homepage/command", json={
            "message": message, "sessionId": first_page}).status_code == 200
    assert client.post("/api/homepage/command", json={
        "message": "这是刷新后的页面", "sessionId": refreshed_page}).status_code == 200

    assert [request["session_id"] for request in FakeHomepageProvider.requests] == [
        f"homepage-{first_page}", f"homepage-{first_page}", f"homepage-{refreshed_page}",
    ]


def test_natural_language_never_uses_or_falls_back_to_snapshot(homepage):
    client, _service = homepage
    first = client.post("/api/homepage/command", json={"message": "介绍一下 Flow Studio"}).json()
    assert "cached" not in first and "fallback" not in first
    FakeHomepageProvider.fail = True
    failed = client.post("/api/homepage/command", json={"message": "介绍一下 Flow Studio"})
    assert failed.status_code == 503


def test_homepage_artifact_accepts_literal_control_characters(homepage):
    client, _service = homepage
    FakeHomepageProvider.raw_reply = (
        '{"schemaVersion":1,"skill":"portfolio-chat","title":"多行回答",'
        '"blocks":[{"type":"text","text":"第一行\n第二行"}],'
        '"suggestions":[],"sources":[]}')

    response = client.post(
        "/api/homepage/command", json={"message": "请给我一个多行回答"})

    assert response.status_code == 200
    assert response.json()["blocks"][0]["text"] == "第一行\n第二行"


def test_exact_command_cache_and_fallback_are_isolated(homepage):
    client, _service = homepage
    original = client.post("/api/homepage/command", json={"message": "/contact"}).json()
    FakeHomepageProvider.fail = True
    cached = client.post("/api/homepage/command", json={"message": "/contact"})
    assert cached.status_code == 200
    assert cached.json()["cached"] is True
    assert cached.json()["blocks"] == original["blocks"]
    fallback = client.post(
        "/api/homepage/command", json={"message": "/contact", "refresh": True})
    assert fallback.status_code == 200
    assert fallback.json()["fallback"] is True
    assert fallback.json()["skill"] == "portfolio-contact"
    assert client.post("/api/homepage/command", json={"message": "/works",
                       "refresh": True}).status_code == 503


def test_project_cache_key_accepts_generic_safe_slugs(homepage):
    _client, service = homepage
    assert service._cache_skill("/project any-new-project") == (
        "portfolio-project-any-new-project")
    assert service._cache_skill("/project 项目") is None
    assert service._cache_skill("/project ../secret") is None


def test_job_request_runs_real_flow_with_chengdu_and_excludes_other_cities(homepage):
    client, service = homepage
    config_dir = service.studio.config_dir
    db_path = config_dir.parent / "data" / "job_agent.db"
    db = DB(db_path)
    db.upsert_job(Job(site="boss", title="开发工程师", company="Example",
                      city="成都",
                      url="https://example.org/job", responsibilities=["开发系统"],
                      requirements_extra=["Python"], raw={"secret": "PRIVATE-RAW"}),
                  12, dt.date.today().isoformat())
    db.upsert_job(Job(site="boss", title="苏州岗位", company="Other",
                      city="苏州", url="https://example.org/suzhou"),
                  10, dt.date.today().isoformat())
    db.close()
    before = db_path.read_bytes()

    result = client.post("/api/homepage/command", json={
        "message": "job去搜索成都的"}).json()

    output = result["blocks"][0]["html"]
    context = FakeHomepageProvider.requests[-1]["context"]
    assert context["jobCity"] == "成都"
    assert context["jobResult"]["city"] == "成都"
    assert "开发工程师" in output and "Python" in output
    assert "苏州岗位" not in json.dumps(context["jobResult"], ensure_ascii=False)
    assert "PRIVATE-RAW" not in json.dumps(context["jobResult"], ensure_ascii=False)
    child = service.legacy_runs.get(context["jobRunId"])
    assert [node["node_id"] for node in child["node_runs"]][:3] == [
        "start", "resolve_city", "cond_scrape"]
    assert db_path.read_bytes() == before


def test_job_request_uses_verified_result_when_agent_returns_invalid_json(homepage):
    client, service = homepage
    db = DB(service.studio.config_dir.parent / "data" / "job_agent.db")
    db.upsert_job(Job(
        site="job51", title="Java开发工程师", company="成都测试公司",
        city="成都", district="武侯区", salary_text="25-35K",
        experience_text="5-10年", education="本科",
        url="https://jobs.51job.com/chengdu/162453184.html",
        responsibilities=["负责核心系统研发"],
        requirements_extra=["熟悉 Java 和 Spring Boot"],
    ), 8, dt.date.today().isoformat())
    db.close()
    FakeHomepageProvider.raw_reply = "这不是合法的制品 JSON"

    response = client.post("/api/homepage/command", json={
        "message": "job去搜索成都的"})

    assert response.status_code == 200, response.text
    artifact = response.json()
    assert artifact["skill"] == "portfolio-jobs"
    rendered = "\n".join(block.get("text", "") for block in artifact["blocks"])
    assert "成都岗位搜索完成" in rendered
    assert "Java开发工程师" in rendered
    assert "熟悉 Java 和 Spring Boot" in rendered
    assert service.legacy_runs.get(artifact["run_id"])["status"] == "success"


def test_unsafe_graph_edits_fail_closed_even_when_cache_exists(homepage):
    client, service = homepage
    assert client.post("/api/homepage/command", json={"message": "/works"}).status_code == 200
    graph = service.legacy_flows.get("homepage-main")
    graph.node("homepage_agent").params["ai_agent_id"] = "kb-assistant"
    service.legacy_flows.save(graph)
    assert client.post("/api/homepage/command", json={"message": "/works"}).status_code == 503


def test_governed_instance_migrates_and_uses_published_job_route(homepage):
    _, service = homepage
    app = create_app(service.studio.config_dir, service.studio.data_root)
    client = make_governed_client(app)
    client.headers["Authorization"] = "Bearer test-homepage-service-token"
    flow = client.get("/api/flows/homepage-main").json()
    assert HOMEPAGE_FLOW_REVISION in flow["description"]
    assert [node["id"] for node in flow["nodes"]] == [
        "start", "request_route", "job_workflow", "job_prompt",
        "prompt_decision", "homepage_agent", "end"]
    response = client.post("/api/homepage/command", json={"message": "/works"})
    assert response.status_code == 200, response.text
    flow["nodes"][-1]["params"]["output"] = (
        '{"schemaVersion":1,"skill":"portfolio-works","title":"DRAFT-ONLY",'
        '"blocks":[{"type":"text","text":"draft"}]}')
    assert client.put("/api/flows/homepage-main", json=flow).status_code == 200
    assert "DRAFT-ONLY" not in client.post(
        "/api/homepage/command", json={"message": "/works"}).text


def test_startup_removes_deprecated_homepage_agents(homepage):
    client, service = homepage
    governed_client = make_governed_client(client.app)
    governance = service.studio.governance
    actor = governance.workspace_creator("default")
    for store in (service.legacy_agents, service._resources()[2]):
        for agent_id in ("homepage-chat-agent", "portfolio-content-agent"):
            store.save({"id": agent_id, "name": "旧主页智能体"})
    for agent_id in ("homepage-chat-agent", "portfolio-content-agent"):
        version = governance.save_version(
            "default", "agent", agent_id,
            {"id": agent_id, "name": "旧主页智能体"}, "upsert", actor)
        version = governance.set_version_status(
            version["version_id"], ("draft",), "pending", actor, "seed legacy Agent")
        version = governance.set_version_status(
            version["version_id"], ("pending",), "approved", actor, "seed legacy Agent")
        governance.set_release(version["version_id"], actor, "seed legacy Agent")

    create_app(service.studio.config_dir, service.studio.data_root)

    assert service.legacy_agents.get("homepage-chat-agent") is None
    assert service.legacy_agents.get("portfolio-content-agent") is None
    governed_agents = service.studio.workspaces.get("default").ai_agents
    assert governed_agents.get("homepage-chat-agent") is None
    assert governed_agents.get("portfolio-content-agent") is None
    visible_ids = {
        item["id"]
        for item in governed_client.get("/api/ai-agents").json()
    }
    assert "homepage-chat-agent" not in visible_ids
    assert "portfolio-content-agent" not in visible_ids
    for agent_id in ("homepage-chat-agent", "portfolio-content-agent"):
        assert governance.get_release(
            "default", "agent", agent_id)["action"] == "delete"


def test_invalid_inputs_and_unconfigured_token_fail_closed(homepage):
    client, service = homepage
    for message in ("", " " * 3, "x" * 2001):
        assert client.post("/api/homepage/command", json={"message": message}).status_code == 422
    assert client.post("/api/homepage/command", json={
        "message": "hello", "sessionId": "not-a-session"}).status_code == 422
    service.token = ""
    assert client.post("/api/homepage/command", json={"message": "/works"}).status_code == 401


def test_artifact_normalizes_model_media_url_alias():
    artifact = validate_artifact({
        "schemaVersion": 1,
        "skill": "portfolio-project-flow-studio",
        "title": "Flow Studio",
        "blocks": [
            {"type": "video", "url": "/assets/work-flow.mp4",
             "poster": "/assets/work-flow.jpg"},
            {"type": "image", "url": "assets/work-flow.jpg", "alt": "Flow Studio"},
        ],
    })

    assert artifact["blocks"][0]["src"] == "/assets/work-flow.mp4"
    assert artifact["blocks"][1]["src"] == "assets/work-flow.jpg"
    assert all("url" not in block for block in artifact["blocks"])
