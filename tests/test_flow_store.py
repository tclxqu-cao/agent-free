"""Flow Studio：流程存储与运行记录测试。"""

import pytest

from flow_studio.builtin_flows import builtin_flows
from flow_studio.graph import graph_from_dict
from flow_studio.store import FlowStore, RunStore


@pytest.fixture
def store(tmp_path):
    return FlowStore(tmp_path / "flows")


def test_seed_and_list(store):
    n = store.seed_if_empty(builtin_flows())
    assert n == 5
    assert store.seed_if_empty(builtin_flows()) == 0  # 幂等
    assert [g.id for g in store.list()] == ["agent-brain-test", "job-hunt-daily",
                                            "job-hunt-demo", "job-intent-demo",
                                            "video-demo"]


def test_seed_missing_only_adds(store):
    assert store.seed_if_empty(builtin_flows()[:2]) == 2
    # 增量补种：只加缺的，不动已有的
    n = store.seed_missing(builtin_flows())
    assert n == 3
    assert {g.id for g in store.list()} == {"agent-brain-test", "job-hunt-daily",
                                            "job-hunt-demo", "job-intent-demo",
                                            "video-demo"}
    assert store.seed_missing(builtin_flows()) == 0


def test_save_get_delete_roundtrip(store):
    g = graph_from_dict({
        "id": "my-flow", "name": "自定义",
        "nodes": [{"id": "start", "type": "start"}, {"id": "end", "type": "end"}],
        "edges": [{"from": "start", "to": "end"}],
    })
    store.save(g)
    got = store.get("my-flow")
    assert got.name == "自定义"
    assert got.nodes[0].pos == {}
    store.delete("my-flow")
    assert store.get("my-flow") is None
    assert store.delete("my-flow") is False


def test_invalid_id_rejected(store):
    with pytest.raises(ValueError, match="非法流程"):
        store.get("../escape")


def test_run_store(tmp_path):
    rs = RunStore(tmp_path / "runs.sqlite")
    rs.append({"run_id": "r1", "flow_id": "f", "flow_name": "F", "status": "success",
               "input": {}, "node_runs": [], "output": "ok",
               "error": None, "started_at": "2026-09-11T10:00:00",
               "finished_at": "2026-09-11T10:00:01"})
    rs.append({"run_id": "r2", "flow_id": "f", "flow_name": "F", "status": "failed",
               "input": {}, "node_runs": [], "output": "",
               "error": "炸了", "started_at": "2026-09-11T10:01:00",
               "finished_at": "2026-09-11T10:01:02"})
    assert len(rs.list()) == 2
    assert rs.list(limit=1)[0]["run_id"] == "r2"
    full = rs.get("r1")
    assert full["output"] == "ok" and full["status"] == "success"
    assert rs.get("missing") is None
