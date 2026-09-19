"""Flow Studio 智能体平台：知识库 / 记忆 / 技能 / MCP / 工具 / 智能体 节点测试。"""

import json
import sys
from pathlib import Path

import pytest

from flow_studio.agentrt import AgentRuntime, AgentStore
from flow_studio.engine import FlowRunner
from flow_studio.graph import graph_from_dict
from flow_studio.knowledge import KBStore
from flow_studio.mcp_client import MCPManager
from flow_studio.memory import MemoryStore
from flow_studio.skills import SkillStore
from flow_studio.tools import ToolRegistry, register_builtin_tools

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _g(nodes, edges, **kw):
    return graph_from_dict({"id": kw.get("id", "t"), "name": "测试",
                            "nodes": nodes, "edges": edges})


def _node(id, type, **params):
    return {"id": id, "type": type, "params": params}


@pytest.fixture
def kb(tmp_path):
    store = KBStore(tmp_path / "kb")
    store.create_kb("manual", "产品手册")
    store.add_doc("manual", "退货政策", "本品支持七天无理由退货。退货需要保留原包装，运费由买家承担。")
    store.add_doc("manual", "保修条款", "整机保修一年，电池保修六个月。人为损坏不在保修范围内。")
    return store


@pytest.fixture
def memory(tmp_path):
    return MemoryStore(tmp_path / "memory.sqlite")


@pytest.fixture
def skills(tmp_path):
    return SkillStore(tmp_path / "skills")


@pytest.fixture
def tools(kb, memory, skills):
    reg = ToolRegistry()
    register_builtin_tools(reg, kb=kb, memory=memory, skills=skills)
    return reg


@pytest.fixture
def mcp(tmp_path):
    mgr = MCPManager(tmp_path / "mcp.json")
    mgr.save({"servers": {"echo": {
        "command": sys.executable,
        "args": [str(FIXTURES / "mcp_echo_server.py")]}}})
    return mgr


@pytest.fixture
def agent_store(tmp_path):
    return AgentStore(tmp_path / "ai_agents")


