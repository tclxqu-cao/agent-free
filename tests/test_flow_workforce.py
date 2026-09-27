"""数字员工（multi-agent workforce）层测试。

覆盖 src/flow_studio/ 下新增的编制层：
workforce.py（岗位模板 / compose / WorkforceStore）、board.py（共享看板状态机）、
bus.py（消息总线 + Hub）、crew.py（轮次驱动器与团队作用域协作工具）、
policy.py（workforce 策略门禁），以及 server.py 的 /api/workforce/* 路由。

约定：
- SSE 端点 /api/workforce/events 不做 TestClient 流式断言（starlette 阻塞 portal
  取不到事件），它的重放原语 bus.recent(workspace_id=, after_seq=) 在第 3 节直接覆盖。
- 驱动器单测全部用 _FakeRuntime（无 LLM），验证规则降级链路。

章节：
1. 岗位模板与自动编制
2. 共享看板
3. 消息总线
4. 编制存储 WorkforceStore
5. 团队驱动器 CrewDriver
6. 团队作用域协作工具
7. 策略门禁 workforce
8. HTTP API：RBAC / happy path / 治理集成 / CSRF
"""

import time

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from conftest import make_governed_client  # noqa: E402

from flow_studio.board import COLUMNS, TRANSITIONS, BoardError, KanbanBoard  # noqa: E402
from flow_studio.bus import (BROADCAST, HUMAN_ACTOR, MESSAGE_KINDS, Hub,  # noqa: E402
                             MessageBus)
from flow_studio.crew import TEAM_TOOLS, Actor, CrewDriver, CrewError  # noqa: E402
from flow_studio.governance import ROLE_CAPABILITIES  # noqa: E402
from flow_studio.policy import PolicyEngine  # noqa: E402
from flow_studio.server import _required_capability, create_app  # noqa: E402
from flow_studio.tools import ToolRegistry  # noqa: E402
from flow_studio.workforce import (CHAIN_ORDER, CORE_ROLES, EMPLOYEE_STATUSES,  # noqa: E402
                                   ROLE_TEMPLATES, TEAM_STATUSES, TEAM_TRANSITIONS,
                                   WorkforceStore, build_agent_spec, compose,
                                   normalize_plan, role_card)

WS = "default"
WS_B = "team-b"
TEAM = "team-unit"
OTHER_TEAM = "team-other"
PASSWORD = "test-password-123"
DEV_TASK = "开发一个会员积分商城产品，要有小程序和后台"
SALES_TASK = "面向渠道客户准备上市话术与报价推广材料"
QA_OPS_TASK = "上线前的回归测试用例与部署监控"


# ==================================================== 替身与公共夹具
class _FakeRuntime:
    """最小 WorkspaceRuntime 替身：无 LLM 时驱动器只读 llm_cfg。"""

    def __init__(self, llm_cfg=None):
        self.llm_cfg = dict(llm_cfg or {})

    def invoke_flow(self, flow_id, inputs):
        raise AssertionError("规则降级路径不应调用流程")


class _SilentCrew(CrewDriver):
    """员工按兵不动：制造「无新消息、无卡片迁移」的轮次。"""

    def _act(self, team_id, employee, round_no):
        return {"skipped": True}


class _SlowCrew(CrewDriver):
    """每个员工上场 0.05s 并发一条广播，让单轮耗时可控，便于验证轮次边界信号。"""

    def _act(self, team_id, employee, round_no):
        time.sleep(0.05)
        team = self._require_team(team_id)
        self.bus.post(workspace_id=team["workspace_id"], team_id=team_id,
                      from_actor=employee["id"], to_actor=BROADCAST, kind="announce",
                      subject=f"{employee['name']} 慢速心跳", body={"text": "本轮还在推进"},
                      round_no=round_no)
        return {}


class _BrokenCrew(CrewDriver):
    """员工执行必炸：验证单人失败不拖垮团队。"""

    def _invoke(self, team, employee, brief):
        raise RuntimeError("模型连接失败")


@pytest.fixture
def hub():
    return Hub()


@pytest.fixture
def bus(tmp_path, hub):
    return MessageBus(tmp_path / "workforce" / "bus.sqlite", hub=hub)


@pytest.fixture
def board(tmp_path, hub):
    return KanbanBoard(tmp_path / "workforce" / "board.sqlite", hub=hub)


@pytest.fixture
def teams(tmp_path):
    return WorkforceStore(tmp_path / "workforce" / "workforce.sqlite")


@pytest.fixture
def driver(teams, board, bus):
    return CrewDriver(teams=teams, board=board, bus=bus, runtime=_FakeRuntime())


@pytest.fixture
def app(config_dir, tmp_path):
    return create_app(config_dir, tmp_path / "data")


@pytest.fixture
def owner(app):
    return make_governed_client(app)


def _make_team(teams, task=DEV_TASK, plan=None, max_rounds=12, workspace_id=WS):
    return teams.create_team(workspace_id=workspace_id, task=task,
                             plan=plan or compose(task), created_by="owner",
                             max_rounds=max_rounds)


def _post(bus, *, team_id=TEAM, workspace_id=WS, from_actor="product", to_actor="qa",
          kind="request", subject="占位主题", body=None, parent_seq=None, round_no=0):
    return bus.post(workspace_id=workspace_id, team_id=team_id, from_actor=from_actor,
                    to_actor=to_actor, kind=kind, subject=subject, body=body,
                    parent_seq=parent_seq, round_no=round_no)


