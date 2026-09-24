"""Flow Studio：内置应聘流程端到端（demo 数据、无 LLM 无凭据跑通）。"""

from pathlib import Path

from flow_studio.builtin_flows import builtin_flows
from flow_studio.engine import FlowRunner
from flow_studio.graph import graph_from_dict
from flow_studio.llm import llm_chat


def _demo_runner(config_dir: Path) -> tuple[FlowRunner, object]:
    from flow_studio.adapters import register_job_agent
    from flow_studio.registry import AgentRegistry

    reg = AgentRegistry()
    assert register_job_agent(reg, config_dir) is True
    return FlowRunner(reg, llm_cfg={}), reg


def test_builtin_flows_shape():
    flows = builtin_flows()
    by_id = {f["id"]: f for f in flows}
    assert {"job-hunt-demo", "job-hunt-daily", "job-hunt-real", "job-intent-demo",
            "agent-brain-test", "video-demo", "project-intro-video",
            "homepage-main"} == set(by_id)
    demo = graph_from_dict(by_id["job-hunt-demo"])
    types = {n.type for n in demo.nodes}
    assert {"start", "end", "agent", "condition", "llm", "template"} <= types
    branches = [e["branch"] for e in demo.edges if "branch" in e]
    assert any("else" in b for b in branches)
    assert any("passed_count" in b for b in branches)

    # 意图分流流程：意图节点 + 按意图名的出边 + else 兜底
    intent_flow = graph_from_dict(by_id["job-intent-demo"])
    router = next(n for n in intent_flow.nodes if n.type == "intent")
    names = {i["name"] for i in router.params["intents"]}
    assert names == {"find_jobs", "trend_analysis"}
    intent_branches = [e["branch"] for e in intent_flow.edges
                       if e["from"] == router.id]
    assert set(intent_branches) == {"find_jobs", "trend_analysis", "else"}

    real = graph_from_dict(by_id["job-hunt-real"])
    real_nodes = {node.id: node for node in real.nodes}
    assert real_nodes["resolve_city"].params["action"] == "resolve_city"
    assert real_nodes["scrape"].params["args"]["city"] == "{{resolve_city.city}}"
    assert real_nodes["match"].params["args"]["city"] == "{{resolve_city.city}}"
    assert "{{resolve_city.city}}" in real_nodes["brain"].params["prompt"]

    # Homepage routes job requests through the real job flow before one renderer Agent.
    homepage = graph_from_dict(by_id["homepage-main"])
    nodes = {node.id: node for node in homepage.nodes}
    assert homepage.name == "个人主页 · 主流程"
    assert {node_id: nodes[node_id].label for node_id in nodes} == {
        "start": "主页访客输入",
        "request_route": "岗位意图路由",
        "job_workflow": "真实岗位流程",
        "job_prompt": "岗位展示任务",
        "prompt_decision": "任务提示词判断",
        "homepage_agent": "小熊",
        "end": "主页展示结果",
    }
    decision = nodes["prompt_decision"]
    assert decision.type == "condition"
    assert decision.params["source"] == "input.message"
    assert [rule["name"] for rule in decision.params["outputs"]] == [
        "help", "whoami", "works", "project", "timeline", "contact", "jobs",
        "unknown-command",
    ]
    assert "{{input.message}}" in decision.params["default_output"]
    assert any(item["key"] == "session_id"
               for item in nodes["start"].params["inputs"])
    assert nodes["job_workflow"].type == "subflow"
    assert nodes["job_workflow"].params["flow_id"] == "job-hunt-real"
    assert nodes["homepage_agent"].params == {
        "ai_agent_id": "homepage-agent",
        "skill_id": "homepage-orchestrator",
        "message": "{{prompt_decision.text}}{{job_prompt.text}}",
        "session_id": "{{input.session_id}}",
        "context": {
            "surface": "public-homepage",
            "originalMessage": "{{input.message}}",
            "promptRule": "{{prompt_decision.rule}}{{job_prompt.text}}",
            "jobSnapshot": "{{input.job_snapshot}}",
            "jobCity": "{{job_workflow.nodes.resolve_city.city}}",
            "jobResult": "{{job_workflow.nodes.match}}",
            "jobRunId": "{{job_workflow.run_id}}",
        },
        "required": True,
    }
    assert homepage.edges == [
        {"from": "start", "to": "request_route"},
        {"from": "request_route", "to": "job_workflow", "branch": (
            "input.message == '/jobs' or input.message == '/job' or "
            "'岗位' in input.message or '招聘' in input.message or "
            "'找工作' in input.message or '应聘' in input.message or "
            "'job' in input.message or 'Job' in input.message or 'JOB' in input.message"
        )},
        {"from": "request_route", "to": "prompt_decision", "branch": "else"},
        {"from": "job_workflow", "to": "job_prompt"},
        {"from": "job_prompt", "to": "homepage_agent"},
        {"from": "prompt_decision", "to": "homepage_agent"},
        {"from": "homepage_agent", "to": "end"},
    ]

    project_video = graph_from_dict(by_id["project-intro-video"])
    project_nodes = {node.id: node for node in project_video.nodes}
    assert project_nodes["storyboard"].params["shot_count"] == 8
    assert project_nodes["storyboard"].params["target_duration"] == 72
    assert project_nodes["storyboard"].params["aspect_ratio"] == "16:9"
    assert project_nodes["voiceover"].params["voice"] == "Serena"
    assert project_nodes["compose"].type == "video_compose"
    assert project_nodes["compose"].params["burn_subtitles"] is True
    assert [node.type for node in project_video.nodes][-3:] == [
        "voiceover", "video_compose", "end"]


