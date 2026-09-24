"""Flow Studio：执行引擎测试（顺序 / 条件 / 降级 / 防环 / 失败传播）。"""

import pytest

from flow_studio.engine import FlowRunner
from flow_studio.graph import FlowGraph, graph_from_dict
from flow_studio.registry import AgentRegistry


def _g(nodes, edges, **kw):
    return graph_from_dict({"id": kw.get("id", "t"), "name": "测试",
                            "nodes": nodes, "edges": edges})


def _node(id, type, **params):
    return {"id": id, "type": type, "params": params}


def test_sequence_and_template():
    g = _g([
        _node("start", "start", inputs=[{"key": "kw", "default": "Java"}]),
        _node("mid", "template", template="查 {{input.kw}} @ {{vars.today}}"),
        _node("end", "end", output="结果：{{mid.text}}"),
    ], [{"from": "start", "to": "mid"}, {"from": "mid", "to": "end"}])
    run = FlowRunner().run(g, {})
    assert run.status == "success"
    assert run.output.startswith("结果：查 Java @ ")
    assert [n.node_id for n in run.node_runs] == ["start", "mid", "end"]


def test_condition_two_branches():
    def make(branch_expr):
        return _g([
            _node("start", "start"),
            _node("m", "template", template="x"),
            _node("branch", "condition"),
            _node("hi", "template", template="有货"),
            _node("lo", "template", template="没货"),
            _node("end1", "end", output="{{hi.text}}"),
            _node("end2", "end", output="{{lo.text}}"),
        ], [
            {"from": "start", "to": "m"}, {"from": "m", "to": "branch"},
            {"from": "branch", "to": "hi", "branch": branch_expr},
            {"from": "branch", "to": "lo", "branch": "else"},
            {"from": "hi", "to": "end1"}, {"from": "lo", "to": "end2"},
        ])

    run = FlowRunner().run(make("1 == 2"), {})
    assert run.output == "没货"
    assert run.node_run("hi") is None

    run2 = FlowRunner().run(make("1 == 1"), {})
    assert run2.output == "有货"
    assert run2.node_run("lo") is None


def test_condition_bad_expr_falls_to_else():
    g = _g([
        _node("start", "start"),
        _node("branch", "condition"),
        _node("ok", "template", template="兜底"),
        _node("end", "end", output="{{ok.text}}"),
    ], [
        {"from": "start", "to": "branch"},
        {"from": "branch", "to": "ok", "branch": "!!!bad!!!"},
        {"from": "branch", "to": "ok", "branch": "else"},
        {"from": "ok", "to": "end"},
    ])
    run = FlowRunner().run(g, {})
    assert run.status == "success" and run.output == "兜底"


@pytest.mark.parametrize("operator,value,value_type,input_value,expected", [
    ("equals", "/works", "string", "/works", "命中"),
    ("contains", "岗位", "string", "看看岗位", "命中"),
    ("greater_than", "10", "number", 11, "命中"),
    ("equals", "true", "boolean", True, "命中"),
    ("equals", "", "null", None, "命中"),
    ("equals", "/works", "string", "/jobs", "兜底"),
])
def test_condition_structured_comparison(operator, value, value_type,
                                         input_value, expected):
    graph = _g([
        _node("start", "start"),
        _node("branch", "condition", source="input.value"),
        _node("hit", "end", output="命中"),
        _node("fallback", "end", output="兜底"),
    ], [
        {"from": "start", "to": "branch"},
        {"from": "branch", "to": "hit", "operator": operator,
         "value": value, "value_type": value_type},
        {"from": "branch", "to": "fallback", "branch": "else"},
    ])
    run = FlowRunner().run(graph, {"value": input_value})
    assert run.status == "success" and run.output == expected


def test_condition_missing_structured_source_falls_to_else():
    graph = _g([
        _node("start", "start"), _node("branch", "condition", source="input.missing"),
        _node("hit", "end", output="命中"), _node("fallback", "end", output="兜底"),
    ], [
        {"from": "start", "to": "branch"},
        {"from": "branch", "to": "hit", "operator": "not_equals",
         "value": "x", "value_type": "string"},
        {"from": "branch", "to": "fallback", "branch": "else"},
    ])
    assert FlowRunner().run(graph, {}).output == "兜底"