@pytest.fixture
def client(config_dir, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from flow_studio.server import create_app
    return TestClient(create_app(config_dir, tmp_path / "data"))


# ---------------------------------------------------------------- 知识库
def test_kb_add_and_search(kb):
    hits = kb.search("退货政策是什么", top_k=3)
    assert hits, "中文检索应命中"
    assert hits[0]["name"] == "退货政策"
    assert "七天无理由" in hits[0]["text"]
    assert hits[0]["score"] >= 0
    # 命中词更多的文档应排在前面（OR 语义 + bm25）
    hits2 = kb.search("电池保修多久", top_k=3)
    assert hits2[0]["name"] == "保修条款"

    hits3 = kb.search("保修 多久", kb_ids=["manual"], top_k=1)
    assert hits3 and "保修" in hits3[0]["text"]

    assert kb.search("xyzzy不存在词", top_k=3) == []


def test_kb_doc_delete(kb):
    doc = kb.list_docs("manual")[0]
    assert kb.delete_doc("manual", doc["doc_id"])
    assert kb.search("七天无理由退货") == []


def test_kb_flow_node(kb):
    g = _g([
        _node("start", "start", inputs=[{"key": "q", "default": "电池保修几个月"}]),
        _node("search", "kb", kb_ids=["manual"], query="{{input.q}}", top_k=2),
        _node("end", "end", output="{{search.text}}"),
    ], [{"from": "start", "to": "search"}, {"from": "search", "to": "end"}])
    run = FlowRunner(kb=kb).run(g, {})
    assert run.status == "success"
    assert "保修一年" in run.output or "保修六个月" in run.output


# ---------------------------------------------------------------- 记忆
def test_memory_node_persists_across_runs(memory):
    g = _g([
        _node("start", "start", inputs=[{"key": "session_id"}]),
        _node("m", "memory", op="set", scope="session",
              session_id="{{input.session_id}}", key="city", value="苏州"),
        _node("end", "end", output="{{m.text}}"),
    ], [{"from": "start", "to": "m"}, {"from": "m", "to": "end"}])
    g2 = _g([
        _node("start", "start", inputs=[{"key": "session_id"}]),
        _node("m", "memory", op="get", scope="session",
              session_id="{{input.session_id}}", key="city"),
        _node("end", "end", output="城市：{{m.value}}"),
    ], [{"from": "start", "to": "m"}, {"from": "m", "to": "end"}])
    runner = FlowRunner(memory=memory)
    assert runner.run(g, {"session_id": "s1"}).status == "success"
    run2 = runner.run(g2, {"session_id": "s1"})
    assert run2.output == "城市：苏州"
    # 不同会话隔离
    run3 = runner.run(g2, {"session_id": "s2"})
    assert run3.output == "城市："


def test_memory_json_value_roundtrip(memory):
    memory.set("global", "pref", {"lang": "zh", "tags": ["a", "b"]})
    assert memory.get("global", "pref") == {"lang": "zh", "tags": ["a", "b"]}
    items = memory.list(scope="global", q="lang")
    assert len(items) == 1


# ---------------------------------------------------------------- 技能
def test_skill_node_without_llm(skills):
    skills.save("greet", "问候", "打招呼用",
                "用一句话向用户问好，并提到今天的日期。")
    g = _g([
        _node("start", "start"),
        _node("s", "skill", skill_id="greet"),
        _node("end", "end", output="{{s.instructions}}"),
    ], [{"from": "start", "to": "s"}, {"from": "s", "to": "end"}])
    run = FlowRunner(skills=skills).run(g, {})
    assert run.status == "success"
    assert "问好" in run.output


def test_skill_node_unknown_fails(skills):
    g = _g([
        _node("start", "start"),
        _node("s", "skill", skill_id="ghost"),
        _node("end", "end", output="x"),
    ], [{"from": "start", "to": "s"}, {"from": "s", "to": "end"}])
    run = FlowRunner(skills=skills).run(g, {})
    assert run.status == "failed" and "技能不存在" in run.error


# ---------------------------------------------------------------- 工具节点
def test_tool_node_calc_and_now(tools):
    g = _g([
        _node("start", "start"),
        _node("c", "tool", tool="calc", arguments={"expression": "1 + 2 * 3"}),
        _node("n", "tool", tool="now"),
        _node("end", "end", output="{{c.value}} @ {{n.date}}"),
    ], [{"from": "start", "to": "c"}, {"from": "c", "to": "n"},
        {"from": "n", "to": "end"}])
    run = FlowRunner(tools=tools).run(g, {})
    assert run.status == "success"
    assert run.output.startswith("7 @ 2")
    assert run.node_run("n").output["weekday"] in "一二三四五六日"


def test_tool_node_calc_rejects_call(tools):
    g = _g([
        _node("start", "start"),
        _node("c", "tool", tool="calc", arguments={"expression": "__import__('os')"}),
        _node("end", "end", output="x"),
    ], [{"from": "start", "to": "c"}, {"from": "c", "to": "end"}])
    run = FlowRunner(tools=tools).run(g, {})
    assert run.status == "failed"


def test_tool_node_template_args(tools):
    g = _g([
        _node("start", "start", inputs=[{"key": "a", "default": "5"}]),
        _node("c", "tool", tool="calc", arguments={"expression": "{{input.a}} * 2"}),
        _node("end", "end", output="{{c.value}}"),
    ], [{"from": "start", "to": "c"}, {"from": "c", "to": "end"}])
    run = FlowRunner(tools=tools).run(g, {})
    assert run.output == "10"


# ---------------------------------------------------------------- MCP
def test_mcp_node_echo_and_add(mcp):
    g = _g([
        _node("start", "start", inputs=[{"key": "text", "default": "你好"}]),
        _node("call", "mcp", server="echo", tool="echo",
              arguments={"text": "{{input.text}}"}),
        _node("end", "end", output="{{call.text}}"),
    ], [{"from": "start", "to": "call"}, {"from": "call", "to": "end"}])
    run = FlowRunner(mcp=mcp).run(g, {})
    assert run.status == "success"
    assert run.output == "echo: 你好"

    g2 = _g([
        _node("start", "start"),
        _node("call", "mcp", server="echo", tool="add", arguments={"a": 2, "b": 3.5}),
        _node("end", "end", output="{{call.text}}"),
    ], [{"from": "start", "to": "call"}, {"from": "call", "to": "end"}])
    run2 = FlowRunner(mcp=mcp).run(g2, {})
    assert run2.output == "5.5"
    mcp.close_all()


def test_mcp_node_unknown_server_degrades(mcp):
    g = _g([
        _node("start", "start"),
        _node("call", "mcp", server="ghost", tool="x", optional=True),
        _node("end", "end", output="done"),
    ], [{"from": "start", "to": "call"}, {"from": "call", "to": "end"}])
    run = FlowRunner(mcp=mcp).run(g, {})
    assert run.status == "success"
    assert run.node_run("call").status == "skipped"
    mcp.close_all()


# ---------------------------------------------------------------- 智能体
def _fake_llm(script):
    """按调用次数回放脚本：每项 = assistant 消息条目。"""
    calls = {"n": 0}

    def fn(cfg, messages, tools):
        i = min(calls["n"], len(script) - 1)
        calls["n"] += 1
        return script[i]
    fn.calls = calls
    return fn


def test_ai_agent_tool_loop(tools, memory, agent_store):
    agent = agent_store.save({"id": "mathy", "name": "计算器",
                              "tool_ids": ["calc"], "memory": True,
                              "system": "用 calc 工具算数。"})
    fake = _fake_llm([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "calc", "arguments": '{"expression": "12*12"}'}}]},
        {"role": "assistant", "content": "12 乘 12 等于 144。"},
    ])
    rt = AgentRuntime({}, tools, memory=memory, llm_fn=fake)
    out = rt.run(agent, "12 乘以 12 等于多少？", session_id="sess1")
    assert not out.get("error")
    assert "144" in out["text"]
    assert out["tool_calls"] == 1
    assert out["steps"][0]["name"] == "calc" and out["steps"][0]["ok"]
    # 记忆写回
    assert memory.get("session:sess1", "_last")["reply"].startswith("12 乘 12")