def test_demo_flow_runs_end_to_end(config_dir):
    runner, _ = _demo_runner(config_dir)
    demo = graph_from_dict(builtin_flows()[0])
    assert demo.id == "job-hunt-demo"
    run = runner.run(demo, {})
    assert run.status == "success", run.error
    statuses = {n.node_id: n.status for n in run.node_runs}
    assert statuses["seed"] == "success" and statuses["match"] == "success"
    assert statuses["branch"] == "success"
    # 无 LLM：advisor 降级跳过，但流程继续走到汇总与 end
    assert statuses["advisor"] == "skipped"
    assert statuses["summary"] == "success"
    assert "应聘 Agent · 模拟数据日检" in run.output
    assert "2026-" in run.output          # vars.today 渲染成功
    assert "命中" in run.output and "个" in run.output


def test_registry_catalog(config_dir):
    _, reg = _demo_runner(config_dir)
    catalog = reg.agents()
    job = next(a for a in catalog if a["agent"] == "job_agent")
    actions = {a["action"] for a in job["actions"]}
    assert {"demo_seed", "resolve_city", "match_today", "daily", "scrape",
            "analyze", "report",
            "login_status"} <= actions


def test_match_today_action_direct(config_dir):
    from flow_studio.adapters import register_job_agent
    from flow_studio.registry import AgentRegistry

    reg = AgentRegistry()
    register_job_agent(reg, config_dir)
    entry = reg.get("job_agent", "demo_seed")
    out = entry.fn({"days": 3})
    assert out["seeded_snapshots"] > 0 and "模拟数据" in out["text"]
    match = reg.get("job_agent", "match_today").fn({})
    assert match["total"] > 0 and match["passed_count"] >= 0
    assert isinstance(match["jobs"], list)


def test_job_actions_apply_request_city_without_mutating_config(config_dir, monkeypatch):
    import datetime as dt

    from flow_studio.adapters import register_job_agent
    from flow_studio.registry import AgentRegistry
    from job_agent.distance import DISTRICTS
    from job_agent.models import Job, load_config

    seen = {"calls": 0}

    def fake_scrape(config, _config_dir, db, sites=None):
        seen.update(config=config, sites=sites, calls=seen["calls"] + 1)
        db.upsert_job(Job(site="boss", title="Java", company="Example",
                          city=config["home"]["city"],
                          url="https://example.org/chengdu"),
                      3, dt.date.today().isoformat())
        return {"total_jobs": 3, "errors": []}

    monkeypatch.setattr("job_agent.sites.scrape_all", fake_scrape)
    reg = AgentRegistry()
    register_job_agent(reg, config_dir)

    resolved = reg.get("job_agent", "resolve_city").fn({
        "message": "job去搜索成都的"})
    scrape = reg.get("job_agent", "scrape").fn
    scraped = scrape({"city": resolved["city"]})
    cached = scrape({"city": resolved["city"]})

    assert resolved["city"] == "成都"
    assert scraped["city"] == "成都"
    assert scraped["cached"] is False
    assert cached["cached"] is True and seen["calls"] == 1
    assert seen["config"]["home"] == {
        "city": "成都", "district": "", "lat": DISTRICTS["成都"]["__city__"][0],
        "lng": DISTRICTS["成都"]["__city__"][1],
    }
    assert seen["config"]["rules"]["cities"] == ["成都"]
    assert load_config(config_dir)["home"]["city"] == "苏州"


def test_intent_flow_routes_three_branches(config_dir):
    """意图分流流程端到端：三条支路各跑一遍（无 LLM → 关键词降级）。"""
    runner, _ = _demo_runner(config_dir)
    flow = graph_from_dict(next(item for item in builtin_flows()
                               if item["id"] == "job-intent-demo"))
    assert flow.id == "job-intent-demo"

    run_jobs = runner.run(flow, {"message": "帮我看看今天的岗位"})
    assert run_jobs.status == "success", run_jobs.error
    assert "意图[find_jobs]" in run_jobs.output
    assert "命中" in run_jobs.output

    run_trend = runner.run(flow, {"message": "分析一下岗位趋势"})
    assert run_trend.status == "success", run_trend.error
    assert "意图[trend_analysis]" in run_trend.output

    run_chat = runner.run(flow, {"message": "今天心情不错"})
    assert run_chat.status == "success", run_chat.error
    assert "else 兜底" in run_chat.output
    router = run_chat.node_run("router")
    assert router.output["matched"] is False


def test_llm_chat_disabled_returns_none():
    assert llm_chat({"enabled": False}, "s", "u") is None
    assert llm_chat({}, "s", "u") is None
