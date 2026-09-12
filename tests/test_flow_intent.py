"""Flow Studio：意图路由与 tools 清单测试。"""

from flow_studio.graph import graph_from_dict
from flow_studio.intent import classify_intent, parse_tool_call, route, tools_manifest


def _flows():
    demo = graph_from_dict({
        "id": "demo", "name": "应聘日检", "description": "跑一次模拟数据日检",
        "triggers": ["跑一下应聘demo", "模拟日检"],
        "nodes": [
            {"id": "start", "type": "start",
             "params": {"inputs": [{"key": "days", "required": False, "default": 10}]}},
            {"id": "end", "type": "end", "params": {}},
        ],
        "edges": [{"from": "start", "to": "end"}],
    })
    daily = graph_from_dict({
        "id": "daily", "name": "每日抓取", "description": "真实抓取招聘网站",
        "triggers": ["抓一下招聘网站"],
        "nodes": [
            {"id": "start", "type": "start", "params": {}},
            {"id": "end", "type": "end", "params": {}},
        ],
        "edges": [{"from": "start", "to": "end"}],
    })
    return [demo, daily]


def test_keyword_route_hit():
    r = route("帮我跑一下应聘demo", _flows(), llm_cfg={"enabled": False})
    assert r.matched and r.flow_id == "demo" and r.via == "keyword"


def test_keyword_route_miss_lists_flows():
    r = route("今天天气怎么样", _flows(), llm_cfg={"enabled": False})
    assert not r.matched
    assert "应聘日检" in r.reply and "每日抓取" in r.reply


def test_route_empty():
    assert not route("", _flows(), {}).matched
    assert not route("随便", [], {}).matched


def test_llm_route_with_mock(monkeypatch):
    from flow_studio import intent as intent_mod
    monkeypatch.setattr(intent_mod, "llm_json", lambda cfg, s, u:
                        {"flow_id": "daily", "params": {"city": "苏州"},
                         "confidence": 0.95, "reply": "选每日抓取"})
    r = route("去抓岗位", _flows(), llm_cfg={"enabled": True, "api_key": "k"})
    assert r.matched and r.flow_id == "daily" and r.via == "llm"
    assert r.params == {}  # city 不在流程输入里，被过滤


def test_llm_route_none_flow(monkeypatch):
    from flow_studio import intent as intent_mod
    monkeypatch.setattr(intent_mod, "llm_json", lambda cfg, s, u:
                        {"flow_id": None, "confidence": 0.2, "reply": "都不合适"})
    r = route("讲个笑话", _flows(), llm_cfg={"enabled": True, "api_key": "k"})
    assert not r.matched and r.reply == "都不合适"


def test_tools_manifest_and_parse():
    tools = tools_manifest(_flows())
    assert [t["function"]["name"] for t in tools] == ["demo", "daily"]
    demo_tool = tools[0]["function"]
    assert "days" in demo_tool["parameters"]["properties"]

    flow, params = parse_tool_call("demo", '{"days": 5, "hack": 1}', _flows())
    assert flow.id == "demo" and params == {"days": 5}
    flow2, params2 = parse_tool_call("ghost", {}, _flows())
    assert flow2 is None


# ---------------- 意图节点分类 ----------------
_INTENTS = [
    {"name": "find_jobs", "description": "想看岗位、跑应聘流程",
     "samples": ["看看今天的岗位", "跑一下应聘demo"]},
    {"name": "trend_analysis", "description": "市场趋势与技能热度",
     "samples": ["分析一下岗位趋势", "技能热度怎么样"]},
]


def test_classify_intent_keyword_hit():
    r = classify_intent("帮我看看今天的岗位", _INTENTS, llm_cfg={"enabled": False})
    assert r["intent"] == "find_jobs" and r["via"] == "keyword" and r["confidence"] > 0
    r2 = classify_intent("分析一下岗位趋势", _INTENTS, llm_cfg=None)
    assert r2["intent"] == "trend_analysis"


def test_classify_intent_keyword_miss():
    r = classify_intent("今天天气不错", _INTENTS, llm_cfg={"enabled": False})
    assert r["intent"] is None and r["via"] == "none" and r["confidence"] == 0.0


def test_classify_intent_llm_mock(monkeypatch):
    from flow_studio import intent as intent_mod
    monkeypatch.setattr(intent_mod, "llm_json", lambda cfg, s, u:
                        {"intent": "trend_analysis", "confidence": 0.9})
    r = classify_intent("随便说点什么", _INTENTS, llm_cfg={"enabled": True, "api_key": "k"})
    assert r["intent"] == "trend_analysis" and r["via"] == "llm"


def test_classify_intent_llm_invalid_falls_back(monkeypatch):
    from flow_studio import intent as intent_mod
    monkeypatch.setattr(intent_mod, "llm_json", lambda cfg, s, u:
                        {"intent": "ghost_intent", "confidence": 0.9})
    r = classify_intent("看看今天的岗位", _INTENTS, llm_cfg={"enabled": True, "api_key": "k"})
    assert r["intent"] == "find_jobs" and r["via"] == "keyword"


def test_classify_intent_empty_inputs():
    assert classify_intent("", _INTENTS, {})["intent"] is None
    assert classify_intent("你好", [], {})["intent"] is None
