"""Live runs expose active nodes, isolated logs, and durable exception details."""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from conftest import make_governed_client  # noqa: E402
from flow_studio.engine import RunResult  # noqa: E402
from flow_studio.graph import graph_from_dict  # noqa: E402
from flow_studio.registry import AgentRegistry  # noqa: E402
from flow_studio.server import create_app  # noqa: E402
from flow_studio.store import RunStore  # noqa: E402
from flow_studio.workspace import WorkspaceRuntime  # noqa: E402


LOG = logging.getLogger("flow_studio.test")


@pytest.fixture
def live_app(config_dir, tmp_path):
    app = create_app(config_dir, tmp_path / "data")
    client = make_governed_client(app)
    runtime = app.state.studio.workspaces.get("default")
    return app, client, runtime


def action_graph(action="wait", *, optional=False):
    return {
        "id": "live-diagnostics", "name": "Live diagnostics",
        "triggers": ["execute live diagnostics"],
        "nodes": [
            {"id": "start", "type": "start", "params": {
                "inputs": [{"key": "tag", "default": "request"}]}},
            {"id": "work", "type": "agent", "label": "Diagnostic work",
             "params": {"agent": "test", "action": action,
                        "optional": optional, "args": {"tag": "{{input.tag}}"}}},
            {"id": "end", "type": "end", "params": {"output": "done"}},
        ],
        "edges": [{"from": "start", "to": "work"},
                  {"from": "work", "to": "end"}],
    }


def save_graph(client, graph, *, publish=True):
    response = client.post("/api/flows", json=graph)
    assert response.status_code == 200, response.text
    version = response.json()["_governance"]["version"]
    if publish:
        for action in ("submit", "approve", "publish"):
            response = client.post(
                f"/api/governance/resources/flow/{graph['id']}/versions/"
                f"{version}/{action}", json={"reason": "Live run test"})
            assert response.status_code == 200, response.text
    return version


def await_finished(client, run_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}")
        assert response.status_code == 200, response.text
        run = response.json()
        if run["status"] not in {"queued", "running"}:
            return run
        time.sleep(0.01)
    pytest.fail(f"Run {run_id} did not finish: {run}")


@pytest.mark.parametrize("entry", ["published", "preview", "chat"])
def test_background_entry_exposes_node_and_log_before_completion(live_app, entry):
    _, client, runtime = live_app
    entered, release = threading.Event(), threading.Event()

    def blocked_action(args):
        LOG.info("waiting for source response")
        entered.set()
        if not release.wait(10):
            raise TimeoutError("test did not release the action")
        LOG.info("source response received")
        return {"ok": True}

    runtime.registry.register("test", "wait", blocked_action)
    graph = action_graph()
    version = save_graph(client, graph, publish=entry != "preview")
    if entry == "chat":
        endpoint = "/api/agent/chat"
        body = {"message": "execute live diagnostics", "background": True}
    elif entry == "preview":
        endpoint = (f"/api/governance/resources/flow/{graph['id']}/versions/"
                    f"{version}/preview")
        body = {"inputs": {}, "background": True}
    else:
        endpoint = f"/api/flows/{graph['id']}/run"
        body = {"inputs": {}, "background": True}

    # A blocked action must not hold the HTTP response open. The separate
    # request thread also lets the finally block unblock a broken implementation.
    with ThreadPoolExecutor(max_workers=1) as requests:
        future = requests.submit(client.post, endpoint, json=body)
        try:
            response = future.result(timeout=3)
            assert response.status_code == 202, response.text
            payload = response.json()
            initial = payload["run"] if entry == "chat" else payload
            assert initial["status"] in {"queued", "running"}
            run_id = initial["run_id"]
            assert entered.wait(3)

            events_response = client.get(f"/api/runs/{run_id}/events")
            assert events_response.status_code == 200, events_response.text
            page = events_response.json()
            assert page["run"]["status"] == "running"
            assert not page["run"]["finished_at"]
            current = next(node for node in page["run"]["node_runs"]
                           if node["node_id"] == "work")
            assert current["status"] == "running"
            assert page["run"]["graph"]["id"] == graph["id"]
            work_events = [event for event in page["events"]
                           if event.get("node_id") == "work"]
            assert any(event["type"] == "node.started" for event in work_events)
            assert any(event["type"] == "node.log"
                       and "waiting for source response" in event["message"]
                       for event in work_events)
            assert not any(event["type"] == "node.finished" for event in work_events)
            cursor = page["next_seq"]
        finally:
            release.set()

    completed = await_finished(client, run_id)
    assert completed["status"] == "success" and completed["output"] == "done"
    tail = client.get(f"/api/runs/{run_id}/events", params={"after": cursor}).json()
    assert all(event["seq"] > cursor for event in tail["events"])
    assert any(event["type"] == "node.log"
               and "source response received" in event["message"]
               for event in tail["events"])
    assert tail["events"][-1]["type"] == "run.finished"