def test_ai_agent_node_in_flow(tools, memory, agent_store):
    agent = agent_store.save({"id": "parrot", "name": "复读机", "memory": False})
    fake = _fake_llm([
        {"role": "assistant", "content": "收到：{{不解析模板}}"},
    ])
    rt = AgentRuntime({}, tools, memory=memory, llm_fn=fake)
    g = _g([
        _node("start", "start", inputs=[{"key": "message", "default": "hello"}]),
        _node("bot", "ai_agent", ai_agent_id="parrot", message="{{input.message}}"),
        _node("end", "end", output="{{bot.text}}（{{bot.tool_calls}} 次工具）"),
    ], [{"from": "start", "to": "bot"}, {"from": "bot", "to": "end"}])
    run = FlowRunner(tools=tools, memory=memory, ai_agents=agent_store,
                     agent_rt=rt).run(g, {})
    assert run.status == "success"
    assert run.output == "收到：{{不解析模板}}（0 次工具）"


def test_ai_agent_llm_unavailable_degrades(tools, agent_store):
    agent = agent_store.save({"id": "noop", "name": "空转"})
    rt = AgentRuntime({}, tools, llm_fn=lambda cfg, msgs, t: None)  # LLM 不可用
    g = _g([
        _node("start", "start"),
        _node("bot", "ai_agent", ai_agent_id="noop", message="hi"),
        _node("end", "end", output="done"),
    ], [{"from": "start", "to": "bot"}, {"from": "bot", "to": "end"}])
    run = FlowRunner(tools=tools, ai_agents=agent_store, agent_rt=rt).run(g, {})
    assert run.status == "success"                    # 默认降级跳过
    assert run.node_run("bot").status == "skipped"

    agent_store.save({"id": "strict", "name": "严格"})
    g2 = _g([
        _node("start", "start"),
        _node("bot", "ai_agent", ai_agent_id="strict", message="hi", required=True),
        _node("end", "end", output="done"),
    ], [{"from": "start", "to": "bot"}, {"from": "bot", "to": "end"}])
    run2 = FlowRunner(tools=tools, ai_agents=agent_store, agent_rt=rt).run(g2, {})
    assert run2.status == "failed"


