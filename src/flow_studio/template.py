"""模板渲染 {{ 路径 }} 与条件表达式求值（受限 AST，禁止调用/属性逃逸）。

命名空间解析顺序：vars → input → 节点输出（按节点 id）。
路径如 `match.passed_count`、`input.city`、`vars.today`；dict 取键、list 取下标。
"""

from __future__ import annotations

import ast
import json
import re

_TMPL = re.compile(r"\{\{\s*([a-zA-Z_][\w.\[\]]*)\s*\}\}")

_MISSING = object()


def _lookup(ns: dict, path: str):
    """按命名空间逐级取值；找不到返回 _MISSING。"""
    parts = path.split(".")
    head = parts[0]
    if head in ns:
        cur = ns[head]
    else:
        # 头段不在命名空间 → 尝试整体作为节点 id 的字段（节点 id 可能含点？不允许，略）
        return _MISSING
    for seg in parts[1:]:
        if isinstance(cur, dict):
            cur = cur.get(seg, _MISSING)
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return _MISSING
        else:
            return _MISSING
        if cur is _MISSING:
            return _MISSING
    return cur


def resolve_path(path: str, ns: dict) -> tuple[bool, object]:
    """Resolve a namespace path without evaluating code.

    Returns ``(False, None)`` for a missing or invalid path so condition edges
    can treat missing data as a non-match instead of confusing it with JSON
    ``null``.
    """
    path = str(path or "").strip()
    if not path or not re.fullmatch(r"[a-zA-Z_][\w]*(?:\.[a-zA-Z_0-9][\w]*)*", path):
        return False, None
    value = _lookup(ns, path)
    return (False, None) if value is _MISSING else (True, value)


def _to_str(val) -> str:
    if val is None or val is _MISSING:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, (dict, list)):
        return json.dumps(val, ensure_ascii=False)
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val)


def build_namespace(vars_: dict, inputs: dict, node_outputs: dict) -> dict:
    """合并求值命名空间：vars / input / 各节点输出（后者优先级依次增高）。"""
    return {
        "vars": dict(vars_ or {}),
        "input": dict(inputs or {}),
        **(node_outputs or {}),
    }


def render(text: str, ns: dict) -> str:
    """{{ 路径 }} 占位符替换；未命中渲染为空串。"""
    if not text or "{{" not in text:
        return text or ""

    def sub(m: re.Match) -> str:
        return _to_str(_lookup(ns, m.group(1)))

    return _TMPL.sub(sub, text)


def render_deep(value, ns: dict):
    """递归渲染：str → 模板；dict/list → 逐项；其余原样。用于 agent args 等。"""
    if isinstance(value, str):
        return render(value, ns)
    if isinstance(value, dict):
        return {k: render_deep(v, ns) for k, v in value.items()}
    if isinstance(value, list):
        return [render_deep(v, ns) for v in value]
    return value


def resolve(text: str, ns: dict):
    """模板解析为原始值：整条模板恰为一个占位符且命中 list/dict 时原样返回
    （供节点引用上游列表，如 {{storyboard.shots}}）；否则按字符串渲染。"""
    if text:
        m = _TMPL.fullmatch(text.strip())
        if m:
            val = _lookup(ns, m.group(1))
            if val is not _MISSING and isinstance(val, (list, dict)):
                return val
    return render(text or "", ns)


# ---------------------------------------------------------------- 条件表达式
_ALLOWED_BIN = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod)
_ALLOWED_CMP = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
                ast.In, ast.NotIn, ast.Is, ast.IsNot)


class ExprError(ValueError):
    """表达式非法（语法不允许或求值失败）。"""


