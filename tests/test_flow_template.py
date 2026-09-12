"""Flow Studio：模板渲染与安全表达式测试。"""

import pytest

from flow_studio.template import ExprError, build_namespace, eval_expr, render, render_deep


@pytest.fixture
def ns():
    return build_namespace(
        {"today": "2026-09-11", "run_id": "r1"},
        {"city": "苏州", "kw": "Java"},
        {"match": {"total": 8, "passed_count": 3, "jobs": [{"title": "Java 架构师"}]},
         "advisor": {"text": "不错"}})


def test_render_basic(ns):
    assert render("今朝 {{vars.today}} 查 {{input.city}}", ns) == "今朝 2026-09-11 查 苏州"
    assert render("命中 {{match.passed_count}} 个", ns) == "命中 3 个"
    assert render("第一个：{{match.jobs.0.title}}", ns) == "第一个：Java 架构师"


def test_render_missing_and_types(ns):
    assert render("缺失 {{nope.deep}} →空", ns) == "缺失  →空"
    assert render("对象 {{match.jobs}}", ns).startswith("对象 [{")


def test_render_deep(ns):
    assert render_deep({"a": "{{input.kw}}", "list": ["{{match.total}}", 1]}, ns) == \
        {"a": "Java", "list": ["8", 1]}


def test_eval_ok(ns):
    assert eval_expr("match.passed_count > 0", ns) is True
    assert eval_expr("match.total >= 8 and input.city == '苏州'", ns) is True
    assert eval_expr("match.passed_count > 5 or 'Java' in input.kw", ns) is True
    assert eval_expr("not (match.total < 8)", ns) is True
    assert eval_expr("match.total - match.passed_count == 5", ns) is True


def test_eval_safety():
    ns = build_namespace({}, {}, {"m": {"n": 1}})
    with pytest.raises(ExprError):
        eval_expr("__import__('os').system('ls')", ns)
    with pytest.raises(ExprError):
        eval_expr("(lambda: 1)()", ns)
    with pytest.raises(ExprError):
        eval_expr("open('/etc/passwd')", ns)
    with pytest.raises(ExprError):
        eval_expr("m.n.__class__", ns)  # dict 之外的属性访问拒绝


def test_eval_errors():
    with pytest.raises(ExprError):
        eval_expr("match.total >", {})
    with pytest.raises(ExprError):
        eval_expr("   ", {})
    with pytest.raises(ExprError):
        eval_expr("unknown_var > 1", {})