def _wait_until(predicate, timeout=20.0, interval=0.02):
    """轮询等待条件成立；超时返回 False，由调用方给出中文断言信息。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ============================================ 1. 岗位模板与自动编制
def test_role_templates_覆盖六个岗位并串成价值链():
    assert list(ROLE_TEMPLATES) == list(CHAIN_ORDER)
    assert set(CORE_ROLES) <= set(CHAIN_ORDER)
    for key in CHAIN_ORDER:
        role = role_card(key)
        assert role["key"] == key and role["name"] and role["charter"]
        assert role["deliverables"] and role["keywords"]
        assert set(role["downstream"]) <= set(CHAIN_ORDER)
    assert role_card("product")["downstream"] == ["architect"]
    assert role_card("sales")["downstream"] == []
    with pytest.raises(ValueError) as caught:
        role_card("hacker")
    assert "未知岗位" in str(caught.value)


def test_compose_开发类任务给出完整六岗价值链():
    plan = compose(DEV_TASK)
    assert [r["role"] for r in plan["roster"]] == list(CHAIN_ORDER)
    assert plan["via"] == "rules"
    assert [m["key"] for m in plan["milestones"]] == [
        f"m-{role}" for role in CHAIN_ORDER] + ["m-accept"]
    by_key = {m["key"]: m for m in plan["milestones"]}
    assert by_key["m-product"]["depends_on"] == []
    for prev, nxt in zip(CHAIN_ORDER, CHAIN_ORDER[1:]):
        assert by_key[f"m-{nxt}"]["depends_on"] == [f"m-{prev}"]
    assert by_key["m-accept"]["role"] == "product"
    assert by_key["m-accept"]["depends_on"] == ["m-ops", "m-sales"]
    assert plan["goal"] == DEV_TASK
    assert "全链路" in plan["reason"]
    assert all(r["focus"] == ROLE_TEMPLATES[r["role"]]["deliverables"]
               for r in plan["roster"])


def test_compose_非开发类任务只按关键词命中选岗():
    plan = compose(SALES_TASK)
    assert [r["role"] for r in plan["roster"]] == ["sales"]
    assert "关键词" in plan["roster"][0]["reason"]
    assert [m["key"] for m in plan["milestones"]] == ["m-sales"]

    mixed = compose(QA_OPS_TASK)
    assert [r["role"] for r in mixed["roster"]] == ["qa", "ops"]
    # 没有产品经理就没有验收环节，m-accept 会被裁掉
    assert [m["key"] for m in mixed["milestones"]] == ["m-qa", "m-ops"]
    assert "测试" in mixed["reason"] and "运维" in mixed["reason"]


def test_compose_关键词全落空时退回默认全链路():
    plan = compose("帮我把这份材料整理一下")
    assert [r["role"] for r in plan["roster"]] == list(CHAIN_ORDER)
    assert all(r["reason"] == "默认编制补齐该环节" for r in plan["roster"])


def test_compose_岗位白名单会裁剪编制并清理悬空依赖():
    plan = compose(DEV_TASK, allowed_roles=["product", "developer"])
    assert [r["role"] for r in plan["roster"]] == ["product", "developer"]
    by_key = {m["key"]: m for m in plan["milestones"]}
    # m-accept 挂在 product 名下所以保留，但它原来依赖的 m-ops/m-sales 被裁掉了
    assert set(by_key) == {"m-product", "m-developer", "m-accept"}
    assert by_key["m-developer"]["depends_on"] == []      # 上游 m-architect 已被裁
    assert by_key["m-accept"]["depends_on"] == []
    assert all(dep in by_key for m in plan["milestones"]
               for dep in m["depends_on"])


def test_compose_白名单里没有认识岗位时报错():
    with pytest.raises(ValueError) as caught:
        compose(DEV_TASK, allowed_roles=["hacker"])
    assert "岗位" in str(caught.value)


def test_compose_空任务直接报错():
    with pytest.raises(ValueError) as caught:
        compose("   ")
    assert "任务描述" in str(caught.value)


def test_compose_模型计划覆盖规则计划并过滤非法岗位():
    calls = []

    def fake_llm_json(llm_cfg, system, user):
        calls.append((llm_cfg, system, user))
        return {"goal": "模型给的目标", "reason": "模型依据任务挑选",
                "roster": [{"role": "qa", "reason": "只要测试"}, {"role": "bogus"},
                           {"role": "qa", "reason": "重复岗位"},
                           {"role": "developer", "focus": "接口联调"}],
                "milestones": [{"key": "d1", "title": "用例矩阵", "role": "qa",
                                "depends_on": ["d1", "ghost"]}]}

    plan = compose(DEV_TASK, {"enabled": True}, llm_json_fn=fake_llm_json)
    assert plan["via"] == "llm" and plan["reason"] == "模型依据任务挑选"
    assert plan["goal"] == "模型给的目标"
    assert [r["role"] for r in plan["roster"]] == ["qa", "developer"]   # 未知/重复被丢
    assert [m["key"] for m in plan["milestones"]] == ["d1"]
    assert plan["milestones"][0]["depends_on"] == []                    # 自引用+悬空清理
    assert len(calls) == 1
    assert calls[0][0] == {"enabled": True}
    assert DEV_TASK in calls[0][2] and "qa（测试）" in calls[0][2]


def test_compose_模型计划越界时仍按白名单裁剪():
    prompt = []

    def fake_llm_json(llm_cfg, system, user):
        prompt.append(user)
        return {"roster": [{"role": "sales"}, {"role": "qa"}],
                "milestones": [{"key": "s1", "title": "话术", "role": "sales"},
                               {"key": "q1", "title": "用例", "role": "qa",
                                "depends_on": ["s1"]}]}

    plan = compose(DEV_TASK, {"enabled": True}, allowed_roles=["qa"],
                   llm_json_fn=fake_llm_json)
    assert [r["role"] for r in plan["roster"]] == ["qa"]      # 白名单外岗位被裁
    assert [m["key"] for m in plan["milestones"]] == ["q1"]
    assert plan["milestones"][0]["depends_on"] == []         # s1 成了悬空引用
    assert "qa（" in prompt[0] and "sales（" not in prompt[0]  # 提示词也只给允许岗位


@pytest.mark.parametrize("raw", [None, {}, {"roster": []}, {"roster": [{"role": "hacker"}]}],
                         ids=["none", "empty", "no-roster", "unknown-role"])
def test_compose_模型返回不可用时退回规则计划(raw):
    plan = compose(DEV_TASK, {"enabled": True}, llm_json_fn=lambda *args: raw)
    assert plan["via"] == "rules"
    assert [r["role"] for r in plan["roster"]] == list(CHAIN_ORDER)


def test_compose_模型返回重复岗位时去重而不是整体作废():
    bloated = {"roster": [{"role": role} for role in CHAIN_ORDER] + [{"role": "product"}],
               "milestones": []}
    plan = compose(DEV_TASK, {"enabled": True}, llm_json_fn=lambda *a: bloated)
    # 去重发生在长度检查之前，所以 len(roster) > len(ROLE_TEMPLATES) 实际不可达
    assert plan["via"] == "llm"
    assert [r["role"] for r in plan["roster"]] == list(CHAIN_ORDER)
    assert [m["key"] for m in plan["milestones"]] == [f"m-{role}" for role in CHAIN_ORDER]


def test_compose_未启用模型时不会调用_llm_json():
    def explode(*args):
        raise AssertionError("未启用模型不该调用 LLM")

    plan = compose(DEV_TASK, {"enabled": False, "api_key": "sk-x"}, llm_json_fn=explode)
    assert plan["via"] == "rules"


def test_normalize_plan_回环保持岗位与里程碑():
    plan = compose(DEV_TASK)
    again = normalize_plan(DEV_TASK, plan)
    assert again["via"] == "human"
    assert [r["role"] for r in again["roster"]] == [r["role"] for r in plan["roster"]]
    assert [m["key"] for m in again["milestones"]] == [m["key"] for m in plan["milestones"]]
    assert again["goal"] == plan["goal"]
    assert again["reason"] == plan["reason"]        # 原计划自带理由时不被覆盖
    assert again["roster"] == plan["roster"]


def test_normalize_plan_清理悬空与自引用依赖():
    plan = normalize_plan(DEV_TASK, {
        "roster": [{"role": "product"}, {"role": "developer"}],
        "milestones": [{"key": "m1", "title": "PRD", "role": "product",
                        "depends_on": ["m1", "ghost"]},
                       {"key": "m2", "title": "实现", "role": "developer",
                        "depends_on": ["m1"]}]})
    by_key = {m["key"]: m for m in plan["milestones"]}
    assert by_key["m1"]["depends_on"] == []
    assert by_key["m2"]["depends_on"] == ["m1"]


def test_normalize_plan_里程碑缺失时按价值链补齐():
    filled = normalize_plan(DEV_TASK, {"roster": [{"role": "ops"}, {"role": "qa"}]})
    assert [m["key"] for m in filled["milestones"]] == ["m-qa", "m-ops"]
    assert filled["milestones"][1]["depends_on"] == ["m-qa"]
    assert filled["reason"] == "人工调整编制"          # 没写理由时给默认说明
    assert filled["goal"] == DEV_TASK


@pytest.mark.parametrize("raw", [None, {}, {"roster": []}, {"roster": [{"role": "hacker"}]}],
                         ids=["none", "empty", "no-roster", "unknown-role"])
def test_normalize_plan_拒绝非法计划(raw):
    with pytest.raises(ValueError) as caught:
        normalize_plan(DEV_TASK, raw)
    assert "roster" in str(caught.value)


def test_normalize_plan_岗位去重与里程碑_key_去重():
    plan = normalize_plan(DEV_TASK, {
        "roster": [{"role": "qa"}, {"role": "qa"}, {"role": "hacker"}],
        "milestones": [{"key": "a", "title": "x", "role": "qa"},
                       {"key": "a", "title": "y", "role": "qa"},
                       {"key": "b", "title": "   ", "role": "qa"}]})
    assert len(plan["roster"]) == 1
    assert [m["key"] for m in plan["milestones"]] == ["a", "a-"]   # 空标题里程碑被丢


def test_build_agent_spec_带协作工具与岗位提示词():
    spec = build_agent_spec("qa", DEV_TASK, "关注回归范围")
    assert spec["id"] == "wf-qa" and spec["role"] == "qa"
    assert spec["name"] == ROLE_TEMPLATES["qa"]["name"]
    assert set(TEAM_TOOLS) <= set(spec["tool_ids"])
    assert spec["memory"] is True and spec["max_steps"] == 8
    assert spec["flow_id"] == "" and spec["kb_ids"] == []
    assert "关注回归范围" in spec["system"] and "board_move" in spec["system"]
    assert DEV_TASK[:40] in spec["description"]
    assert build_agent_spec("qa", DEV_TASK, "")["system"].count("按职责推进") == 1
    with pytest.raises(ValueError):
        build_agent_spec("hacker", DEV_TASK, "")


# ============================================================ 2. 共享看板
def _new_card(board, title="卡片", role="product", depends_on=None, priority=0,
              team_id=TEAM):
    return board.add(workspace_id=WS, team_id=team_id, title=title, role=role,
                     detail="细节", depends_on=depends_on, priority=priority,
                     created_by="e-owner")


# 把一张卡合法开到某一列所需的路径
_TO_STATUS_PATH = {
    "backlog": [],
    "doing": ["doing"],
    "review": ["doing", "review"],
    "done": ["doing", "review", "done"],
    "blocked": ["blocked"],
}


@pytest.fixture
def card_board(board):
    return board, _new_card(board)


@pytest.mark.parametrize("to", COLUMNS, ids=list(COLUMNS))
@pytest.mark.parametrize("frm", COLUMNS, ids=list(COLUMNS))
def test_board_穷举全部迁移边(card_board, frm, to):
    board, card = card_board
    for step in _TO_STATUS_PATH[frm]:
        board.move(card["id"], step, "owner", "开到起点")
    assert board.get(card["id"])["status"] == frm, f"起点状态构造失败：{frm}"

    if to == frm:
        assert board.move(card["id"], to, "owner")["status"] == frm, "同列迁移应为空操作"
        return
    if to in TRANSITIONS[frm]:
        moved = board.move(card["id"], to, "owner", "合法迁移")
        assert moved["status"] == to, f"{frm} → {to} 应被允许"
        last = board.history(TEAM, card["id"])[-1]
        assert (last["from"], last["to"]) == (frm, to)
        assert last["reason"] == "合法迁移" and last["actor"] == "owner"
    else:
        with pytest.raises(BoardError) as caught:
            board.move(card["id"], to, "owner")
        assert caught.value.code == "card_transition_denied"
        assert f"{frm} → {to}" in caught.value.message
        assert board.get(card["id"])["status"] == frm, "非法迁移不应改变状态"


def test_transitions_表的每个键都是合法列():
    assert set(TRANSITIONS) == set(COLUMNS)
    for frm, targets in TRANSITIONS.items():
        assert targets <= set(COLUMNS), frm
    assert TRANSITIONS["backlog"] == {"doing", "blocked"}
    assert TRANSITIONS["blocked"] == {"doing", "backlog"}


def test_board_依赖未完成时禁止开工(board):
    up = _new_card(board, "上游")
    down = _new_card(board, "下游", depends_on=[up["id"]])
    with pytest.raises(BoardError) as caught:
        board.move(down["id"], "doing", "owner")
    assert caught.value.code == "card_dependency_blocked"
    assert up["id"] in caught.value.message
    assert board.get(down["id"])["status"] == "backlog"

    for step in _TO_STATUS_PATH["done"]:
        board.move(up["id"], step, "owner")
    assert board.move(down["id"], "doing", "owner")["status"] == "doing"


def test_board_依赖的卡片不存在时永远开不了工(board):
    orphan = _new_card(board, "脏依赖", depends_on=["ghost-card"])
    with pytest.raises(BoardError) as caught:
        board.move(orphan["id"], "doing", "owner")
    assert caught.value.code == "card_dependency_blocked"
    assert "ghost-card" in caught.value.message


def test_board_退回backlog后依赖未完成同样挡住开工(board):
    up = _new_card(board, "上游")
    down = _new_card(board, "下游", depends_on=[up["id"]])
    board.move(up["id"], "doing", "owner")
    board.move(down["id"], "blocked", "owner")
    assert board.move(down["id"], "backlog", "owner")["status"] == "backlog"
    with pytest.raises(BoardError) as caught:
        board.move(down["id"], "doing", "owner")
    assert caught.value.code == "card_dependency_blocked"


def test_board_未知列与不存在的卡片报对应错误码(board):
    card = _new_card(board)
    with pytest.raises(BoardError) as caught:
        board.move(card["id"], "someday", "owner")
    assert caught.value.code == "card_status_invalid"
    with pytest.raises(BoardError) as caught:
        board.move("ghost", "doing", "owner")
    assert caught.value.code == "card_not_found"
    assert board.get("ghost") is None
    with pytest.raises(BoardError) as caught:
        board.update("ghost", "owner", title="x")
    assert caught.value.code == "card_not_found"
    with pytest.raises(BoardError) as caught:
        board.set_output("ghost", "owner", {})
    assert caught.value.code == "card_not_found"


def test_board_建卡与改卡的字段校验(board):
    with pytest.raises(BoardError) as caught:
        board.add(workspace_id=WS, team_id=TEAM, title="  ")
    assert caught.value.code == "card_title_required"
    card = _new_card(board)
    with pytest.raises(BoardError) as caught:
        board.update(card["id"], "owner", depends_on=[card["id"]])
    assert caught.value.code == "card_self_dependency"
    with pytest.raises(BoardError) as caught:
        board.update(card["id"], "owner", depends_on=["ghost"])
    assert caught.value.code == "card_dependency_missing"
    with pytest.raises(BoardError) as caught:
        board.update(card["id"], "owner", title=" ")
    assert caught.value.code == "card_title_required"
    # add 不校验依赖存在性（与 update 不一致），这里钉住当前行为
    loose = board.add(workspace_id=WS, team_id=TEAM, title="脏依赖", depends_on=["ghost"])
    assert loose["depends_on"] == ["ghost"] and loose["status"] == "backlog"
    assert loose["output"] == {} and len(loose["id"]) == 12


def test_board_update_无字段时不改写卡片(board):
    card = _new_card(board)
    assert board.update(card["id"], "owner") == card
    changed = board.update(card["id"], "owner", title="改名", detail="新细节",
                           role="qa", priority=5, depends_on=[])
    assert (changed["title"], changed["detail"], changed["role"]) == ("改名", "新细节", "qa")
    assert changed["priority"] == 5 and changed["depends_on"] == []
    assert changed["status"] == "backlog"


def test_board_标题会被裁剪到两百字(board):
    long_title = "长" * 300
    card = board.add(workspace_id=WS, team_id=TEAM, title=long_title)
    assert card["title"] == "长" * 200


def test_board_评论是追加式且空评论被拒(board):
    card = _new_card(board)
    board.comment(card["id"], "e1", "第一条结论")
    board.comment(card["id"], "e2", "第二条结论")
    notes = board.get(card["id"])["output"]["notes"]
    assert [n["text"] for n in notes] == ["第一条结论", "第二条结论"]
    assert [n["actor"] for n in notes] == ["e1", "e2"]
    assert all(n["at"] for n in notes)
    with pytest.raises(BoardError) as caught:
        board.comment(card["id"], "e1", "   ")
    assert caught.value.code == "card_comment_empty"
    assert board.get(card["id"])["output"]["notes"] == notes, "被拒评论不应入列"


def test_board_迁移历史只追加且可按卡片过滤(board):
    card = _new_card(board, "有历史的卡")
    _new_card(board, "别的卡")
    opening = board.history(TEAM)
    assert len(opening) == 2 and opening[0]["card_id"] == card["id"]
    assert opening[0]["from"] == "" and opening[0]["to"] == "backlog"
    assert opening[0]["reason"] == "建卡" and opening[0]["actor"] == "e-owner"

    board.move(card["id"], "doing", "e1", "开工")
    board.move(card["id"], "review", "e1", "交付")
    mine = board.history(TEAM, card["id"])
    assert [h["to"] for h in mine] == ["backlog", "doing", "review"]
    assert [h["seq"] for h in mine] == sorted(h["seq"] for h in mine), "seq 应单调递增"
    assert mine[-1]["reason"] == "交付"
    assert len(board.history(TEAM)) == len(mine) + 1
    assert board.history(TEAM, card["id"], limit=2) == mine[-2:]
    # 评论不写迁移历史
    before = len(board.history(TEAM))
    board.comment(card["id"], "e1", "结论")
    assert len(board.history(TEAM)) == before


def test_board_delete_同时清理迁移历史(board):
    card = _new_card(board)
    assert board.delete(card["id"]) is True
    assert board.get(card["id"]) is None
    assert board.history(TEAM, card["id"]) == []
    assert board.delete(card["id"]) is False


def test_board_output_合并写入并记录最后写入人(board):
    card = _new_card(board)
    board.set_output(card["id"], "e1", {"deliverable": "初版"})
    board.set_output(card["id"], "e2", {"score": 9})
    out = board.get(card["id"])["output"]
    assert out["deliverable"] == "初版" and out["score"] == 9
    assert out["updated_by"] == "e2" and out["updated_at"]


def test_board_progress_形状(board):
    assert board.progress("no-such-team") == {
        "total": 0, "by_status": {col: 0 for col in COLUMNS},
        "done_ratio": 0.0, "roles_open": []}

    a = _new_card(board, "A", role="product")
    b = _new_card(board, "B", role="qa")
    for step in _TO_STATUS_PATH["done"]:
        board.move(a["id"], step, "owner")
    board.move(b["id"], "doing", "owner")
    board.move(b["id"], "blocked", "owner")
    progress = board.progress(TEAM)
    assert progress["total"] == 2
    assert progress["by_status"] == {"backlog": 0, "doing": 0, "review": 0,
                                     "done": 1, "blocked": 1}
    assert progress["done_ratio"] == 0.5
    assert progress["roles_open"] == ["qa"]
    assert board.progress(OTHER_TEAM)["total"] == 0


def test_board_list_按状态岗位过滤并体现优先级(board):
    _new_card(board, "低优先", role="qa", priority=1)
    _new_card(board, "高优先", role="qa", priority=9)
    _new_card(board, "别的岗位", role="ops", priority=5, team_id=OTHER_TEAM)
    qa_cards = board.list(TEAM, role="qa")
    assert [c["title"] for c in qa_cards] == ["高优先", "低优先"]
    assert [c["id"] for c in board.list(TEAM, status="backlog")] == [c["id"] for c in qa_cards]
    assert [c["title"] for c in board.list(OTHER_TEAM)] == ["别的岗位"]
    assert board.list(TEAM, status="done") == []
    assert board.list(TEAM, role="ops") == []


def test_board_事件会推给共享_hub(board, hub):
    event = hub.subscribe()
    card = _new_card(board)
    created = event.get_nowait()
    assert created["type"] == "card" and created["payload"]["action"] == "created"
    assert created["workspace_id"] == WS and created["team_id"] == TEAM
    board.move(card["id"], "doing", "owner", "开工")
    moved = event.get_nowait()
    assert moved["payload"]["action"] == "moved"
    assert moved["payload"]["from"] == "backlog" and moved["payload"]["to"] == "doing"
    assert moved["payload"]["reason"] == "开工"
    assert moved["payload"]["card"]["status"] == "doing"
    assert moved["id"] == card["id"]
    board.comment(card["id"], "owner", "结论")
    assert event.get_nowait()["payload"]["action"] == "comment"


# ============================================================ 3. 消息总线
def test_bus_消息落库并广播到_hub(bus, hub):
    event = hub.subscribe()
    message = _post(bus, body={"text": "内容"})
    assert message["seq"] >= 1 and message["thread_id"] is None
    assert message["workspace_id"] == WS and message["team_id"] == TEAM
    assert message["kind"] == "request" and message["body"] == {"text": "内容"}
    assert message["created_at"] and message["round_no"] == 0
    pushed = event.get_nowait()
    assert pushed["type"] == "message" and pushed["id"] == message["seq"]
    assert pushed["workspace_id"] == WS and pushed["payload"] == message
    assert bus.get(message["seq"]) == message and bus.get(9999) is None


def test_bus_未知类型与缺失字段会拒绝(bus):
    with pytest.raises(ValueError) as caught:
        _post(bus, kind="carrier-pigeon")
    assert "未知消息类型" in str(caught.value)
    with pytest.raises(ValueError) as caught:
        _post(bus, from_actor="  ")
    assert "发件人" in str(caught.value)
    with pytest.raises(ValueError) as caught:
        _post(bus, subject="   ")
    assert "主题" in str(caught.value)
    assert set(MESSAGE_KINDS) == {"assign", "request", "response", "review",
                                  "deliver", "announce", "human"}
    assert bus.list(TEAM) == []


def test_bus_非字典消息体会包成_text(bus):
    assert _post(bus, body="纯文本")["body"] == {"text": "纯文本"}
    assert _post(bus, body=None)["body"] == {"text": ""}
    nested = _post(bus, body={"progress": {"done_ratio": 1.0}})
    assert nested["body"] == {"progress": {"done_ratio": 1.0}}


def test_bus_主题会被裁剪到两百字(bus):
    assert len(_post(bus, subject="主" * 300)["subject"]) == 200


def test_bus_回复会归并到同一线程(bus):
    root = _post(bus, kind="deliver", subject="架构方案")
    reply = _post(bus, from_actor="qa", to_actor="product", kind="response",
                  subject="收到", parent_seq=root["seq"])
    again = _post(bus, from_actor="ops", to_actor="qa", kind="review",
                  subject="再确认", parent_seq=reply["seq"])
    assert root["thread_id"] is None and root["parent_seq"] is None
    assert reply["thread_id"] == root["seq"] and again["thread_id"] == root["seq"]
    assert reply["parent_seq"] == root["seq"] and again["parent_seq"] == reply["seq"]
    assert [m["seq"] for m in bus.thread(root["seq"])] == [
        root["seq"], reply["seq"], again["seq"]]
    assert [m["subject"] for m in bus.thread(root["seq"])] == ["架构方案", "收到", "再确认"]
    assert bus.thread(again["seq"]) == [again], "线程号不是根消息时只命中自己"


def test_bus_回复不存在或别团队的线程会被拒(bus):
    elsewhere = _post(bus, team_id=OTHER_TEAM)
    with pytest.raises(ValueError) as caught:
        _post(bus, parent_seq=9999)
    assert "不存在" in str(caught.value)
    with pytest.raises(ValueError) as caught:
        _post(bus, parent_seq=elsewhere["seq"])
    assert "其他团队" in str(caught.value)


def test_bus_未读计数与已读标记(bus):
    direct = _post(bus, subject="私信")
    _post(bus, from_actor="qa", to_actor="product", subject="不该出现在收件箱")
    assert bus.unread(TEAM, "qa") == 1
    bus.mark_read(direct["seq"], "qa")
    assert bus.unread(TEAM, "qa") == 0
    bus.mark_read(direct["seq"], "qa")                 # 重复标记幂等
    assert bus.unread(TEAM, "qa") == 0

    _post(bus, from_actor="ops", subject="全员公告", to_actor=BROADCAST)
    assert bus.unread(TEAM, "qa") == 1
    assert [m["subject"] for m in bus.inbox(TEAM, "qa", unread_only=True)] == ["全员公告"]
    assert [m["subject"] for m in bus.inbox(TEAM, "qa")] == ["私信", "全员公告"]
    assert bus.unread(TEAM, "ops") == 0                # 自己发的广播不算未读


def test_bus_广播对所有员工可见且排除自己(bus):
    mine = _post(bus, from_actor="qa", to_actor=BROADCAST, subject="qa 广播")
    others = _post(bus, from_actor="ops", to_actor=BROADCAST, subject="ops 广播")
    assert [m["seq"] for m in bus.inbox(TEAM, "qa")] == [others["seq"]]
    assert [m["seq"] for m in bus.inbox(TEAM, "product")] == [mine["seq"], others["seq"]]
    assert bus.unread(TEAM, "qa") == 1 and bus.unread(TEAM, "product") == 2


def test_bus_to_actor留空时默认广播(bus):
    assert _post(bus, to_actor="")["to_actor"] == BROADCAST


def test_bus_list_过滤与分页(bus):
    first = _post(bus, to_actor="architect", kind="assign", subject="1")
    _post(bus, to_actor="architect", kind="request", subject="2")
    _post(bus, from_actor="qa", to_actor="product", kind="assign", subject="3")
    assert [m["subject"] for m in bus.list(TEAM)] == ["1", "2", "3"]
    assert [m["subject"] for m in bus.list(TEAM, kind="assign")] == ["1", "3"]
    assert [m["subject"] for m in bus.list(TEAM, after_seq=first["seq"])] == ["2", "3"]
    assert [m["subject"] for m in bus.list(TEAM, actor="qa")] == ["3"]
    assert [m["subject"] for m in bus.list(TEAM, limit=2)] == ["2", "3"]
    assert bus.list(OTHER_TEAM) == []
    assert bus.unread(TEAM, "architect") == 2


def test_bus_pairs_给出组织图连线权重(bus):
    _post(bus, from_actor="product", to_actor="architect")
    _post(bus, from_actor="product", to_actor="architect", subject="第二次")
    _post(bus, from_actor="architect", to_actor="developer")
    pairs = bus.pairs(TEAM)
    assert pairs[0] == {"from": "product", "to": "architect", "count": 2}
    assert {"from": "architect", "to": "developer", "count": 1} in pairs
    assert bus.pairs(OTHER_TEAM) == []


def test_bus_stats_统计总数与分类(bus):
    _post(bus, kind="deliver")
    _post(bus, kind="deliver", subject="又一条")
    _post(bus, kind="human", from_actor=HUMAN_ACTOR)
    assert bus.stats(TEAM) == {"total": 3, "by_kind": {"deliver": 2, "human": 1}}
    assert bus.stats(OTHER_TEAM) == {"total": 0, "by_kind": {}}


def test_bus_recent_是sse重放的原语并按游标升序(bus):
    a1 = _post(bus, subject="A1")
    a2 = _post(bus, subject="A2", round_no=3)
    assert [m["seq"] for m in bus.recent(workspace_id=WS)] == [a1["seq"], a2["seq"]]
    assert [m["seq"] for m in bus.recent(workspace_id=WS, after_seq=a1["seq"])] == [a2["seq"]]
    assert bus.recent(workspace_id=WS, after_seq=a2["seq"]) == []
    assert [m["seq"] for m in bus.recent(workspace_id=WS, limit=1)] == [a1["seq"]]
    assert a2["round_no"] == 3


def test_bus_按_workspace_隔离重放(bus):
    mine = _post(bus, subject="本工作区")
    _post(bus, subject="别的区", workspace_id=WS_B)
    _post(bus, subject="别的区2", workspace_id=WS_B, from_actor="ops")
    assert [m["seq"] for m in bus.recent(workspace_id=WS)] == [mine["seq"]]
    assert len(bus.recent(workspace_id=WS_B)) == 2
    assert bus.recent(workspace_id="ghost-ws") == []
    assert bus.stats(TEAM)["total"] == 3, "stats 只按 team_id 聚合，跨工作区需要隔离 team_id"


def test_bus_重新打开同一个sqlite文件后消息仍在(tmp_path, hub):
    path = tmp_path / "reopen.sqlite"
    first = MessageBus(path, hub=hub)
    seq = _post(first, subject="持久化")["seq"]
    second = MessageBus(path, hub=hub)
    assert [m["seq"] for m in second.list(TEAM)] == [seq]
    assert second.get(seq)["subject"] == "持久化"


def test_hub_队列满时挤掉最旧事件并把_overflow_信号送出去():
    """慢客户端不能静默丢事件：队列满时先腾出一个位置，再投 overflow，
    SSE 端才会收到「回退到快照刷新」的提示（bus.py:48-58）。"""
    from flow_studio.bus import SUBSCRIBER_QUEUE

    hub = Hub()
    sub = hub.subscribe()
    for i in range(SUBSCRIBER_QUEUE):
        hub.publish({"type": "message", "id": i})
    hub.publish({"type": "message", "id": "overflowed"})

    assert sub.qsize() == SUBSCRIBER_QUEUE, "队列不会超长"
    events = [sub.get_nowait() for _ in range(SUBSCRIBER_QUEUE)]
    assert events[-1] == {"type": "overflow", "reason": "subscriber_lagged"}
    assert [e["id"] for e in events[:-1]] == list(range(1, SUBSCRIBER_QUEUE)), \
        "丢的应是最旧事件，不是刚发的"

    hub.unsubscribe(sub)
    hub.publish({"type": "message", "id": "after"})   # 退订后不再投递，也不抛异常
    assert sub.empty()
    assert hub._subs == []


# ================================================ 4. 编制存储 WorkforceStore
def test_store_团队读写与工作区隔离(teams):
    team = _make_team(teams)
    assert team["status"] == "draft" and team["round_no"] == 0
    assert team["max_rounds"] == 12 and team["created_by"] == "owner"
    assert team["summary"] == "" and team["error"] is None
    assert team["started_at"] is None and team["finished_at"] is None
    assert team["plan"]["roster"][0]["role"] == "product"
    assert teams.get_team("ghost") is None
    assert teams.get_team(team["id"])["task"] == DEV_TASK

    _make_team(teams, task=SALES_TASK, plan=compose(SALES_TASK), workspace_id=WS_B)
    assert [t["id"] for t in teams.list_teams(WS)] == [team["id"]]
    assert len(teams.list_teams(WS_B)) == 1
    teams.set_task(team["id"], "换目标")
    assert teams.get_team(team["id"])["task"] == "换目标"
    assert teams.set_team_plan(team["id"], {"roster": []})["plan"] == {"roster": []}


def test_store_团队状态机与终态字段(teams):
    tid = _make_team(teams)["id"]
    with pytest.raises(ValueError) as caught:
        teams.set_team_status(tid, "paused")
    assert "不允许的团队状态迁移" in str(caught.value)
    assert teams.set_team_status(tid, "draft")["status"] == "draft"   # 同态空操作

    assert teams.set_team_status(tid, "running")["started_at"], "首次 running 记开始时间"
    assert teams.set_team_status(tid, "waiting_human")["status"] == "waiting_human"
    teams.set_team_status(tid, "running")
    done = teams.set_team_status(tid, "done", summary="交付完成")
    assert done["finished_at"] and done["summary"] == "交付完成"
    teams.set_team_status(tid, "running")
    assert teams.set_team_status(tid, "failed", error="炸了")["error"] == "炸了"
    assert teams.set_team_status(tid, "stopped")["status"] == "stopped"
    with pytest.raises(ValueError):
        teams.set_team_status(tid, "running")            # stopped 是终态
    with pytest.raises(ValueError):
        teams.set_team_status("ghost", "running")
    assert set(TEAM_STATUSES) == set(TEAM_TRANSITIONS)
    assert TEAM_TRANSITIONS["stopped"] == set()


def test_store_轮次记录与可重启清单(teams):
    tid = _make_team(teams)["id"]
    assert teams.bump_round(tid) == 1 and teams.bump_round(tid) == 2
    assert teams.get_team(tid)["round_no"] == 2
    seq = teams.begin_round(tid, 2)
    teams.end_round(seq, status="partial", detail={"messages": 3})
    rounds = teams.rounds(tid)
    assert len(rounds) == 1 and rounds[0]["round_no"] == 2
    assert rounds[0]["status"] == "partial" and rounds[0]["detail"] == {"messages": 3}
    assert rounds[0]["started_at"] and rounds[0]["finished_at"] and rounds[0]["ms"] >= 0
    assert teams.restartable() == []
    teams.set_team_status(tid, "running")
    assert teams.restartable() == [tid] and teams.restartable(WS_B) == []
    teams.set_team_status(tid, "paused")
    assert teams.restartable() == []


def test_store_max_rounds_夹紧与团队计数(teams):
    tid = _make_team(teams)["id"]
    teams.set_max_rounds(tid, 999)
    assert teams.get_team(tid)["max_rounds"] == 50
    teams.set_max_rounds(tid, 0)
    assert teams.get_team(tid)["max_rounds"] == 1
    assert teams.count_teams(WS) == 1 and teams.count_teams(WS_B) == 0
    assert teams.count_teams(WS, statuses=("draft",)) == 1
    assert teams.count_teams(WS, statuses=("running",)) == 0


def test_store_员工读写与收件人解析(teams):
    tid = _make_team(teams)["id"]
    emp = teams.add_employee(team_id=tid, role="qa", name="测试", agent_id="wf-qa",
                             focus="回归范围")
    assert emp["status"] == "idle" and emp["turns"] == 0 and emp["produced"] == {}
    assert emp["id"].startswith("e") and emp["focus"] == "回归范围"
    assert teams.get_employee("ghost") is None
    assert teams.get_employee(emp["id"]) == emp
    assert [e["id"] for e in teams.employees(tid)] == [emp["id"]]
    assert teams.employees(OTHER_TEAM) == []

    for token in (emp["id"], "qa", "测试", "wf-qa", "试"):
        assert teams.find_employee(tid, token)["id"] == emp["id"], token
    assert teams.find_employee(tid, "") is None
    assert teams.find_employee(tid, "根本没有人") is None

    updated = teams.set_employee(emp["id"], status="working", turns=2, messages_sent=5,
                                 produced={"mode": "fallback"})
    assert (updated["status"], updated["turns"], updated["messages_sent"]) == (
        "working", 2, 5)
    assert updated["last_active_at"]
    merged = teams.set_employee(emp["id"], produced={"last_summary": "小结"})
    assert merged["produced"] == {"mode": "fallback", "last_summary": "小结"}
    assert set(EMPLOYEE_STATUSES) == {"idle", "working", "blocked", "done"}
    with pytest.raises(ValueError):
        teams.set_employee(emp["id"], status="sleeping")
    with pytest.raises(ValueError):
        teams.set_employee("ghost", status="idle")


# ========================================================== 5. 团队驱动器
def test_crew_provision_建员工建卡发开工指令且幂等(driver, teams, board, bus):
    tid = _make_team(teams)["id"]
    out = driver.provision(tid, created_by="owner")
    assert out == {"employees": 6, "cards": 7}
    employees = teams.employees(tid)
    assert [e["role"] for e in employees] == list(CHAIN_ORDER), "按价值链顺序装配"
    assert [e["agent_id"] for e in employees] == [f"wf-{r}" for r in CHAIN_ORDER]
    assert all(e["focus"] for e in employees)

    cards = board.list(tid)
    assert len(cards) == 7 and {c["status"] for c in cards} == {"backlog"}
    assert {c["role"] for c in cards} == set(CHAIN_ORDER)
    assert all(c["created_by"].startswith("e") for c in cards)
    by_role = {c["role"]: c for c in cards if c["role"] != "product" or not c["depends_on"]}
    assert by_role["architect"]["depends_on"] == [by_role["product"]["id"]]
    assert by_role["product"]["depends_on"] == []

    assert bus.stats(tid)["by_kind"] == {"assign": 6}
    for emp in employees:
        assert bus.unread(tid, emp["id"]) == 1
        inbox = bus.inbox(tid, emp["id"])
        assert len(inbox) == 1 and inbox[0]["subject"].startswith("开工")

    again = driver.provision(tid)
    assert again["reused"] is True and again["employees"] == 6
    assert len(board.list(tid)) == 7 and bus.stats(tid)["total"] == 6
    assert len(teams.employees(tid)) == 6


def test_crew_provision_团队不存在时报错(driver):
    with pytest.raises(CrewError) as caught:
        driver.provision("ghost")
    assert caught.value.code == "team_not_found"


def test_crew_step_单轮推进卡片且每个上场员工都发言(driver, teams, board, bus):
    tid = _make_team(teams)["id"]
    driver.provision(tid)
    opening = {emp["id"]: next(m["seq"] for m in bus.inbox(tid, emp["id"])
                               if m["subject"].startswith("开工"))
               for emp in teams.employees(tid)}
    result = driver.step(tid, actor="owner")
    assert result["ok"] is True and result["status"] == "paused", "step 在轮次边界收回"
    detail = result["round"]
    assert detail["round_no"] == 1 and detail["errors"] == [] and not detail["terminal"]
    assert set(detail["participants"]) == set(CHAIN_ORDER)
    assert detail["skipped"] == []
    assert detail["messages"] >= 6 and detail["moves"] >= 1

    senders = {m["from_actor"] for m in bus.list(tid, limit=500)}
    for emp in teams.employees(tid):
        assert emp["id"] in senders, f"{emp['role']} 本轮没有发言"
        still_unread = {m["seq"] for m in
                        bus.inbox(tid, emp["id"], unread_only=True, limit=200)}
        assert opening[emp["id"]] not in still_unread, "上场时读过的开工指令应标记已读"
        # 本轮晚于自己上场的同事消息仍是未读：_act 读的是上场那一刻的收件箱
    assert teams.get_team(tid)["round_no"] == 1

    product = next(e for e in teams.employees(tid) if e["role"] == "product")
    assert product["turns"] == 1 and product["status"] == "idle"
    assert product["messages_sent"] >= 1
    assert product["produced"]["mode"] == "fallback", "无 LLM 必须记录降级模式"
    assert product["produced"]["last_summary"] and product["produced"]["last_at"]

    progress = board.progress(tid)
    assert progress["total"] == 7
    # 链条串行：第一轮只有产品经理的卡片被架构师验收，其余岗位还在等上游
    assert progress["by_status"] == {"backlog": 6, "doing": 0, "review": 0,
                                     "done": 1, "blocked": 0}
    assert progress["done_ratio"] == 0.143
    assert set(progress["roles_open"]) == set(CHAIN_ORDER)
    rounds = teams.rounds(tid)
    assert len(rounds) == 1 and rounds[0]["status"] == "done"
    assert rounds[0]["detail"]["participants"] == detail["participants"]
    # 交付物写进了卡片，并且给下游岗位留了可回复的消息
    delivered = [m for m in bus.list(tid, limit=500) if m["kind"] == "deliver"]
    assert len(delivered) == 6
    assert [m["body"]["from_role"] for m in delivered] == list(CHAIN_ORDER)
    assert delivered[0]["subject"] == "产品经理交付完成"


def test_crew_step_重复调用累加轮次(driver, teams):
    tid = _make_team(teams)["id"]
    driver.step(tid)
    second = driver.step(tid)
    assert second["round"]["round_no"] == 2
    assert teams.get_team(tid)["round_no"] == 2
    assert len(teams.rounds(tid)) == 2


def test_crew_收敛到done并写出交付总结(driver, teams, board, bus):
    tid = _make_team(teams)["id"]
    status = ""
    for _ in range(14):
        status = driver.step(tid, actor="owner")["status"]
        if status == "done":
            break
    assert status == "done", f"链路未能收敛：{board.progress(tid)}"
    team = teams.get_team(tid)
    assert team["round_no"] >= 3 and team["finished_at"]
    assert team["summary"] and "已交付" in team["summary"]
    assert "全部完成" in team["summary"]

    progress = board.progress(tid)
    assert progress["done_ratio"] == 1.0 and progress["roles_open"] == []
    assert bus.stats(tid)["total"] > 20
    closing = [m for m in bus.list(tid, limit=500)
               if m["from_actor"] == "crew" and m["to_actor"] == HUMAN_ACTOR]
    assert closing and closing[-1]["subject"] == "任务交付完成"
    assert closing[-1]["body"]["progress"]["done_ratio"] == 1.0
    for card in board.list(tid):
        assert card["output"].get("deliverable"), card["title"]
    assert all(e["produced"]["mode"] == "fallback" for e in teams.employees(tid))
    assert all(e["status"] == "idle" for e in teams.employees(tid))


def test_crew_收敛后继续step仍是终态(driver, teams, board):
    plan = normalize_plan(DEV_TASK, {
        "roster": [{"role": "product"}, {"role": "developer"}],
        "milestones": [{"key": "m1", "title": "PRD", "role": "product"},
                       {"key": "m2", "title": "实现", "role": "developer",
                        "depends_on": ["m1"]}]})
    tid = _make_team(teams, plan=plan)["id"]
    for _ in range(10):
        if driver.step(tid)["status"] == "done":
            break
    assert board.progress(tid)["done_ratio"] == 1.0
    rounds_before = len(teams.rounds(tid))
    after = driver.step(tid)
    assert after["status"] == "done", "done 之后继续 step 不应退回 paused"
    assert len(teams.rounds(tid)) == rounds_before + 1


def test_crew_停滞两轮后转为等待人类(driver, teams, board, bus):
    tid = _make_team(teams)["id"]
    driver.provision(tid)
    for card in board.list(tid):
        board.delete(card["id"])
    for emp in teams.employees(tid):
        for message in bus.list(tid, limit=500):
            bus.mark_read(message["seq"], emp["id"])

    silent = _SilentCrew(teams=teams, board=board, bus=bus, runtime=_FakeRuntime())
    silent.start(tid, actor="owner")
    assert _wait_until(lambda: not silent.running(tid), timeout=20), "驱动线程未退出"
    team = teams.get_team(tid)
    assert team["status"] == "waiting_human", team["status"]
    assert team["round_no"] == 2, "应在连续两轮无进展后停下"
    asks = [m for m in bus.list(tid, limit=500)
            if m["to_actor"] == HUMAN_ACTOR and m["kind"] == "request"]
    assert asks and "停滞" in asks[-1]["subject"]
    assert "连续 2 轮" in asks[-1]["body"]["text"]


def test_crew_轮次上限触发暂停并向老板求助(driver, teams, board, bus):
    tid = _make_team(teams, task=SALES_TASK, plan=compose(SALES_TASK),
                     max_rounds=1)["id"]
    driver.start(tid, actor="owner")
    assert _wait_until(lambda: not driver.running(tid), timeout=20), "驱动线程未退出"
    team = teams.get_team(tid)
    assert team["status"] == "paused" and team["round_no"] == 1
    assert board.progress(tid)["done_ratio"] < 1.0, "未收敛才该被轮次上限截住"
    asks = [m for m in bus.list(tid, limit=500)
            if m["from_actor"] == "crew" and m["to_actor"] == HUMAN_ACTOR]
    assert asks, "轮次上限应当向人类发一条求助消息"
    assert "轮次上限" in asks[-1]["subject"]
    assert asks[-1]["kind"] == "request"
    assert "max_rounds" in asks[-1]["body"]["text"]


def test_crew_pause_在轮次边界生效(teams, board, bus):
    slow = _SlowCrew(teams=teams, board=board, bus=bus, runtime=_FakeRuntime())
    tid = _make_team(teams)["id"]
    slow.start(tid, actor="owner")
    assert _wait_until(lambda: teams.get_team(tid)["round_no"] >= 1, timeout=20)
    signal = slow.pause(tid, actor="owner")
    assert signal["ok"] is True and signal["signalled"] is True
    assert signal["status"] == "running", "运行中的 pause 只登记信号"
    assert _wait_until(lambda: not slow.running(tid), timeout=20)
    team = teams.get_team(tid)
    assert team["status"] == "paused" and team["round_no"] == 1, "只允许跑完整的一轮"
    assert bus.stats(tid)["by_kind"].get("announce", 0) >= 6, "轮次内应有心跳"

    assert slow.start(tid, actor="owner")["status"] == "running"
    assert _wait_until(lambda: teams.get_team(tid)["round_no"] >= 2, timeout=20)
    slow.pause(tid)
    assert _wait_until(lambda: teams.get_team(tid)["status"] == "paused", timeout=20)


def test_crew_stop_信号与草稿团队直接落状态(driver, teams):
    tid = _make_team(teams)["id"]
    stopped = driver.stop(tid, actor="owner")        # 草稿团队没有线程，直接落状态
    assert stopped["status"] == "stopped" and stopped["signalled"] is False
    assert driver.running(tid) is False
    with pytest.raises(CrewError) as caught:
        driver.step(tid)
    assert caught.value.code == "team_status_conflict", "stopped 之后不能继续跑轮次"
    assert teams.get_team(tid)["status"] == "stopped"


def test_crew_运行中重复start会被拒绝(teams, board, bus):
    slow = _SlowCrew(teams=teams, board=board, bus=bus, runtime=_FakeRuntime())
    tid = _make_team(teams)["id"]
    slow.start(tid)
    assert slow.running(tid) is True
    with pytest.raises(CrewError) as caught:
        slow.start(tid)
    assert caught.value.code == "crew_already_running"
    slow.stop(tid)
    assert _wait_until(lambda: not slow.running(tid), timeout=20)
    assert teams.get_team(tid)["status"] == "stopped"


def test_crew_员工报错只阻塞本人并向人类求助(teams, board, bus):
    broken = _BrokenCrew(teams=teams, board=board, bus=bus, runtime=_FakeRuntime())
    tid = _make_team(teams)["id"]
    result = broken.step(tid, actor="owner")
    detail = result["round"]
    assert len(detail["errors"]) == 6
    assert {e["role"] for e in detail["errors"]} == set(CHAIN_ORDER)
    assert all("模型连接失败" in e["error"] for e in detail["errors"])
    for emp in teams.employees(tid):
        assert emp["status"] == "blocked", emp["role"]
        assert emp["produced"]["mode"] == "react", "出错轮次也要记录 mode"
        asks = [m for m in bus.list(tid, limit=500)
                if m["from_actor"] == emp["id"] and m["kind"] == "request"
                and m["to_actor"] == HUMAN_ACTOR]
        assert asks and "执行受阻" in asks[-1]["subject"]
        assert "模型连接失败" in asks[-1]["body"]["text"]
    assert board.progress(tid)["by_status"]["backlog"] == 7, "失败的轮次不该动卡片"
    assert teams.rounds(tid)[0]["status"] == "partial"
    assert result["status"] == "paused", "单个员工失败不拖垮团队"


def test_crew_degrade_interrupted_降级重启残留的running(driver, teams):
    tid = _make_team(teams)["id"]
    other = _make_team(teams, task=SALES_TASK, plan=compose(SALES_TASK))["id"]
    teams.set_team_status(tid, "running")
    assert driver.degrade_interrupted(WS_B) == []
    assert driver.degrade_interrupted(WS) == [tid]
    team = teams.get_team(tid)
    assert team["status"] == "paused" and team["error"] == "进程重启，驱动线程已中断"
    assert team["plan"]["restart"] is True
    assert teams.get_team(other)["status"] == "draft"


def test_crew_审计写入带团队资源坐标(teams, board, bus, governance_stub):
    crew = CrewDriver(teams=teams, board=board, bus=bus, runtime=_FakeRuntime(),
                      governance=governance_stub)
    tid = _make_team(teams)["id"]
    crew.step(tid, actor="owner")
    types = {row[0] for row in governance_stub.rows}
    for event in ("workforce.team.provision", "workforce.card.move",
                  "workforce.message.post", "workforce.team.step"):
        assert event in types, sorted(types)
    for event_type, kwargs, username, details in governance_stub.rows:
        assert kwargs["resource_type"] == "workforce", event_type
        assert kwargs["resource_id"] == tid, event_type
        assert kwargs["workspace_id"] == WS, event_type
        assert kwargs["outcome"] == "success", event_type
        assert username, event_type

    step_rows = governance_stub.of("workforce.team.step")
    assert len(step_rows) == 1 and step_rows[0][2] == "owner"
    assert step_rows[0][3]["round_no"] == 1 and step_rows[0][3]["participants"]
    provision = governance_stub.of("workforce.team.provision")[0]
    assert provision[2] == "owner"          # step(actor=owner) 触发的 provision
    assert provision[3] == {"employees": 6, "cards": 7}
    employee_ids = {e["id"] for e in teams.employees(tid)}
    posts = governance_stub.of("workforce.message.post")
    assert posts and {row[2] for row in posts} <= employee_ids, "发言审计落在员工名下"
    assert all({"kind", "to", "seq"} <= set(row[3]) for row in posts)
    moves = governance_stub.of("workforce.card.move")
    assert moves and all({"card_id", "to", "reason"} <= set(row[3]) for row in moves)
    assert governance_stub.of("workforce.team.start") == [], "step 不该写 start 审计"


class _StubGovernance:
    """记录 governance.audit(event_type, workspace_id=..., ...) 的调用。"""

    def __init__(self):
        self.rows: list[tuple] = []

    def audit(self, event_type, **kwargs):
        self.rows.append((event_type, kwargs, kwargs.get("username"),
                          kwargs.get("details")))

    def of(self, event_type):
        return [row for row in self.rows if row[0] == event_type]


@pytest.fixture
def governance_stub():
    return _StubGovernance()


# ================================================ 6. 团队作用域协作工具
def _tools(driver):
    reg = ToolRegistry()
    driver._register_team_tools(reg)
    return reg


def _actor(employee, team_id, round_no=1):
    return Actor(workspace_id=WS, team_id=team_id, employee_id=employee["id"],
                 role=employee["role"], name=employee["name"], round_no=round_no)


@pytest.fixture
def provisioned(driver, teams):
    tid = _make_team(teams)["id"]
    driver.provision(tid)
    return driver, tid, teams.employees(tid)[0]


def test_tools_注册全套协作工具(provisioned):
    driver, tid, _ = provisioned
    reg = _tools(driver)
    assert set(TEAM_TOOLS) <= set(reg.names())
    assert {item["function"]["name"] for item in reg.manifest()} >= set(TEAM_TOOLS)
    required = reg.get("bus_post").parameters.get("required") or []
    assert set(required) == {"to", "subject", "text"}


def test_tools_轮次外调用会被拒绝(provisioned):
    driver, _tid, _ = provisioned
    reg = _tools(driver)
    with pytest.raises(RuntimeError) as caught:
        reg.call("board_list", {})
    assert "团队轮次内" in str(caught.value)


def test_tools_board_list_与_bus_read_限定在团队作用域(provisioned, board):
    driver, tid, employee = provisioned
    reg = _tools(driver)
    driver._tls.actor = _actor(employee, tid)
    try:
        listed = reg.call("board_list", {})
        assert len(listed["cards"]) == 7 and listed["progress"]["total"] == 7
        assert {"id", "title", "role", "status", "depends_on", "output"} <= set(
            listed["cards"][0])
        assert {c["role"] for c in reg.call("board_list", {"role": "qa"})["cards"]} == {"qa"}
        read = reg.call("bus_read", {"limit": 3})
        assert [m["kind"] for m in read["messages"]] == ["assign"]
        assert read["messages"][0]["to_actor"] == employee["id"]
        # 默认只看未读：标记已读后收件箱就空了
        bus_read_all = reg.call("bus_read", {"limit": 3, "unread_only": False})
        assert len(bus_read_all["messages"]) == 1
        driver.bus.mark_read(read["messages"][0]["seq"], employee["id"])
        assert reg.call("bus_read", {"limit": 3})["messages"] == []
        assert reg.call("bus_read", {"limit": 3, "unread_only": False})["messages"]
    finally:
        driver._tls.actor = None


def test_tools_bus_post_解析岗位员工与特殊收件人(provisioned, bus):
    driver, tid, employee = provisioned
    reg = _tools(driver)
    actor = _actor(employee, tid, round_no=2)
    driver._tls.actor = actor
    try:
        architect = next(e for e in driver.teams.employees(tid) if e["role"] == "architect")
        by_role = reg.call("bus_post", {"to": "architect", "kind": "request",
                                        "subject": "方案", "text": "看一下"})
        assert by_role["to"] == {"to": architect["id"], "role": "architect"}
        stored = bus.get(by_role["seq"])
        assert stored["to_actor"] == architect["id"] and stored["round_no"] == 2
        assert stored["body"]["from_role"] == "product"
        assert reg.call("bus_post", {"to": "human", "subject": "求助"})["to"] == \
            {"to": HUMAN_ACTOR}
        assert reg.call("bus_post", {"to": "*", "subject": "公告"})["to"] == \
            {"to": BROADCAST}
        assert reg.call("bus_post", {"to": employee["id"], "subject": "给自己"})["to"] == \
            {"to": employee["id"], "role": "product"}
        # kind 非法时退回 response，subject 缺省时给默认文案
        loose = reg.call("bus_post", {"to": "*", "kind": "carrier-pigeon"})
        assert bus.get(loose["seq"])["kind"] == "response"
        assert bus.get(loose["seq"])["subject"] == "来自同事的消息"
        assert actor.posted == 5

        replied = reg.call("bus_post", {"to": "*", "subject": "回复", "text": "y",
                                        "parent_seq": by_role["seq"]})
        assert bus.get(replied["seq"])["thread_id"] == by_role["seq"]
    finally:
        driver._tls.actor = None


def test_tools_bus_post_未知收件人报错(teams, board, bus):
    driver = CrewDriver(teams=teams, board=board, bus=bus, runtime=_FakeRuntime())
    tid = _make_team(teams, task=SALES_TASK, plan=compose(SALES_TASK))["id"]
    driver.provision(tid)
    sales = teams.employees(tid)[0]
    reg = _tools(driver)
    driver._tls.actor = _actor(sales, tid)
    try:
        with pytest.raises(BoardError) as caught:
            reg.call("bus_post", {"to": "查无此人", "subject": "x"})
        assert caught.value.code == "recipient_unknown"
        with pytest.raises(BoardError) as caught:
            reg.call("bus_post", {"to": "qa", "subject": "x"})
        assert caught.value.code == "recipient_not_in_team"
        assert reg.call("bus_post", {"to": "销售", "subject": "按中文名也能送"})["to"] == \
            {"to": sales["id"], "role": "sales"}
    finally:
        driver._tls.actor = None


def test_tools_board_move_只允许本岗位或下游验收(provisioned, board):
    driver, tid, employee = provisioned
    reg = _tools(driver)
    driver._tls.actor = _actor(employee, tid)
    cards = board.list(tid)
    own = next(c for c in cards if c["role"] == "product" and not c["depends_on"])
    foreign = next(c for c in cards if c["role"] == "developer")
    try:
        assert reg.call("board_move", {"card_id": own["id"], "to": "doing"})["ok"]
        assert board.get(own["id"])["status"] == "doing"
        with pytest.raises(BoardError) as caught:
            reg.call("board_move", {"card_id": foreign["id"], "to": "doing"})
        assert caught.value.code == "card_owner_mismatch"
        with pytest.raises(BoardError) as caught:
            reg.call("board_move", {"card_id": "ghost", "to": "doing"})
        assert caught.value.code == "card_not_found"
        with pytest.raises(BoardError) as caught:
            reg.call("board_move", {"card_id": own["id"], "to": "someday"})
        assert caught.value.code == "card_status_invalid"
        assert reg.call("board_move", {"card_id": own["id"], "to": "review"})["ok"]
    finally:
        driver._tls.actor = None

    architect = next(e for e in driver.teams.employees(tid) if e["role"] == "architect")
    reg = _tools(driver)
    driver._tls.actor = _actor(architect, tid)
    try:
        moved = reg.call("board_move", {"card_id": own["id"], "to": "done",
                                        "reason": "认可"})["card"]
        assert moved["status"] == "done" and moved["id"] == own["id"]
    finally:
        driver._tls.actor = None


def test_tools_board_add_与_comment_留痕并通知岗位(provisioned, board, bus):
    driver, tid, employee = provisioned
    reg = _tools(driver)
    actor = _actor(employee, tid)
    driver._tls.actor = actor
    try:
        before = {m["seq"] for m in bus.list(tid, limit=500)}
        added = reg.call("board_add", {"title": "补一张卡", "detail": "细节"})
        card = board.get(added["card_id"])
        assert card["role"] == "product" and card["created_by"] == employee["id"]
        assert card["id"] in actor.touched
        new_messages = [m for m in bus.list(tid, limit=500) if m["seq"] not in before]
        assert [m["subject"] for m in new_messages] == [f"新增任务卡：{card['title']}"]
        assert new_messages[0]["kind"] == "assign"
        assert new_messages[0]["to_actor"] == BROADCAST, "派给自己的卡广播给全员"

        qa = next(e for e in driver.teams.employees(tid) if e["role"] == "qa")
        before = {m["seq"] for m in bus.list(tid, limit=500)}
        reg.call("board_add", {"title": "交给测试", "role": "qa"})
        targeted = [m for m in bus.list(tid, limit=500) if m["seq"] not in before]
        assert targeted[0]["to_actor"] == qa["id"], "派给别人的卡要私聊对方"

        with pytest.raises(BoardError) as caught:
            reg.call("board_add", {"title": "怪岗位", "role": "hacker"})
        assert caught.value.code == "role_unknown"

        assert reg.call("board_comment", {"card_id": added["card_id"],
                                          "text": "结论：可开发"})["ok"] is True
        fresh = board.get(added["card_id"])
        assert fresh["output"]["deliverable"] == "结论：可开发"
        assert fresh["output"]["notes"][-1]["text"] == "结论：可开发"
        with pytest.raises(BoardError) as caught:
            reg.call("board_comment", {"card_id": added["card_id"], "text": " "})
        assert caught.value.code == "card_comment_empty"
        with pytest.raises(BoardError) as caught:
            reg.call("board_comment", {"card_id": "ghost", "text": "x"})
        assert caught.value.code == "card_not_found"
    finally:
        driver._tls.actor = None


# ==================================================== 7. 策略门禁 workforce
def test_policy_normalize_补齐并夹紧数字员工限制():
    engine = PolicyEngine()
    workforce = engine.normalize({"workforce": {
        "max_teams": 9999, "max_members": 0, "allowed_roles": ["qa", "qa", "  "],
        "enabled": False, "not-a-key": 1}})["workforce"]
    assert workforce["max_teams"] == 50 and workforce["max_members"] == 1
    assert workforce["allowed_roles"] == ["qa"]
    assert workforce["enabled"] is False
    assert workforce["auto_create_agents"] is True and workforce["allow_human_messages"]
    assert "not-a-key" not in workforce
    assert workforce["max_cards"] == 30 and workforce["max_messages_per_round"] == 40
    # 显式传 null 会被当成下限，而不是回到默认值（当前行为）
    assert engine.normalize({"workforce": {"max_rounds": None}})["workforce"][
        "max_rounds"] == 1
    assert engine.normalize(None)["workforce"]["enabled"] is True


def test_policy_workforce_在限制内放行():
    result = PolicyEngine().evaluate("start", "workforce", {
        "members": 6, "cards": 7, "roles": list(CHAIN_ORDER), "max_rounds": 12,
        "team_count": 0}, {"policy": None})
    assert result.allowed and result.violations == []
    assert result.primary_code == "policy_allowed"
    assert result.to_dict() == {"decision": "allow", "violations": []}


@pytest.mark.parametrize("snapshot, workforce_policy, code", [
    ({"members": 1}, {"enabled": False}, "workforce_disabled"),
    ({"members": 9}, {}, "max_members_exceeded"),
    ({"cards": 31}, {}, "max_cards_exceeded"),
    ({"team_count": 5}, {}, "max_teams_exceeded"),
    ({"max_rounds": 13}, {}, "max_rounds_exceeded"),
    ({"roles": ["developer", "qa"]}, {"allowed_roles": ["qa"]}, "role_denied"),
], ids=["disabled", "members", "cards", "teams", "rounds", "roles"])
def test_policy_workforce_限制产生文档化违规码(snapshot, workforce_policy, code):
    result = PolicyEngine().evaluate("start", "workforce", snapshot,
                                    {"policy": {"workforce": workforce_policy}})
    assert result.decision == "deny" and not result.allowed
    assert result.primary_code == code
    assert all(v.path and v.message for v in result.violations)
    assert code in [v.to_dict()["code"] for v in result.violations]


def test_policy_workforce_关闭开关时不再检查其他限制():
    result = PolicyEngine().evaluate("start", "workforce",
                                     {"members": 99, "cards": 99, "max_rounds": 99},
                                     {"policy": {"workforce": {"enabled": False}}})
    assert [v.code for v in result.violations] == ["workforce_disabled"]


def test_policy_workforce_多项违规一起报():
    result = PolicyEngine().evaluate("start", "workforce",
                                     {"members": 9, "cards": 31, "team_count": 5,
                                      "max_rounds": 99, "roles": ["sales"]},
                                     {"policy": {"workforce": {"allowed_roles": ["qa"]}}})
    assert {v.code for v in result.violations} == {
        "max_members_exceeded", "max_cards_exceeded", "max_teams_exceeded",
        "max_rounds_exceeded", "role_denied"}


def test_policy_空快照与未知资源类型():
    engine = PolicyEngine()
    assert engine.evaluate("start", "workforce", None).allowed
    assert engine.evaluate("start", "workforce", {}).allowed
    bad = engine.evaluate("start", "widget", {"anything": 1})
    assert [v.code for v in bad.violations] == ["invalid_resource_type"]


# ================================================ 8. HTTP API（TestClient）
def _login(app, username):
    client = TestClient(app)
    response = client.post("/api/auth/login", json={
        "username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    client.headers.update({"X-CSRF-Token": response.json()["csrf_token"],
                           "X-Workspace-ID": "default"})
    return client


@pytest.fixture
def members(app, owner):
    for name, role in (("admin", "admin"), ("editor", "editor"), ("viewer", "viewer")):
        added = owner.post("/api/workspaces/default/members", json={
            "username": name, "display_name": name.title(),
            "password": PASSWORD, "role": role})
        assert added.status_code == 200, added.text
    return {role: _login(app, role) for role in ("admin", "editor", "viewer")}


def _create_team(client, task=DEV_TASK, **body):
    response = client.post("/api/workforce/teams", json={"task": task, **body})
    assert response.status_code == 200, response.text
    return response.json()


def _snapshot(client, team_id):
    response = client.get(f"/api/workforce/teams/{team_id}/snapshot")
    assert response.status_code == 200, response.text
    return response.json()


def _set_policy(client, **workforce):
    saved = client.put("/api/governance/policy", json={"workforce": workforce})
    assert saved.status_code == 200, saved.text
    return saved.json()["workforce"]


def _wait_team_idle(client, team_id, timeout=60.0):
    ok = _wait_until(lambda: not _snapshot(client, team_id)["running"], timeout=timeout)
    assert ok, f"{timeout}s 内驱动线程未退出：{_snapshot(client, team_id)['team']}"
    return _snapshot(client, team_id)


def test_api_roles_列出六个岗位(owner):
    roles = owner.get("/api/workforce/roles").json()
    assert [r["key"] for r in roles] == list(CHAIN_ORDER)
    assert all({"name", "icon", "color", "charter", "deliverables",
                "downstream"} <= set(r) for r in roles)


def test_api_创建团队落库并返回快照(owner):
    snap = _create_team(owner)
    team = snap["team"]
    assert team["status"] == "draft" and team["task"] == DEV_TASK
    assert team["created_by"] == "owner" and team["plan"]["via"] == "rules"
    assert snap["employees"] == [] and snap["cards"] == [] and snap["edges"] == []
    assert snap["running"] is False and snap["stats"] == {"total": 0, "by_kind": {}}
    listed = owner.get("/api/workforce/teams").json()
    assert [t["id"] for t in listed] == [team["id"]]
    assert listed[0]["employee_count"] == 0 and listed[0]["running"] is False

    missing = owner.post("/api/workforce/teams", json={"task": "  "})
    assert missing.status_code == 422
    assert owner.get("/api/workforce/teams/ghost/snapshot").status_code == 404
    assert owner.post("/api/workforce/teams/ghost/start").status_code == 404


def test_api_step_装配团队并同步跑一轮(owner):
    team_id = _create_team(owner)["team"]["id"]
    stepped = owner.post(f"/api/workforce/teams/{team_id}/step")
    assert stepped.status_code == 200, stepped.text
    assert stepped.json()["status"] == "paused"

    snap = _snapshot(owner, team_id)
    assert len(snap["employees"]) == 6 and len(snap["cards"]) == 7
    assert snap["team"]["round_no"] == 1 and snap["progress"]["total"] == 7
    assert snap["rounds"][0]["round_no"] == 1
    assert snap["messages"] and snap["edges"] and snap["moves"]
    assert all(e["produced"]["mode"] == "fallback" for e in snap["employees"])
    assert all(e["role_meta"]["key"] == e["role"] for e in snap["employees"])
    assert owner.get("/api/workforce/teams").json()[0]["employee_count"] == 6


def test_api_patch_编制只接受合法计划(owner):
    team_id = _create_team(owner)["team"]["id"]
    bad = owner.patch(f"/api/workforce/teams/{team_id}",
                      json={"plan": {"roster": [{"role": "hacker"}]}})
    assert bad.status_code == 422
    empty_task = owner.patch(f"/api/workforce/teams/{team_id}", json={"task": " "})
    assert empty_task.status_code == 422

    patched = owner.patch(f"/api/workforce/teams/{team_id}", json={
        "task": QA_OPS_TASK,
        "plan": {"roster": [{"role": "qa"}, {"role": "ops"}],
                 "milestones": [{"key": "q1", "title": "用例", "role": "qa"},
                                {"key": "o1", "title": "发布", "role": "ops",
                                 "depends_on": ["q1", "ghost"]}]}})
    assert patched.status_code == 200, patched.text
    plan = patched.json()["team"]["plan"]
    assert [r["role"] for r in plan["roster"]] == ["qa", "ops"]
    assert plan["via"] == "human"
    assert [m["depends_on"] for m in plan["milestones"]] == [[], ["q1"]]

    started = owner.post(f"/api/workforce/teams/{team_id}/start")
    assert started.status_code == 200, started.text
    snap = _wait_team_idle(owner, team_id)
    assert len(snap["cards"]) == 2 and {c["role"] for c in snap["cards"]} == {"qa", "ops"}


def test_api_pause_与_stop_遵守团队状态机(owner):
    team_id = _create_team(owner)["team"]["id"]
    conflict = owner.post(f"/api/workforce/teams/{team_id}/pause")
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "team_status_conflict"
    stopped = owner.post(f"/api/workforce/teams/{team_id}/stop")
    assert stopped.status_code == 200 and stopped.json()["status"] == "stopped"
    assert owner.post(f"/api/workforce/teams/{team_id}/resume").status_code == 409


def test_api_cards_增改与状态门禁(owner):
    team_id = _create_team(owner)["team"]["id"]
    owner.post(f"/api/workforce/teams/{team_id}/step")
    cards_before = _snapshot(owner, team_id)["cards"]

    added = owner.post(f"/api/workforce/teams/{team_id}/cards",
                       json={"title": "人工插入的卡", "role": "developer",
                             "detail": "补一个导出功能"})
    assert added.status_code == 200, added.text
    new_card = added.json()["card"]
    assert new_card["role"] == "developer" and new_card["status"] == "backlog"
    assert new_card["created_by"] == "owner" and new_card["detail"] == "补一个导出功能"
    snap = _snapshot(owner, team_id)
    assert len(snap["cards"]) == len(cards_before) + 1
    assign = [m for m in snap["messages"] if m["subject"].startswith("老板加卡")]
    developer = next(e for e in snap["employees"] if e["role"] == "developer")
    assert len(assign) == 1 and assign[0]["to_actor"] == developer["id"]

    bad_role = owner.post(f"/api/workforce/teams/{team_id}/cards",
                          json={"title": "怪岗位", "role": "hacker"})
    assert bad_role.status_code == 422
    bad_title = owner.post(f"/api/workforce/teams/{team_id}/cards", json={"title": " "})
    assert bad_title.status_code == 409
    assert bad_title.json()["code"] == "card_title_required"

    moved = owner.put(f"/api/workforce/teams/{team_id}/cards/{new_card['id']}",
                      json={"status": "doing", "reason": "人工推进"})
    assert moved.status_code == 200 and moved.json()["card"]["status"] == "doing"
    review = owner.put(f"/api/workforce/teams/{team_id}/cards/{new_card['id']}",
                       json={"status": "review"})
    assert review.json()["card"]["status"] == "review"
    illegal = owner.put(f"/api/workforce/teams/{team_id}/cards/{new_card['id']}",
                        json={"status": "backlog"})
    assert illegal.status_code == 409
    assert illegal.json()["code"] == "card_transition_denied"
    renamed = owner.put(f"/api/workforce/teams/{team_id}/cards/{new_card['id']}",
                        json={"title": "改名", "priority": 3})
    assert renamed.json()["card"]["title"] == "改名"
    assert renamed.json()["card"]["priority"] == 3
    assert owner.put(f"/api/workforce/teams/{team_id}/cards/ghost",
                     json={"title": "x"}).status_code == 404


def test_api_inbox_读取即标记已读(owner):
    team_id = _create_team(owner)["team"]["id"]
    owner.post(f"/api/workforce/teams/{team_id}/step")
    product = next(e for e in _snapshot(owner, team_id)["employees"]
                   if e["role"] == "product")
    assert owner.get(f"/api/workforce/teams/ghost/inbox/{product['id']}").status_code == 404

    first = owner.get(f"/api/workforce/teams/{team_id}/inbox/{product['id']}")
    assert first.status_code == 200, first.text
    payload = first.json()
    assert any(m["to_actor"] == product["id"] and m["kind"] == "assign"
               for m in payload["messages"])
    assert payload["unread"] == 0, "GET 之后发给本人的消息应被标记已读"
    assert owner.get(f"/api/workforce/teams/{team_id}/inbox/ghost").status_code == 404, \
        "查无此人不能读到全团队广播，也不能写入已读"


def test_api_message_老板发言进入总线并可定向(owner):
    team_id = _create_team(owner)["team"]["id"]
    owner.post(f"/api/workforce/teams/{team_id}/step")
    assert owner.post(f"/api/workforce/teams/{team_id}/message",
                      json={"text": "  "}).status_code == 422
    assert owner.post(f"/api/workforce/teams/{team_id}/message",
                      json={"text": "在吗", "to": "查无此人"}).status_code == 422

    sent = owner.post(f"/api/workforce/teams/{team_id}/message", json={
        "text": "先做小程序端", "to": "产品经理", "subject": "方向", "resume": False})
    assert sent.status_code == 200, sent.text
    assert sent.json()["ok"] is True and sent.json()["resumed"] is False
    snap = _snapshot(owner, team_id)
    human_messages = [m for m in snap["messages"] if m["kind"] == "human"]
    assert len(human_messages) == 1
    message = human_messages[0]
    assert message["subject"] == "方向" and message["from_actor"] == HUMAN_ACTOR
    assert message["body"] == {"text": "先做小程序端", "from_role": "human"}
    assert message["round_no"] == snap["team"]["round_no"]
    product = next(e for e in snap["employees"] if e["role"] == "product")
    assert message["to_actor"] == product["id"]
    assert snap["team"]["status"] == "paused", "resume=False 不应把团队踢回 running"

    broadcast = owner.post(f"/api/workforce/teams/{team_id}/message",
                          json={"text": "都停一下", "resume": False})
    assert _snapshot(owner, team_id)["messages"][-1]["to_actor"] == BROADCAST
    assert broadcast.json()["ok"] is True


def test_api_草稿团队没有员工时定向消息被拒(owner):
    team_id = _create_team(owner)["team"]["id"]
    response = owner.post(f"/api/workforce/teams/{team_id}/message",
                         json={"text": "开工", "to": "产品经理"})
    assert response.status_code == 422, response.text
    assert "收件人不存在" in response.json()["detail"]


def test_api_message_在暂停或等待人类时自动续跑(owner):
    team_id = _create_team(owner, task=SALES_TASK)["team"]["id"]
    owner.post(f"/api/workforce/teams/{team_id}/step")
    assert _snapshot(owner, team_id)["team"]["status"] == "paused"

    resumed = owner.post(f"/api/workforce/teams/{team_id}/message",
                         json={"text": "按我的口径重写话术"})
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["resumed"] is True
    assert _wait_until(lambda: _snapshot(owner, team_id)["team"]["round_no"] >= 2,
                       timeout=30), "续跑后至少要多跑一轮"
    snap = _snapshot(owner, team_id)
    assert any(m["kind"] == "human" for m in snap["messages"])
    stopped = owner.post(f"/api/workforce/teams/{team_id}/stop")
    assert stopped.status_code == 200, stopped.text
    _wait_team_idle(owner, team_id, timeout=30)


def test_api_happy_path_产品任务跑到交付完成(owner):
    snap = _create_team(owner, start=True)
    team_id = snap["team"]["id"]
    assert snap["team"]["status"] == "running"

    final = _wait_until(lambda: (lambda s: s["team"]["status"] == "done"
                                 and not s["running"])(_snapshot(owner, team_id)),
                        timeout=60)
    snap = _snapshot(owner, team_id)
    assert final, f"60 秒内未收敛：{snap['team']['status']} / {snap['progress']}"
    assert snap["team"]["error"] is None
    assert len(snap["employees"]) == 6
    assert {e["role"] for e in snap["employees"]} == set(CHAIN_ORDER)
    assert len(snap["cards"]) == 7
    assert {c["status"] for c in snap["cards"]} == {"done"}
    assert snap["progress"]["done_ratio"] == 1.0
    assert snap["progress"]["roles_open"] == []
    assert snap["stats"]["total"] > 20, snap["stats"]
    assert len(snap["messages"]) > 20
    assert snap["edges"], "组织图连线不应为空"
    assert snap["moves"] and snap["rounds"]
    assert snap["team"]["summary"] and "已交付" in snap["team"]["summary"]
    assert snap["team"]["started_at"] and snap["team"]["finished_at"]
    assert snap["team"]["round_no"] >= 3
    assert all(e["produced"]["mode"] == "fallback" for e in snap["employees"])
    assert {m["kind"] for m in snap["messages"]} >= {"assign", "deliver", "review",
                                                     "announce"}
    assert snap["stats"]["by_kind"]["assign"] == 6
    assert owner.get("/api/workforce/teams").json()[0]["running"] is False


def test_api_规则降级仍产出可核对交付物(owner):
    snap = _create_team(owner, task=SALES_TASK, start=True)
    snap = _wait_team_idle(owner, snap["team"]["id"])
    assert len(snap["employees"]) == 1 and len(snap["cards"]) == 1
    deliverables = [m for m in snap["messages"] if m["kind"] == "deliver"]
    assert deliverables and "规则降级产出" in deliverables[-1]["body"]["text"]
    assert "未调用大模型" in deliverables[-1]["body"]["text"]
    assert snap["employees"][0]["produced"]["mode"] == "fallback"
    assert "目标客户" in snap["cards"][0]["output"]["deliverable"]


def test_api_没有产品经理的团队末端卡片无法放行(owner):
    """规则链路把「末端验收」交给产品经理；编制里没有产品经理时卡片会卡在 review，
    连续两轮无进展后转 waiting_human（当前行为，见最终报告）。"""
    team_id = _create_team(owner, task=SALES_TASK, start=True)["team"]["id"]
    snap = _wait_team_idle(owner, team_id)
    assert snap["team"]["status"] == "waiting_human", snap["team"]
    assert {c["status"] for c in snap["cards"]} == {"review"}
    assert snap["progress"]["done_ratio"] == 0.0
    asks = [m for m in snap["messages"] if m["from_actor"] == "crew"
            and m["to_actor"] == HUMAN_ACTOR]
    assert asks and "停滞" in asks[-1]["subject"]


# --------------------------------------------------------- RBAC 矩阵
def test_rbac_能力映射符合路由约定():
    assert _required_capability("GET", "/api/workforce/roles") == "resource.read"
    assert _required_capability("POST", "/api/workforce/teams") == "resource.write"
    for verb in ("start", "resume", "pause", "stop"):
        assert _required_capability(
            "POST", f"/api/workforce/teams/t1/{verb}") == "runtime.execute", verb
    # /message 走 resource.write（不是 runtime.execute）
    assert _required_capability("POST", "/api/workforce/teams/t1/message") == \
        "resource.write"
    assert _required_capability("POST", "/api/workforce/teams/t1/cards") == \
        "resource.write"
    assert _required_capability("PUT", "/api/workforce/teams/t1/cards/c1") == \
        "resource.write"
    assert _required_capability("POST", "/api/workforce/teams/t1/step") == "runtime.preview"
    assert _required_capability("GET", "/api/workforce/teams/t1/snapshot") == \
        "resource.read"
    assert _required_capability("GET", "/api/workforce/teams/t1/inbox/e1") == \
        "resource.read"
    assert _required_capability("GET", "/api/workforce/events") == "resource.read"
    assert "resource.read" in ROLE_CAPABILITIES["viewer"]
    assert "runtime.execute" in ROLE_CAPABILITIES["viewer"]
    assert "resource.write" not in ROLE_CAPABILITIES["viewer"]
    assert "runtime.preview" not in ROLE_CAPABILITIES["viewer"]


def test_rbac_viewer_只能读不能改(owner, members):
    viewer = members["viewer"]
    team_id = _create_team(owner)["team"]["id"]
    owner.post(f"/api/workforce/teams/{team_id}/step")

    assert viewer.get("/api/workforce/roles").status_code == 200
    assert viewer.get(f"/api/workforce/teams/{team_id}/snapshot").status_code == 200
    assert viewer.get("/api/workforce/teams").status_code == 200
    real_employee = _snapshot(viewer, team_id)["employees"][0]["id"]
    assert viewer.get(f"/api/workforce/teams/{team_id}/inbox/e1").status_code == 404, \
        "RBAC 放行不等于凭空造工号"
    assert viewer.get(
        f"/api/workforce/teams/{team_id}/inbox/{real_employee}").status_code == 200
    for method, path, payload in (
        ("post", "/api/workforce/teams", {"task": DEV_TASK}),
        ("post", f"/api/workforce/teams/{team_id}/cards", {"title": "加卡"}),
        ("post", f"/api/workforce/teams/{team_id}/message", {"text": "指示"}),
        ("post", f"/api/workforce/teams/{team_id}/step", {}),
        ("patch", f"/api/workforce/teams/{team_id}", {"task": "改目标"}),
        ("put", f"/api/workforce/teams/{team_id}/cards/c1", {"title": "x"}),
    ):
        response = getattr(viewer, method)(path, json=payload)
        assert response.status_code == 403, (path, response.text)
        assert response.json()["code"] == "permission_denied", path
        assert "权限" in response.json()["detail"], path


def test_rbac_viewer_可start与stop因为它持有_runtime_execute(owner, members):
    """start/resume/pause/stop 映射 runtime.execute，而 viewer 恰好有该能力
    （与 /api/flows/{id}/run 同档），所以只读角色能开团队也能停团队。"""
    viewer, admin = members["viewer"], members["admin"]
    team_id = _create_team(owner)["team"]["id"]
    started = viewer.post(f"/api/workforce/teams/{team_id}/start", json={})
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "running"
    assert admin.post(f"/api/workforce/teams/{team_id}/stop").status_code == 200
    _wait_team_idle(viewer, team_id, timeout=30)


def test_rbac_editor_可编排与推进但不能管治理(owner, members):
    editor = members["editor"]
    snap = _create_team(editor, task=SALES_TASK)
    team_id = snap["team"]["id"]
    assert snap["team"]["created_by"] == "editor"
    assert editor.post(f"/api/workforce/teams/{team_id}/step").status_code == 200
    assert editor.post(f"/api/workforce/teams/{team_id}/cards",
                       json={"title": "编辑加的卡"}).status_code == 200
    assert editor.post(f"/api/workforce/teams/{team_id}/message",
                       json={"text": "先做话术", "resume": False}).status_code == 200
    assert editor.post(f"/api/workforce/teams/{team_id}/start", json={}).status_code == 200
    _wait_team_idle(editor, team_id, timeout=30)

    for method, path, payload in (
        ("get", "/api/governance/audit", None),
        ("put", "/api/governance/policy", {"workforce": {"max_teams": 2}}),
        ("post", "/api/workspaces/default/members", {"username": "x", "role": "viewer"}),
        ("post", "/api/workspaces", {"id": "another", "name": "Another"}),
    ):
        response = (getattr(editor, method)(path) if payload is None
                    else getattr(editor, method)(path, json=payload))
        assert response.status_code == 403, (path, response.text)


def test_rbac_admin_可读审计但改策略只有_owner(owner, members):
    """ROLE_CAPABILITIES 里 admin 止步于 audit.read，policy.manage 只有 owner（"*"）
    持有（governance.py:25-34）：数字员工的门禁不能由被管的人自己放宽。"""
    admin = members["admin"]
    before = admin.get("/api/governance/policy").json()["workforce"]["max_teams"]
    assert admin.get("/api/governance/audit").status_code == 200
    denied = admin.put("/api/governance/policy",
                       json={"workforce": {"max_teams": 2}})
    assert denied.status_code == 403, denied.text
    assert denied.json()["code"] == "permission_denied"
    assert (admin.get("/api/governance/policy").json()
            ["workforce"]["max_teams"]) == before, "拒绝后策略不应被改动"
    team_id = _create_team(admin, task=SALES_TASK)["team"]["id"]
    assert admin.post(f"/api/workforce/teams/{team_id}/pause").status_code == 409
    assert admin.post(f"/api/workforce/teams/{team_id}/stop").status_code == 200
    assert owner.put("/api/governance/policy",
                     json={"workforce": {"max_teams": 2}}).status_code == 200


# --------------------------------------------------- 治理集成
def test_governance_团队创建与运行写入审计(owner):
    snap = _create_team(owner, start=True)
    team_id = snap["team"]["id"]
    _wait_team_idle(owner, team_id)
    audit = owner.get("/api/governance/audit", params={"limit": 300}).json()
    by_type = {}
    for event in audit:
        by_type.setdefault(event["event_type"], []).append(event)
    assert "workforce.team.create" in by_type, sorted(by_type)
    create = by_type["workforce.team.create"][0]
    assert create["resource_type"] == "workforce" and create["resource_id"] == team_id
    assert create["username"] == "owner" and create["outcome"] == "success"
    assert {"via", "roles", "cards"} <= set(create["details"])
    assert create["details"]["roles"] == list(CHAIN_ORDER)
    for event_type in ("workforce.team.provision", "workforce.team.start",
                       "workforce.card.move", "workforce.message.post",
                       "workforce.deliver.summary"):
        assert event_type in by_type, (event_type, sorted(by_type))
    assert all(row["resource_id"] == team_id for row in by_type["workforce.card.move"])
    assert by_type["workforce.deliver.summary"][0]["details"]["cards"] == 7


def test_governance_max_teams_拦住第二支团队(owner):
    _create_team(owner, task=SALES_TASK)
    saved = _set_policy(owner, max_teams=1)
    assert saved["max_teams"] == 1 and saved["max_members"] == 8
    blocked = owner.post("/api/workforce/teams", json={"task": DEV_TASK})
    assert blocked.status_code == 403, blocked.text
    body = blocked.json()
    assert body["code"] == "max_teams_exceeded"
    assert body["policy"] == {"decision": "deny", "violations": [
        {"code": "max_teams_exceeded",
         "message": body["policy"]["violations"][0]["message"], "path": "workforce"}]}
    assert body["request_id"]
    audit = owner.get("/api/governance/audit", params={"limit": 20}).json()
    denied = [e for e in audit if e["event_type"] == "policy.denied"]
    assert denied and denied[0]["outcome"] == "failed"
    assert denied[0]["details"]["code"] == "max_teams_exceeded"


def test_governance_岗位白名单与成员轮次上限在创建时生效(owner):
    _set_policy(owner, allowed_roles=["qa", "ops"])
    snap = _create_team(owner, task=DEV_TASK)
    assert [r["role"] for r in snap["team"]["plan"]["roster"]] == ["qa", "ops"]
    assert len(snap["team"]["plan"]["milestones"]) == 2

    _set_policy(owner, allowed_roles=["qa", "ops"], max_members=1)
    blocked = owner.post("/api/workforce/teams", json={"task": DEV_TASK})
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "max_members_exceeded"

    _set_policy(owner, max_rounds=2)
    too_many = owner.post("/api/workforce/teams", json={"task": SALES_TASK,
                                                        "max_rounds": 99})
    assert too_many.status_code == 403
    assert too_many.json()["code"] == "max_rounds_exceeded"
    assert owner.get("/api/workforce/teams").json()[0]["max_rounds"] == 12


def test_governance_白名单与任务不匹配时建队失败(owner):
    _set_policy(owner, allowed_roles=["product"])
    assert owner.post("/api/workforce/teams",
                      json={"task": SALES_TASK}).status_code == 422


def test_governance_关闭数字员工与禁止人工消息(owner):
    _set_policy(owner, enabled=False)
    blocked = owner.post("/api/workforce/teams", json={"task": DEV_TASK})
    assert blocked.status_code == 403 and blocked.json()["code"] == "workforce_disabled"
    assert owner.get("/api/workforce/teams").json() == []

    _set_policy(owner, enabled=True)
    team_id = _create_team(owner, task=SALES_TASK)["team"]["id"]
    _set_policy(owner, enabled=True, allow_human_messages=False)
    denied = owner.post(f"/api/workforce/teams/{team_id}/message", json={"text": "继续"})
    assert denied.status_code == 403, denied.text
    assert denied.json()["code"] == "human_message_denied"
    # 只读接口不受开关影响
    assert owner.get(f"/api/workforce/teams/{team_id}/snapshot").status_code == 200


def test_governance_自动创建岗位智能体资产(owner):
    _set_policy(owner, auto_create_agents=False)
    _create_team(owner, task=SALES_TASK)
    assert owner.get("/api/governance/resources/agent/wf-sales/versions").json() == []

    _set_policy(owner, auto_create_agents=True)
    _create_team(owner, task=DEV_TASK)
    versions = owner.get("/api/governance/resources/agent/wf-sales/versions").json()
    assert versions and versions[0]["status"] == "pending"
    snapshot = versions[0]["snapshot"]
    assert snapshot["role"] == "sales" and snapshot["id"] == "wf-sales"
    assert set(snapshot["tool_ids"]) >= set(TEAM_TOOLS)


def test_governance_start_按当前策略复核计划编制(owner):
    """draft 团队还没 provision，编制只存在于计划里。start 若按「已存在的学生人数」
    过门禁就恒等于放行：先宽松建队 → 收紧 max_members → start 即可绕过策略。
    现在 start 用计划编制复核（server.py:workforce_start）。"""
    team_id = _create_team(owner, task=DEV_TASK)["team"]["id"]
    patched = owner.patch(f"/api/workforce/teams/{team_id}", json={
        "plan": {"roster": [{"role": "developer"}, {"role": "qa"}],
                 "milestones": [{"key": "d", "title": "实现", "role": "developer"},
                                {"key": "q", "title": "用例", "role": "qa",
                                 "depends_on": ["d"]}]}})
    assert patched.status_code == 200, patched.text
    _set_policy(owner, max_members=1)          # 计划落库后再收紧门禁
    started = owner.post(f"/api/workforce/teams/{team_id}/start")
    assert started.status_code == 403, started.text
    assert started.json()["code"] == "max_members_exceeded"
    snap = _snapshot(owner, team_id)
    assert snap["team"]["status"] == "draft"
    assert snap["employees"] == [], "被门禁挡住时不能已经把员工开出来"


# ------------------------------------------------- CSRF / 会话
def test_csrf_workforce_写操作缺少_token_被拒(app, owner):
    team_id = _create_team(owner)["team"]["id"]
    anonymous = TestClient(app)
    assert anonymous.post("/api/auth/login", json={
        "username": "owner", "password": PASSWORD}).status_code == 200
    anonymous.headers.update({"X-Workspace-ID": "default"})
    for method, path in (("post", "/api/workforce/teams"),
                         ("post", f"/api/workforce/teams/{team_id}/start"),
                         ("post", f"/api/workforce/teams/{team_id}/message"),
                         ("post", f"/api/workforce/teams/{team_id}/cards"),
                         ("patch", f"/api/workforce/teams/{team_id}"),
                         ("get", f"/api/workforce/teams/{team_id}/snapshot")):
        send = getattr(anonymous, method)
        response = (send(path) if method == "get"
                    else send(path, json={"task": DEV_TASK,
                                          "text": "x", "title": "y"}))
        if method == "get":
            assert response.status_code == 200, path      # GET 不需要 CSRF
            continue
        assert response.status_code == 403, (path, response.text)
        assert response.json()["code"] == "csrf_failed", path


def test_api_未登录与未知工作区(app, owner):
    fresh = TestClient(app)
    anonymous = fresh.get("/api/workforce/teams")
    assert anonymous.status_code == 401
    assert anonymous.json()["code"] == "authentication_required"

    wrong_ws = owner.get("/api/workforce/teams", headers={"X-Workspace-ID": "ghost"})
    assert wrong_ws.status_code == 403
    assert wrong_ws.json()["code"] == "workspace_forbidden"


def test_api_workspace_隔离两支团队(owner):
    created = owner.post("/api/workspaces", json={"id": WS_B, "name": "Team B"})
    assert created.status_code == 200, created.text
    dev_team = _create_team(owner)["team"]["id"]

    owner.headers["X-Workspace-ID"] = WS_B
    other_team = _create_team(owner, task=SALES_TASK)["team"]["id"]
    assert dev_team != other_team
    assert [t["id"] for t in owner.get("/api/workforce/teams").json()] == [other_team]
    cross = owner.get(f"/api/workforce/teams/{dev_team}/snapshot")
    assert cross.status_code == 404, cross.text
    assert owner.post(f"/api/workforce/teams/{dev_team}/step").status_code == 404

    owner.headers["X-Workspace-ID"] = "default"
    assert [t["id"] for t in owner.get("/api/workforce/teams").json()] == [dev_team]
    stepped = owner.post(f"/api/workforce/teams/{dev_team}/step")
    assert stepped.status_code == 200, stepped.text
    assert owner.get(f"/api/workforce/teams/{other_team}/snapshot").status_code == 404
