"""Flow Studio：图模型与校验测试。"""

import pytest

from flow_studio.graph import FlowGraph, Node, graph_from_dict, start_inputs, validate


def _ok_graph() -> dict:
    return {
        "id": "t1", "name": "测试流程",
        "nodes": [
            {"id": "start", "type": "start", "params": {"inputs": [
                {"key": "kw", "required": True, "default": "Java"}, "opt"]}},
            {"id": "end", "type": "end", "params": {"output": "ok {{input.kw}}"}},
        ],
        "edges": [{"from": "start", "to": "end"}],
    }


def _graph(nodes, edges) -> FlowGraph:
    return FlowGraph(id="t1", name="测试流程",
                     nodes=[Node(**n) for n in nodes], edges=edges)


def test_validate_ok():
    g = graph_from_dict(_ok_graph())
    assert validate(g) == []


def test_missing_start_or_end():
    errs = validate(_graph([{"id": "a", "type": "template"}], []))
    assert any("开始" in e for e in errs) and any("结束" in e for e in errs)


def test_duplicate_and_unknown_type():
    errs = validate(_graph(
        [{"id": "a", "type": "template"}, {"id": "a", "type": "nope"},
         {"id": "s", "type": "start"}, {"id": "e", "type": "end"}],
        [{"from": "s", "to": "a"}]))
    assert any("重复" in e for e in errs)
    assert any("类型未知" in e for e in errs)


def test_dangling_edges():
    errs = validate(_graph(
        [{"id": "s", "type": "start"}, {"id": "e", "type": "end"}],
        [{"from": "ghost", "to": "e"}, {"from": "s", "to": "nope"}]))
    assert any("起点不存在" in e for e in errs)
    assert any("终点不存在" in e for e in errs)


def test_condition_edge_requires_branch():
    errs = validate(_graph(
        [{"id": "c", "type": "condition"}, {"id": "s", "type": "start"},
         {"id": "e", "type": "end"}],
        [{"from": "s", "to": "c"}, {"from": "c", "to": "e"}]))
    assert any("branch" in e for e in errs)


def test_intent_edge_branch_must_be_declared():
    params = {"intents": [{"name": "a"}, {"name": "b"}]}
    errs = validate(_graph(
        [{"id": "i", "type": "intent", "params": params},
         {"id": "s", "type": "start"}, {"id": "e", "type": "end"}],
        [{"from": "s", "to": "i"},
         {"from": "i", "to": "e", "branch": "ghost"}]))
    assert any("不在意图清单" in e for e in errs)

    errs2 = validate(_graph(
        [{"id": "i", "type": "intent", "params": params},
         {"id": "s", "type": "start"}, {"id": "e", "type": "end"}],
        [{"from": "s", "to": "i"},
         {"from": "i", "to": "e", "branch": ""}]))
    assert any("缺少 branch" in e for e in errs2)

    errs3 = validate(_graph(
        [{"id": "i", "type": "intent", "params": params},
         {"id": "s", "type": "start"}, {"id": "e", "type": "end"}],
        [{"from": "s", "to": "i"},
         {"from": "i", "to": "e", "branch": "else"},
         {"from": "i", "to": "e", "branch": "a"}]))
    assert errs3 == []


def test_from_dict_rejects_bad():
    g = _ok_graph()
    g["edges"].append({"from": "start", "to": "nope"})
    with pytest.raises(ValueError, match="终点"):
        graph_from_dict(g)


def test_start_inputs():
    g = graph_from_dict(_ok_graph())
    keys = [i["key"] for i in start_inputs(g)]
    assert keys == ["kw", "opt"]
    assert start_inputs(g)[0]["required"] is True
    assert start_inputs(g)[0]["default"] == "Java"