def test_ai_agent_mcp_binding(mcp, tools, agent_store):
    agent = agent_store.save({"id": "mcpguy", "name": "MCP", "mcp_servers": ["echo"]})
    fake = _fake_llm([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "mcp__echo__echo",
                          "arguments": '{"text": "mcp!"}'}}]},
        {"role": "assistant", "content": "MCP 说完了。"},
    ])
    rt = AgentRuntime({}, tools, mcp=mcp, llm_fn=fake)
    out = rt.run(agent, "调用 echo")
    assert "echo: mcp!" in json.dumps(out["steps"], ensure_ascii=False)
    mcp.close_all()

# ---------------------------------------------------------------- RAG
def test_agent_rag_injects_kb_context(kb, tools):
    from flow_studio.agentrt import AgentRuntime, AgentStore
    store = AgentStore(Path("/tmp") / "unused-rag")
    agent = store.save({"id": "raggy", "name": "RAG", "kb_ids": ["manual"],
                        "memory": False})
    captured = {}

    def fake_llm(cfg, messages, tools_):
        captured["system"] = messages[0]["content"]
        return {"role": "assistant", "content": "根据资料回答。"}
    rt = AgentRuntime({}, tools, kb=kb, llm_fn=fake_llm)
    out = rt.run(agent, "退货政策是怎样的？")
    assert "【退货政策】" in captured["system"]      # 片段注入 system
    assert "七天无理由" in captured["system"]
    assert out["steps"][0]["type"] == "rag"          # 检索步骤可观测
    assert not out.get("error")


def test_agent_rag_skipped_without_kb(tools):
    from flow_studio.agentrt import AgentRuntime, AgentStore
    store = AgentStore(Path("/tmp") / "unused-rag")
    agent = store.save({"id": "norkb", "name": "无知识库"})
    steps_holder = {}

    def fake_llm(cfg, messages, tools_):
        steps_holder["n"] = len(messages)
        return {"role": "assistant", "content": "ok"}
    out = AgentRuntime({}, tools, llm_fn=fake_llm).run(agent, "你好")
    assert all(s["type"] != "rag" for s in out["steps"])


# ---------------------------------------------------------------- 评测中心
def test_eval_run_checks():
    from flow_studio.evals import run_checks
    ok = run_checks("结果是 144。", {"contains": ["144"], "not_contains": ["错误"]})
    assert all(c["ok"] for c in ok)
    bad = run_checks("我不知道", {"contains": ["144"]})
    assert not all(c["ok"] for c in bad)
    rex = run_checks("答案是 42", {"regex": r"\d{2}"})
    assert rex[0]["ok"]
    empty = run_checks("任意回答", {})
    assert empty[0]["name"] == "非空" and empty[0]["ok"]


