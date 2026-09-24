import json
import stat
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from flow_studio.external_agent import (
    CustomerAgentProvider,
    ExternalAgentCredentialStore,
    ExternalAgentError,
)


class _FlowProtocolHandler(BaseHTTPRequestHandler):
    authorization = ""
    run_body = None

    def log_message(self, *_args):
        pass

    def _json(self, value, status=200):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).authorization = self.headers.get("Authorization", "")
        if self.path == "/api/flow/v1/catalog":
            self._json({
                "protocolVersion": "1",
                "provider": {"id": "customer-agent", "name": "Customer Agent"},
                "features": {"streaming": True, "memory": True,
                             "cancellation": True},
                "models": [{"id": "p1", "name": "Model", "provider": "aihub"}],
                "skills": [{"id": "job-hunt", "name": "job-hunt",
                            "description": "jobs"}],
                "tools": [{"id": "read_file", "name": "read_file",
                           "description": "read", "inputSchema": {}}],
                "mcpServers": [{"id": "m1", "name": "MCP",
                                "status": "available"}],
                "toolPolicies": [{"id": "local-readonly",
                                  "name": "Local read only"}],
            })
            return
        if self.path == "/api/flow/v1/runs/r1/events":
            events = [
                ("1", "run.started", {"runId": "r1", "sessionId": "s1"}),
                ("2", "tool.started", {"name": "read_file", "callId": "c1"}),
                ("3", "tool.completed", {"callId": "c1", "content": "ok"}),
                ("4", "assistant.delta", {"text": "hello "}),
                ("5", "run.completed", {"text": "hello world", "durationMs": 12}),
            ]
            body = "".join(
                f"id: {event_id}\nevent: {event_type}\ndata: {json.dumps(data)}\n\n"
                for event_id, event_type, data in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({"code": "NOT_FOUND", "error": "not found"}, 404)

    def do_POST(self):
        type(self).authorization = self.headers.get("Authorization", "")
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/flow/v1/runs":
            type(self).run_body = body
            self._json({"runId": "r1", "sessionId": "s1",
                        "eventsUrl": "/api/flow/v1/runs/r1/events"})
            return
        if self.path == "/api/flow/v1/runs/r1/cancel":
            self._json({"ok": True, "runId": "r1"})
            return
        self._json({"code": "NOT_FOUND", "error": "not found"}, 404)


@pytest.fixture(scope="module")
def flow_protocol_server():
    server = HTTPServer(("127.0.0.1", 0), _FlowProtocolHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_credential_store_masks_by_interface_and_uses_private_mode(tmp_path):
    path = tmp_path / "credentials.json"
    store = ExternalAgentCredentialStore(path)
    assert not store.configured("customer-agent-default")
    store.replace("customer-agent-default", "secret-token")
    assert store.configured("customer-agent-default")
    assert store.resolve("customer-agent-default") == "secret-token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    store.clear("customer-agent-default")
    assert not store.configured("customer-agent-default")


def test_catalog_and_run_use_flow_protocol(flow_protocol_server):
    provider = CustomerAgentProvider(flow_protocol_server, "service-token")
    catalog = provider.catalog()
    assert catalog["models"][0]["id"] == "p1"
    assert catalog["toolPolicies"] == [{"id": "local-readonly",
                                        "name": "Local read only"}]
    assert _FlowProtocolHandler.authorization == "Bearer service-token"

    seen = []
    out = provider.run(
        "find jobs", instructions="Only return public job data.",
        agent_id="existing-agent",
        context={"flowRunId": "flow-1"},
        selection={
            "modelId": "p1", "skillIds": ["job-hunt"],
            "activatedSkillIds": ["job-hunt"], "toolIds": ["read_file"],
            "mcpServerIds": [], "memoryEnabled": False,
            "toolPolicyId": "local-readonly",
        }, on_event=lambda event, data: seen.append((event, data)))
    assert out["text"] == "hello world"
    assert out["tool_calls"] == 1 and out["last_event_id"] == "5"
    assert _FlowProtocolHandler.run_body["instructions"] == "Only return public job data."
    assert _FlowProtocolHandler.run_body["agentId"] == "existing-agent"
    assert _FlowProtocolHandler.run_body["selection"]["toolIds"] == ["read_file"]
    assert _FlowProtocolHandler.run_body["selection"]["toolPolicyId"] == "local-readonly"
    assert [event for event, _ in seen][-1] == "run.completed"
    assert provider.cancel("r1") == {"ok": True, "runId": "r1"}


def test_remote_provider_requires_token():
    with pytest.raises(ExternalAgentError) as error:
        CustomerAgentProvider("https://ca.example.com")
    assert error.value.code == "UNAUTHORIZED"