def test_condition_output_mode_selects_first_match_and_renders_template():
    graph = _g([
        _node("start", "start"),
        _node("prompt", "condition", outputs=[
            {"name": "project", "expression": "'/project ' in input.message",
             "output": "查询项目资料：{{input.message}}"},
            {"name": "fallback-project", "expression": "'project' in input.message",
             "output": "不应命中"},
        ], default_output="处理访客请求：{{input.message}}"),
        _node("end", "end", output="{{prompt.rule}}|{{prompt.matched}}|{{prompt.text}}"),
    ], [
        {"from": "start", "to": "prompt"},
        {"from": "prompt", "to": "end"},
    ])

    matched = FlowRunner().run(graph, {"message": "/project agentroam"})
    assert matched.output == "project|True|查询项目资料：/project agentroam"

    fallback = FlowRunner().run(graph, {"message": "你好"})
    assert fallback.output == "default|False|处理访客请求：你好"


def test_condition_output_mode_treats_bad_expression_as_not_matched():
    graph = _g([
        _node("start", "start"),
        _node("prompt", "condition", outputs=[
            {"name": "bad", "expression": "!!!bad!!!", "output": "bad"},
        ], default_output="{{input.message}}"),
        _node("end", "end", output="{{prompt.text}}"),
    ], [{"from": "start", "to": "prompt"}, {"from": "prompt", "to": "end"}])

    assert FlowRunner().run(graph, {"message": "原文"}).output == "原文"


def test_llm_degrades_without_config():
    g = _g([
        _node("start", "start"),
        _node("llm", "llm", system="s", prompt="p", required=False),
        _node("t", "template", template="降级后继续 {{llm.text}}"),
        _node("end", "end", output="{{t.text}}"),
    ], [{"from": "start", "to": "llm"}, {"from": "llm", "to": "t"},
        {"from": "t", "to": "end"}])
    run = FlowRunner(llm_cfg={"enabled": False}).run(g, {})
    assert run.status == "success"
    assert run.node_run("llm").status == "skipped"
    assert run.output == "降级后继续 "


def test_llm_required_fails_flow():
    g = _g([
        _node("start", "start"),
        _node("llm", "llm", prompt="p", required=True),
        _node("end", "end", output="x"),
    ], [{"from": "start", "to": "llm"}, {"from": "llm", "to": "end"}])
    run = FlowRunner(llm_cfg={"enabled": False}).run(g, {})
    assert run.status == "failed"
    assert "LLM" in run.error


def test_llm_success_with_fake_transport(monkeypatch):
    import flow_studio.engine as engine_mod
    monkeypatch.setattr(engine_mod, "llm_chat",
                        lambda cfg, system, user, temperature=0.3: "模型回答")
    g = _g([
        _node("start", "start"),
        _node("llm", "llm", prompt="{{input.q}}", required=True),
        _node("end", "end", output="{{llm.text}}"),
    ], [{"from": "start", "to": "llm"}, {"from": "llm", "to": "end"}])
    run = FlowRunner(llm_cfg={"enabled": True}).run(g, {"q": "你好"})
    assert run.status == "success" and run.output == "模型回答"


def test_agent_node_with_registry_and_coercion():
    reg = AgentRegistry()
    seen = {}

    def action(args):
        seen.update(args)
        return {"count": args["days"], "flag": args["reset"]}

    reg.register("mock", "do", action,
                 params=[{"key": "days", "type": "number"},
                         {"key": "reset", "type": "bool"}])
    g = _g([
        _node("start", "start", inputs=[{"key": "reset", "default": False}]),
        _node("a", "agent", agent="mock", action="do",
              args={"days": "5", "reset": "{{input.reset}}"}),
        _node("end", "end", output="{{a.count}}/{{a.flag}}"),
    ], [{"from": "start", "to": "a"}, {"from": "a", "to": "end"}])
    run = FlowRunner(reg).run(g, {"reset": "false"})
    assert run.status == "success"
    assert seen["days"] == 5 and seen["reset"] is False  # 字符串已被按声明类型纠正
    assert run.output == "5/False"


def test_agent_unknown_action_fails():
    g = _g([
        _node("start", "start"),
        _node("a", "agent", agent="nope", action="x"),
        _node("end", "end", output="y"),
    ], [{"from": "start", "to": "a"}, {"from": "a", "to": "end"}])
    run = FlowRunner(AgentRegistry()).run(g, {})
    assert run.status == "failed" and "未注册" in run.error


def test_agent_optional_degrades():
    def boom(args):
        raise RuntimeError("炸了")
    reg = AgentRegistry()
    reg.register("mock", "boom", boom)
    g = _g([
        _node("start", "start"),
        _node("a", "agent", agent="mock", action="boom", optional=True),
        _node("end", "end", output="继续"),
    ], [{"from": "start", "to": "a"}, {"from": "a", "to": "end"}])
    run = FlowRunner(reg).run(g, {})
    assert run.status == "success"
    assert run.node_run("a").status == "skipped"