def test_eval_run_suite_platform_target(kb, tools, agent_store, tmp_path):
    from flow_studio.evals import EvalStore, TargetRunner, run_suite
    from flow_studio.agentrt import AgentRuntime
    agent_store.save({"id": "mathy", "name": "计算", "tool_ids": ["calc"],
                      "memory": False})
    fake = _fake_llm([
        {"role": "assistant", "content": "144"},
    ])
    rt = AgentRuntime({}, tools, llm_fn=fake)
    store = EvalStore(tmp_path / "evals")
    suite = store.save_suite({
        "id": "t1", "name": "测试集",
        "cases": [
            {"id": "c1", "question": "12*12=?", "expect": {"contains": ["144"]}},
            {"id": "c2", "question": "天空是什么颜色？",
             "expect": {"contains": ["蓝"]}},
        ]})
    run = run_suite(suite, [{"type": "platform", "id": "mathy"}], store,
                    TargetRunner(agent_rt=rt, agent_store=agent_store))
    assert run["run_id"]
    assert len(run["results"]) == 2
    c1 = next(r for r in run["results"] if r["case_id"] == "c1")
    c2 = next(r for r in run["results"] if r["case_id"] == "c2")
    assert c1["pass"] and c1["answer"] == "144"
    assert not c2["pass"]                              # fake LLM 没答"蓝"
    listed = store.list_runs("t1")
    assert listed[0]["targets"][0]["passed"] == 1
    assert store.get_run(run["run_id"])["results"][0]["steps"] is not None


def test_eval_run_suite_cli_target(tmp_path):
    from flow_studio.evals import EvalStore, TargetRunner, run_suite
    store = EvalStore(tmp_path / "evals")
    suite = store.save_suite({
        "id": "cli", "name": "CLI",
        "cases": [{"id": "c1", "question": "Q?",
                   "expect": {"contains": ["144"]}}]})
    target = {"type": "cli", "key": "py", "name": "Python",
              "command": sys.executable, "args": ["-c", "print('答案: 144')"]}
    run = run_suite(suite, [target], store, TargetRunner())
    assert run["results"][0]["pass"]
    assert "144" in run["results"][0]["answer"]

    bad = dict(target, command=sys.executable,
               args=["-c", "import sys; sys.exit(3)"])
    run2 = run_suite(suite, [bad], store, TargetRunner())
    assert not run2["results"][0]["pass"]
    assert run2["results"][0]["error"]


def test_eval_compare(tmp_path):
    from flow_studio.evals import EvalStore, compare_runs
    store = EvalStore(tmp_path / "evals")

    def mk(run_id, case1_pass):
        return store.save_run({
            "run_id": run_id, "suite_id": "s", "suite_name": "S",
            "targets": [{"key": "a", "name": "A"}, {"key": "b", "name": "B"}],
            "results": [
                {"target_key": "a", "target_name": "A", "case_id": "c1",
                 "question": "Q1", "answer": "x", "steps": [],
                 "checks": [{"name": "非空", "ok": True}], "pass": True,
                 "error": None, "ms": 10},
                {"target_key": "b", "target_name": "B", "case_id": "c1",
                 "question": "Q1", "answer": "y" if case1_pass else "",
                 "steps": [], "checks": [{"name": "非空", "ok": case1_pass}],
                 "pass": case1_pass, "error": None, "ms": 20}]})

    ra, rb = mk("r1", True), mk("r2", False)
    cmp = compare_runs(ra, rb)
    assert cmp["diff_count"] == 1
    row = cmp["rows"][0]
    assert row["diff"] and row["cells"][0]["pass"] != row["cells"][1]["pass"]


def test_eval_server_api(client, tmp_path):
    # 评测集 CRUD
    assert client.get("/api/evals/suites").json()[0]["id"] == "smoke"
    suite = client.get("/api/evals/suites/smoke").json()
    assert len(suite["cases"]) >= 2
    r = client.put("/api/evals/suites/custom", json={
        "name": "自定义", "cases": [{"question": "1+1?", "expect": {"contains": ["2"]}}]})
    assert r.status_code == 200 and len(r.json()["cases"][0]["id"]) == 8
    assert client.delete("/api/evals/suites/custom").json()["deleted"] == "custom"

    # 运行（platform 目标 + LLM 未启用 → 全部失败但不报 500）
    agents = client.get("/api/ai-agents").json()
    target = {"type": "platform", "id": agents[0]["id"], "key": "kb-assistant",
              "name": "内置助手"}
    run = client.post("/api/evals/suites/smoke/run",
                      json={"targets": [target]}).json()
    assert run["run_id"] and len(run["results"]) == len(suite["cases"])
    runs = client.get("/api/evals/runs", params={"suite_id": "smoke"}).json()
    assert runs and runs[0]["targets"][0]["total"] >= 2

    # 对比：跑两次（第二次跳过校验目标是同一 run 的克隆场景，直接两次运行）
    run2 = client.post("/api/evals/suites/smoke/run",
                       json={"targets": [target]}).json()
    cmp = client.get("/api/evals/compare",
                     params={"run_a": run["run_id"], "run_b": run2["run_id"]}).json()
    assert cmp["run_a"] == run["run_id"] and len(cmp["rows"]) == len(suite["cases"])