def _eval_node(node: ast.AST, ns: dict):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, ns)
    if isinstance(node, ast.Constant):
        if node.value is None or isinstance(node.value, (bool, int, float, str)):
            return node.value
        raise ExprError(f"不支持常量类型：{type(node.value).__name__}")
    if isinstance(node, ast.Name):
        val = ns.get(node.id, _MISSING)
        if val is _MISSING:
            if node.id == "true":
                return True
            if node.id == "false":
                return False
            if node.id == "null":
                return None
            raise ExprError(f"未知变量：{node.id}")
        return val
    if isinstance(node, ast.Attribute):
        val = _eval_node(node.value, ns)
        if isinstance(val, dict):
            return val.get(node.attr)
        raise ExprError("只能对 JSON 对象取字段")
    if isinstance(node, ast.Subscript):
        val = _eval_node(node.value, ns)
        idx = _eval_node(node.slice, ns)
        try:
            if isinstance(val, dict):
                return val.get(idx)
            if isinstance(val, (list, tuple)):
                return val[int(idx)]
        except (ValueError, TypeError, IndexError):
            raise ExprError(f"下标越界或类型不符：{idx}")
        raise ExprError("只能对对象/数组取下标")
    if isinstance(node, ast.BoolOp):
        vals = [_truthy(_eval_node(v, ns)) for v in node.values]
        if isinstance(node.op, ast.And):
            return all(vals)
        return any(vals)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _truthy(_eval_node(node.operand, ns))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval_node(node.operand, ns)
    if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BIN):
        return _binop(node.op, _eval_node(node.left, ns), _eval_node(node.right, ns))
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, ns)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval_node(comp, ns)
            if not _compare(op, left, right):
                return False
            left = right
        return True
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval_node(v, ns) for v in node.elts]
    if isinstance(node, ast.Dict):
        return {_eval_node(k, ns): _eval_node(v, ns)
                for k, v in zip(node.keys, node.values) if k is not None}
    raise ExprError(f"表达式不允许该语法：{type(node).__name__}")


def _binop(op, a, b):
    try:
        if isinstance(op, ast.Add):
            return a + b
        if isinstance(op, ast.Sub):
            return a - b
        if isinstance(op, ast.Mult):
            return a * b
        if isinstance(op, ast.Div):
            return a / b
        if isinstance(op, ast.FloorDiv):
            return a // b
        if isinstance(op, ast.Mod):
            return a % b
    except TypeError:
        raise ExprError("算术运算类型不符")
    raise ExprError("不支持算术运算符")


def _compare(op, a, b) -> bool:
    if isinstance(op, ast.Eq):
        return a == b
    if isinstance(op, ast.NotEq):
        return a != b
    if isinstance(op, ast.In):
        return a in (b or [])
    if isinstance(op, ast.NotIn):
        return a not in (b or [])
    if isinstance(op, ast.Is):
        return a is b
    if isinstance(op, ast.IsNot):
        return a is not b
    try:
        if isinstance(op, ast.Lt):
            return a < b
        if isinstance(op, ast.LtE):
            return a <= b
        if isinstance(op, ast.Gt):
            return a > b
        if isinstance(op, ast.GtE):
            return a >= b
    except TypeError:
        raise ExprError("比较类型不符")
    raise ExprError("不支持比较运算符")


def _truthy(val) -> bool:
    if val is _MISSING or val is None:
        return False
    return bool(val)


def eval_expr(expr: str, ns: dict) -> bool:
    """安全求值条件表达式，返回真值。任何语法/求值错误抛 ExprError。"""
    if not expr or not expr.strip():
        raise ExprError("空表达式")
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as e:
        raise ExprError(f"表达式语法错误：{expr!r}（{e.msg}）")
    # 黑名单兜底：出现调用/属性外的可疑节点直接拒绝（在 _eval_node 里也会拦）
    for sub in ast.walk(tree):
        if isinstance(sub, (ast.Call, ast.Lambda, ast.Import, ast.ImportFrom,
                            ast.NamedExpr, ast.Await, ast.Yield)):
            raise ExprError("表达式不允许函数调用/导入")
    return _truthy(_eval_node(tree, ns))


def compare_value(actual, operator: str, expected) -> bool:
    """Evaluate one structured condition without arbitrary code execution."""
    operator = str(operator or "").strip()
    if operator == "equals":
        return actual == expected
    if operator == "not_equals":
        return actual != expected
    if operator in {"contains", "not_contains"}:
        try:
            matched = expected in actual
        except (TypeError, ValueError):
            return False
        return matched if operator == "contains" else not matched
    comparisons = {
        "greater_than": lambda: actual > expected,
        "greater_or_equal": lambda: actual >= expected,
        "less_than": lambda: actual < expected,
        "less_or_equal": lambda: actual <= expected,
    }
    fn = comparisons.get(operator)
    if fn is None:
        raise ExprError(f"不支持结构化比较运算符：{operator}")
    try:
        return bool(fn())
    except TypeError as exc:
        raise ExprError("比较类型不符") from exc