@pytest.mark.parametrize("optional", [False, True])
def test_full_exception_chain_survives_persistence_and_optional_nodes(live_app, optional):
    _, client, runtime = live_app

    def failing_action(args):
        try:
            raise ValueError("source payload invalid")
        except ValueError as cause:
            raise RuntimeError("job extraction failed") from cause

    runtime.registry.register("test", "fail", failing_action)
    graph = action_graph("fail", optional=optional)
    save_graph(client, graph)
    response = client.post(f"/api/flows/{graph['id']}/run", json={"inputs": {}})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == ("success" if optional else "failed")
    run_id = result["run_id"]
    stored = client.get(f"/api/runs/{run_id}").json()
    node = next(item for item in stored["node_runs"] if item["node_id"] == "work")
    assert node["status"] == ("skipped" if optional else "failed")
    traceback = node["traceback"]
    assert "ValueError: source payload invalid" in traceback
    assert "RuntimeError: job extraction failed" in traceback
    assert "The above exception was the direct cause" in traceback
    assert "failing_action" in traceback and 'File "' in traceback
    page = client.get(f"/api/runs/{run_id}/events").json()
    error_events = [event for event in page["events"] if event.get("traceback")]
    assert any("ValueError: source payload invalid" in event["traceback"]
               and "RuntimeError: job extraction failed" in event["traceback"]
               for event in error_events)


def test_event_cursor_can_resume_without_duplicates_after_store_reopen(live_app):
    _, client, runtime = live_app
    runtime.registry.register("test", "wait", lambda args: {"ok": True})
    graph = action_graph()
    save_graph(client, graph)
    response = client.post(f"/api/flows/{graph['id']}/run", json={"inputs": {}})
    assert response.status_code == 200, response.text
    run = response.json()
    run_id = run["run_id"]
    first = client.get(f"/api/runs/{run_id}/events", params={"limit": 2}).json()
    assert len(first["events"]) == 2 and first["has_more"]
    assert first["next_seq"] == first["events"][-1]["seq"]

    reopened = RunStore(runtime.root / "flows" / "runs.sqlite")
    seen = list(first["events"])
    cursor = first["next_seq"]
    for _ in range(100):
        page = reopened.events(run_id, after=cursor, limit=2)
        assert page["run"]["status"] == "success"
        assert all(event["seq"] > cursor for event in page["events"])
        seen.extend(page["events"])
        cursor = page["next_seq"]
        if not page["has_more"]:
            break
    else:
        pytest.fail("Event pagination did not terminate")

    sequences = [event["seq"] for event in seen]
    assert sequences == sorted(set(sequences))
    all_events = client.get(f"/api/runs/{run_id}/events").json()["events"]
    assert seen == all_events
    assert seen[-1]["type"] == "run.finished"
    empty = reopened.events(run_id, after=cursor, limit=2)
    assert empty["events"] == [] and empty["next_seq"] == cursor
    assert empty["has_more"] is False
    assert reopened.get(run_id)["output"] == run["output"]
    assert any(item["run_id"] == run_id for item in reopened.list(graph["id"]))