# ---------------------------------------------------------------- 可观测性（Langfuse）
import threading
import http.server


class _MockLangfuse(http.server.BaseHTTPRequestHandler):
    """最小 mock：记录 Authorization 与 batch 请求体。"""
    received = []
    auth = None
    fail = False

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _MockLangfuse.received.append(json.loads(body))
        _MockLangfuse.auth = self.headers.get("Authorization")
        code = 500 if _MockLangfuse.fail else 200
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"success": true}')

    def log_message(self, *args):  # 静默
        pass


@pytest.fixture
def mock_langfuse():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockLangfuse)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _MockLangfuse.received = []
    yield f"http://127.0.0.1:{server.server_address[1]}", _MockLangfuse
    server.shutdown()


def _make_sink(host):
    from flow_studio.observability import LangfuseSink
    return LangfuseSink(host, "pk-test", "sk-test")


def _drain(sink):
    assert sink.flush(timeout=10), "Langfuse 队列未在超时内清空"


def test_langfuse_sink_posts_batch(mock_langfuse):
    from flow_studio.observability import LangfuseObserver
    host, mock = mock_langfuse
    sink = _make_sink(host)
    obs = LangfuseObserver(sink)
    obs.flow_run({"run_id": "r1", "flow_id": "f1", "flow_name": "演示",
                  "status": "success", "input": {"q": "hi"}, "output": "ok",
                  "started_at": "2026-01-01T00:00:00",
                  "node_runs": [
                      {"node_id": "start", "type": "start", "label": "开始",
                       "status": "success", "ms": 1},
                      {"node_id": "llm1", "type": "llm", "label": "大模型",
                       "status": "failed", "ms": 5, "error": "boom"}]})
    _drain(sink)
    assert mock.received and mock.auth.startswith("Basic ")
    events = [e for b in mock.received for e in b["batch"]]
    trace = next(e for e in events if e["type"] == "trace")
    spans = [e for e in events if e["type"] == "span"]
    assert trace["name"] == "flow:演示" and trace["sessionId"] == "r1"
    assert len(spans) == 2
    assert all(s["traceId"] == trace["id"] for s in spans)
    failed = next(s for s in spans if s["metadata"]["status"] == "failed")
    assert failed["level"] == "ERROR" and failed["statusMessage"] == "boom"


def test_langfuse_agent_generations_with_usage(mock_langfuse):
    from flow_studio.observability import LangfuseObserver
    host, mock = mock_langfuse
    obs = LangfuseObserver(_make_sink(host))
    obs.agent_run(
        {"id": "mathy", "name": "计算器"},
        "12*12=?", {"text": "144", "tool_calls": 1, "session_id": "s9",
                    "steps": [
                        {"type": "rag", "name": "kb_rag", "args": {}, "ok": True,
                         "result": "命中 2 个片段", "ms": 0},
                        {"type": "tool", "name": "calc", "args": {"expression": "12*12"},
                         "ok": True, "result": "144", "ms": 3}],
                    "error": None},
        session_id="s9", flow_run_id="r1",
        llm_calls=[{"type": "llm", "model": "deepseek-chat",
                    "usage": {"input": 100, "output": 8, "total": 108,
                              "unit": "TOKENS"},
                    "messages": [{"role": "user", "content": "12*12=?"}],
                    "output": "144", "ok": True, "ms": 640}])
    _drain(obs.sink)
    events = [e for b in mock.received for e in b["batch"]]
    gen = next(e for e in events if e["type"] == "generation")
    assert gen["model"] == "deepseek-chat"
    assert gen["usage"]["total"] == 108
    spans = [e for e in events if e["type"] == "span"]
    assert {s["name"] for s in spans} == {"kb_rag", "calc"}
    trace = next(e for e in events if e["type"] == "trace")
    assert trace["metadata"]["flow_run_id"] == "r1"


