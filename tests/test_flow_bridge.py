"""Flow Studio：Agent 桥（bridge）测试——fake SSE 服务 + 引擎 brain 节点。"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from flow_studio import bridge as bridge_mod


class _FakeAgent(BaseHTTPRequestHandler):
    """最小 AgentRoam 假体：POST run -> 202 body；GET stream -> SSE done。"""

    def log_message(self, *a):  # 静默
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self._last_input = body["input"]
        payload = json.dumps({"sessionId": "s-1", "runId": "r-1",
                              "streamUrl": "/api/agent/stream?sessionId=s-1"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        events = [
            {"type": "runtime_progress", "phase": "thinking", "label": "x"},
            {"type": "tool_call", "toolCall": {"id": "t1"}},
            {"type": "text_chunk", "text": "部分", "messagePhase": "commentary"},
            {"type": "done", "finalText": "你好，我是你的 agent", "durationMs": 123},
        ]
        body = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def fake_server():
    server = HTTPServer(("127.0.0.1", 0), _FakeAgent)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_agent_reason_parses_done(fake_server):
    out = bridge_mod.agent_reason({"base_url": fake_server}, "你好")
    assert out["text"] == "你好，我是你的 agent"
    assert out["session_id"] == "s-1" and out["run_id"] == "r-1"
    assert out["tool_calls"] == 1 and out["duration_ms"] == 123


def test_agent_reason_error_event(tmp_path):
    class ErrAgent(_FakeAgent):
        def do_GET(self):
            events = [{"type": "error", "message": "provider 未配置"}]
            body = "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), ErrAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with pytest.raises(RuntimeError, match="provider 未配置"):
        bridge_mod.agent_reason({"base_url": f"http://127.0.0.1:{server.server_port}"},
                                "hi")
    server.shutdown()


def test_agent_reason_unreachable():
    with pytest.raises(Exception):
        bridge_mod.agent_reason({"base_url": "http://127.0.0.1:1"}, "hi", timeout=2)


# ---------------- 引擎 brain 节点 ----------------
def _g(nodes, edges):
    from flow_studio.graph import graph_from_dict
    return graph_from_dict({"id": "t", "name": "测试", "nodes": nodes, "edges": edges})


def _node(id, type, **params):
    return {"id": id, "type": type, "params": params}


def test_brain_node_success(monkeypatch):
    import flow_studio.engine as engine_mod
    monkeypatch.setattr(engine_mod.bridge_mod, "agent_reason",
                        lambda cfg, prompt, session_id=None, timeout=300.0:
                        {"text": f"echo:{prompt}", "session_id": "s", "run_id": "r",
                         "tool_calls": 2, "asked_user": False, "duration_ms": 5})
    from flow_studio.engine import FlowRunner

    g = _g([
        _node("start", "start", inputs=[{"key": "message", "default": "hi"}]),
        _node("b", "brain", prompt="说：{{input.message}}"),
        _node("end", "end", output="{{b.text}}（{{b.tool_calls}}次工具）"),
    ], [{"from": "start", "to": "b"}, {"from": "b", "to": "end"}])
    run = FlowRunner(bridge_cfg={"base_url": "http://x"}).run(g, {})
    assert run.status == "success" and run.output == "echo:说：hi（2次工具）"


def test_brain_node_degrades(monkeypatch):
    import flow_studio.engine as engine_mod

    def boom(*a, **k):
        raise RuntimeError("连不上 :3000")
    monkeypatch.setattr(engine_mod.bridge_mod, "agent_reason", boom)
    from flow_studio.engine import FlowRunner

    g = _g([
        _node("start", "start"),
        _node("b", "brain", prompt="x"),
        _node("end", "end", output="继续"),
    ], [{"from": "start", "to": "b"}, {"from": "b", "to": "end"}])
    run = FlowRunner().run(g, {})
    assert run.status == "success"
    assert run.node_run("b").status == "skipped"
    assert run.output == "继续"

    g2 = _g([
        _node("start", "start"),
        _node("b", "brain", prompt="x", required=True),
        _node("end", "end", output="继续"),
    ], [{"from": "start", "to": "b"}, {"from": "b", "to": "end"}])
    run2 = FlowRunner().run(g2, {})
    assert run2.status == "failed" and "推理失败" in run2.error