def test_parallel_runs_do_not_mix_node_logs(live_app):
    _, client, runtime = live_app
    release = threading.Event()
    entered = {"alpha": threading.Event(), "beta": threading.Event()}

    def concurrent_action(args):
        tag = args["tag"]
        LOG.warning("source started for %s", tag)
        entered[tag].set()
        if not release.wait(10):
            raise TimeoutError("test did not release concurrent actions")
        LOG.warning("source finished for %s", tag)
        return {"tag": tag}

    runtime.registry.register("test", "wait", concurrent_action)
    graph = graph_from_dict(action_graph())
    runs = {}
    try:
        for tag in entered:
            runs[tag] = runtime.start_flow(graph, {"tag": tag})["run_id"]
        assert all(event.wait(3) for event in entered.values())
        LOG.warning("unrelated request outside any run")
        for tag, run_id in runs.items():
            page = client.get(f"/api/runs/{run_id}/events").json()
            assert page["run"]["status"] == "running"
            messages = [event["message"] for event in page["events"]
                        if event["type"] == "node.log"]
            assert f"source started for {tag}" in messages
            other = "beta" if tag == "alpha" else "alpha"
            assert all(other not in message for message in messages)
            assert all("unrelated request" not in message for message in messages)
    finally:
        release.set()
    for tag, run_id in runs.items():
        assert await_finished(client, run_id)["status"] == "success"
        messages = [event["message"] for event in
                    client.get(f"/api/runs/{run_id}/events").json()["events"]
                    if event["type"] == "node.log"]
        assert f"source finished for {tag}" in messages
        other = "beta" if tag == "alpha" else "alpha"
        assert all(other not in message for message in messages)


def test_run_events_require_authentication_and_workspace_membership(live_app):
    app, client, runtime = live_app
    runtime.registry.register("test", "wait", lambda args: {"ok": True})
    run = runtime.run_flow(graph_from_dict(action_graph()))
    endpoint = f"/api/runs/{run['run_id']}/events"
    anonymous = TestClient(app)
    assert anonymous.get(endpoint).status_code == 401
    assert client.get(endpoint).status_code == 200
    assert client.get("/api/runs/unknown/events").status_code == 404
    created = client.post("/api/workspaces", json={"id": "other", "name": "Other"})
    assert created.status_code == 200, created.text
    client.headers["X-Workspace-ID"] = "other"
    assert client.get(endpoint).status_code == 404
    assert client.get(f"/api/runs/{run['run_id']}").status_code == 404
    assert client.get("/api/runs").json() == []


def test_restart_marks_only_unfinished_runs_interrupted(tmp_path):
    root = tmp_path / "workspace"
    store = RunStore(root / "flows" / "runs.sqlite")
    for status in ("queued", "running", "success"):
        run = RunResult(run_id=status, flow_id="test", flow_name="Test",
                        status=status).to_dict()
        if status == "running":
            run["node_runs"] = [{"node_id": "work", "status": "running"}]
        store.record(run, {"type": f"run.{status}", "message": status})

    # Merely opening another reader must not interrupt a live process.
    assert RunStore(root / "flows" / "runs.sqlite").get("running")["status"] == "running"
    runtime = WorkspaceRuntime("test", root, {}, AgentRegistry(), None)
    for run_id in ("queued", "running"):
        snapshot = runtime.runs.get(run_id)
        assert snapshot["status"] == "interrupted" and snapshot["finished_at"]
        assert snapshot["error"]
        events = runtime.runs.events(run_id)["events"]
        assert events[-1]["type"] == "run.finished"
        assert events[-1]["level"] == "error"
    assert runtime.runs.get("running")["node_runs"][0]["status"] != "running"
    assert runtime.runs.get("success")["status"] == "success"
    first_count = len(runtime.runs.events("running")["events"])
    runtime.runs.interrupt_pending()
    assert len(runtime.runs.events("running")["events"]) == first_count


@pytest.mark.parametrize("optional", [False, True])
def test_credentials_are_redacted_in_live_logs_and_stored_traceback(live_app, optional):
    _, client, runtime = live_app
    samples = [
        "Authorization: Basic ZGVtbzpwYXNz",
        "Cookie: theme=dark; session=secret-session",
        '{"password": "two words secret"}',
        "https://demo:secret-pass@example.invalid/path?token=query-secret",
    ]

    def fails_with_credentials(args):
        for sample in samples:
            LOG.warning("upstream context %s", sample)
        raise RuntimeError("\n".join(samples))

    runtime.registry.register("test", "fail", fails_with_credentials)
    result = runtime.run_flow(graph_from_dict(action_graph("fail", optional=optional)))
    page = client.get(f"/api/runs/{result['run_id']}/events")
    assert page.status_code == 200
    for secret in ("ZGVtbzpwYXNz", "secret-session", "two words secret",
                   "secret-pass", "query-secret"):
        assert secret not in page.text
    work = next(node for node in page.json()["run"]["node_runs"] if node["node_id"] == "work")
    assert work["status"] == ("skipped" if optional else "failed")
    trace = work["traceback"]
    assert "fails_with_credentials" in trace and "RuntimeError" in trace
    assert "[REDACTED]" in trace