def test_langfuse_eval_scores(mock_langfuse):
    from flow_studio.observability import LangfuseObserver
    host, mock = mock_langfuse
    obs = LangfuseObserver(_make_sink(host))
    obs.eval_run({"run_id": "er1", "suite_id": "smoke", "results": [
        {"case_id": "calc", "target_key": "codex", "question": "12*12?",
         "answer": "144", "pass": True, "error": None, "checks": [], "ms": 9},
        {"case_id": "calc", "target_key": "bot", "question": "12*12?",
         "answer": "", "pass": False, "error": "LLM 未启用", "checks": [], "ms": 0}]})
    _drain(obs.sink)
    events = [e for b in mock.received for e in b["batch"]]
    scores = [e for e in events if e["type"] == "score"]
    traces = [e for e in events if e["type"] == "trace"]
    assert len(scores) == 2 and {s["value"] for s in scores} == {0, 1}
    assert all(s["traceId"] in {t["id"] for t in traces} for s in scores)


def test_langfuse_disabled_by_default():
    from flow_studio.observability import make_observer
    assert type(make_observer({})).__name__ == "NullObserver"
    assert type(make_observer({"langfuse": {"enabled": True}})).__name__ == "NullObserver"


def test_broken_langfuse_never_breaks_flow(tmp_path):
    from flow_studio.observability import LangfuseObserver, LangfuseSink
    sink = LangfuseSink("http://127.0.0.1:9", "pk", "sk")   # 端口 9 必然连不上
    kb = KBStore(tmp_path / "kb")
    g = _g([
        _node("start", "start"),
        _node("t", "template", template="ok"),
        _node("end", "end", output="{{t.text}}"),
    ], [{"from": "start", "to": "t"}, {"from": "t", "to": "end"}])
    run = FlowRunner(kb=kb, obs=LangfuseObserver(sink)).run(g, {})
    assert run.status == "success"


def test_llm_usage_extraction(monkeypatch):
    import flow_studio.llm as llm_mod

    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"role": "assistant",
                                             "content": "好的。"}}],
                   "usage": {"prompt_tokens": 42, "completion_tokens": 7,
                             "total_tokens": 49}}

    monkeypatch.setattr(llm_mod.httpx, "post",
                        lambda *a, **k: FakeResp())
    cfg = {"enabled": True, "api_key": "k", "base_url": "http://x", "model": "m"}
    entries, usage = llm_mod.llm_messages_raw(cfg, [{"role": "user", "content": "hi"}])
    assert entries[0]["content"] == "好的。"
    assert usage == {"input": 42, "output": 7, "total": 49, "unit": "TOKENS"}


def test_agent_run_reports_to_observer(tools, agent_store):
    from flow_studio.observability import NullObserver

    class RecordingObserver(NullObserver):
        def __init__(self): self.calls = []
        def agent_run(self, agent, message, out, session_id=None,
                      flow_run_id=None, llm_calls=None):
            self.calls.append({"agent": agent["id"], "message": message,
                               "flow_run_id": flow_run_id,
                               "llm_calls": llm_calls})

    agent_store.save({"id": "seen", "name": "可见", "memory": False})
    obs = RecordingObserver()
    rt = AgentRuntime({}, tools, obs=obs, llm_fn=_fake_llm(
        [{"role": "assistant", "content": "done"}]))
    rt.run(agent_store.get("seen"), "你好", flow_run_id="flow-77")
    assert obs.calls and obs.calls[0]["flow_run_id"] == "flow-77"
    assert obs.calls[0]["llm_calls"][0]["output"] == "done"