def test_cycle_terminates():
    g = _g([
        _node("start", "start"),
        _node("a", "template", template="a"),
        _node("b", "template", template="b"),
        _node("end", "end", output="done"),
    ], [{"from": "start", "to": "a"}, {"from": "a", "to": "b"},
        {"from": "b", "to": "a"}, {"from": "b", "to": "end"}])
    run = FlowRunner().run(g, {})   # 不应死循环
    assert run.status == "success" and run.output == "done"


def test_http_unreachable():
    g = _g([
        _node("start", "start"),
        _node("h", "http", url="http://127.0.0.1:1/nope", timeout=2),
        _node("end", "end", output="x"),
    ], [{"from": "start", "to": "h"}, {"from": "h", "to": "end"}])
    assert FlowRunner().run(g, {}).status == "failed"

    g2 = _g([
        _node("start", "start"),
        _node("h", "http", url="http://127.0.0.1:1/nope", timeout=2, optional=True),
        _node("end", "end", output="ok"),
    ], [{"from": "start", "to": "h"}, {"from": "h", "to": "end"}])
    run2 = FlowRunner().run(g2, {})
    assert run2.status == "success" and run2.node_run("h").status == "skipped"


# ---------------- 意图节点 ----------------
_INTENTS = [{"name": "a", "samples": ["看岗位"]}, {"name": "b", "samples": ["看趋势"]}]


def _intent_graph(branch_to: dict) -> FlowGraph:
    """branch_to: {意图名或else: 模板文本}；end 输出渲染命中的意图名。"""
    nodes = [
        _node("start", "start", inputs=[{"key": "message", "default": ""}]),
        _node("router", "intent", source="{{input.message}}", intents=_INTENTS),
        _node("end", "end", output="命中:{{router.intent}}"),
    ]
    edges = [{"from": "start", "to": "router"}]
    for i, (branch, text) in enumerate(branch_to.items()):
        nodes.insert(len(nodes) - 1, _node(f"t{i}", "template", template=text))
        edges += [{"from": "router", "to": f"t{i}", "branch": branch},
                  {"from": f"t{i}", "to": "end"}]
    return _g(nodes, edges)


def test_intent_node_keyword_route():
    g = _intent_graph({"a": "岗位路", "b": "趋势路", "else": "兜底路"})
    run = FlowRunner().run(g, {"message": "帮我看看岗位"})
    assert run.status == "success" and run.output == "命中:a"
    assert run.node_run("t0") is not None and run.node_run("t1") is None
    nr = run.node_run("router")
    assert nr.output["matched"] is True and nr.output["via"] == "keyword"


def test_intent_node_else_fallback():
    g = _intent_graph({"a": "岗位路", "else": "兜底路"})
    run = FlowRunner().run(g, {"message": "讲个笑话"})
    assert run.status == "success" and run.output == "命中:"
    assert run.node_run("t0") is None          # "岗位路"分支未执行
    assert run.node_run("router").output["matched"] is False


def test_intent_node_llm_route(monkeypatch):
    from flow_studio import intent as intent_mod
    monkeypatch.setattr(intent_mod, "llm_json", lambda cfg, s, u:
                        {"intent": "b", "confidence": 0.88})
    g = _intent_graph({"a": "岗位路", "b": "趋势路", "else": "兜底路"})
    run = FlowRunner(llm_cfg={"enabled": True, "api_key": "k"}).run(
        g, {"message": "随便一句话"})
    assert run.output == "命中:b"
    assert run.node_run("router").output["via"] == "llm"


def test_intent_node_no_else_ends_quietly():
    g = _intent_graph({"a": "岗位路"})
    run = FlowRunner().run(g, {"message": "讲个笑话"})
    assert run.status == "success" and run.output == ""


def test_intent_node_required_fails_when_unmatched():
    nodes = [
        _node("start", "start", inputs=[{"key": "message", "default": ""}]),
        _node("router", "intent", source="{{input.message}}", required=True,
              intents=_INTENTS),
        _node("end", "end", output="x"),
    ]
    g = _g(nodes, [{"from": "start", "to": "router"},
                   {"from": "router", "to": "end", "branch": "else"}])
    run = FlowRunner().run(g, {"message": "无关的话"})
    assert run.status == "failed" and "意图未命中" in run.error