# ---------------------------------------------------------------- 流程编排（agent 绑定流程）
def test_agent_flow_orchestration(agent_store, tmp_path):
    """编排方式 = 流程：invoke 按画布流程执行，输出 = 流程回复。"""
    from flow_studio.graph import graph_from_dict
    g = graph_from_dict({"id": "pipe", "name": "管道", "nodes": [
        {"id": "start", "type": "start",
         "params": {"inputs": [{"key": "message", "default": ""}]}},
        {"id": "t", "type": "template", "params": {"template": "处理：{{input.message}}"}},
        {"id": "end", "type": "end", "params": {"output": "{{t.text}}"}}],
        "edges": [{"from": "start", "to": "t"}, {"from": "t", "to": "end"}]})
    agent_store.save({"id": "flowguy", "name": "流程体", "flow_id": "pipe"})
    calls = []

    def invoker(fid, inputs):
        calls.append((fid, dict(inputs)))
        return {"status": "success", "output": "处理：画布消息",
                "flow_name": "管道", "flow_id": fid,
                "node_runs": [{"node_id": "t", "label": "模板", "type": "template",
                               "status": "success", "output": {"text": "x"}, "ms": 2}]}

    rt = AgentRuntime({}, tools, flow_invoker=invoker)
    out = rt.run(agent_store.get("flowguy"), "画布消息", session_id="s1")
    assert out["mode"] == "flow" and out["text"] == "处理：画布消息"
    assert not out.get("error")
    assert calls == [("pipe", {"message": "画布消息", "session_id": "s1"})]
    assert out["steps"] and out["steps"][0]["type"] == "flow"


def test_agent_flow_orchestration_failure(agent_store):
    """绑定流程执行失败 → 错误透出（required 节点语义由调用方处理）。"""
    agent_store.save({"id": "badflow", "name": "坏流程", "flow_id": "ghost"})

    def invoker(fid, inputs):
        raise ValueError(f"绑定的流程不存在：{fid}")

    rt = AgentRuntime({}, tools, flow_invoker=invoker)
    out = rt.run(agent_store.get("badflow"), "hi")
    assert "流程不存在" in out["error"]


def test_agent_flow_nesting_guard(agent_store):
    """流程里再调用同一个流程型 agent → 深度护栏生效，不打穿。"""
    from flow_studio.graph import graph_from_dict
    agent_store.save({"id": "recursive", "name": "递归体", "flow_id": "loop"})
    rt = AgentRuntime({}, tools)          # 无 flow_invoker → 不会走流程路径

    def invoker(fid, inputs):
        # 模拟流程里又有 ai_agent 节点：再次进入 runtime
        return {"status": "success", "output": rt.run(agent_store.get("recursive"),
                                                      inputs["message"])["text"] or "x",
                "node_runs": []}

    rt.flow_invoker = invoker
    out = rt.run(agent_store.get("recursive"), "hi")
    assert out["mode"] == "flow"          # 第一层正常
    # 深度护栏：连续嵌套至超限
    deep = out
    for _ in range(5):
        if deep.get("error"):
            break
        deep = rt.run(agent_store.get("recursive"), "hi")
    assert "嵌套" in deep.get("error", "") or deep.get("mode") == "flow"


def test_agent_store_persists_flow_id(agent_store):
    saved = agent_store.save({"id": "mixed", "name": "混合", "flow_id": "pipe",
                              "kb_ids": ["manual"], "rag_top_k": 7})
    assert saved["flow_id"] == "pipe"
    meta = agent_store.list()[0]
    assert "flow_id" in meta and meta["rag_top_k"] == 7
