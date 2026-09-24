"""External Agent provider protocol and workspace-private credentials."""

from __future__ import annotations

import ipaddress
import json
import os
import threading
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit

import httpx


class ExternalAgentError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int | None = None,
                 remote_stack: str = ""):
        super().__init__(message)
        self.code = code
        self.status = status
        self.remote_stack = remote_stack


class ExternalAgentCredentialStore:
    """Stores provider tokens outside Agent snapshots and never returns them in lists."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def configured(self, credential_ref: str) -> bool:
        return bool(self.resolve(credential_ref))

    def resolve(self, credential_ref: str) -> str:
        ref = _credential_ref(credential_ref)
        with self._lock:
            value = self._read().get(ref) or {}
            return str(value.get("token") or "")

    def replace(self, credential_ref: str, token: str) -> None:
        ref = _credential_ref(credential_ref)
        value = str(token or "").strip()
        if not value or len(value) > 16_384:
            raise ValueError("token 必须是非空字符串且不超过 16384 字符")
        with self._lock:
            data = self._read()
            data[ref] = {"token": value}
            self._write(data)

    def clear(self, credential_ref: str) -> None:
        ref = _credential_ref(credential_ref)
        with self._lock:
            data = self._read()
            data.pop(ref, None)
            self._write(data)

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(self.path)
        os.chmod(self.path, 0o600)


class ExternalAgentProvider:
    def catalog(self) -> dict:
        raise NotImplementedError

    def run(self, message: str, **kwargs) -> dict:
        raise NotImplementedError

    def cancel(self, run_id: str) -> dict:
        raise NotImplementedError


class CustomerAgentProvider(ExternalAgentProvider):
    def __init__(self, base_url: str, token: str = "", *, timeout: float = 300.0):
        self.base_url = validate_provider_url(base_url)
        self.token = str(token or "").strip()
        self.timeout = max(1.0, min(float(timeout), 1800.0))
        if not _is_loopback(self.base_url) and not self.token:
            raise ExternalAgentError(
                "UNAUTHORIZED", "非本机 Customer Agent 地址必须配置访问令牌")

    def catalog(self) -> dict:
        with self._client(timeout=min(self.timeout, 30.0)) as client:
            response = client.get(f"{self.base_url}/api/flow/v1/catalog")
            data = self._json(response)
        if str(data.get("protocolVersion") or "") != "1":
            raise ExternalAgentError(
                "PROTOCOL_VERSION_UNSUPPORTED", "Customer Agent 不支持 Flow 协议 v1")
        for field in ("models", "skills", "tools", "mcpServers"):
            if not isinstance(data.get(field), list):
                raise ExternalAgentError("INVALID_RESPONSE", f"目录字段 {field} 不是数组")
            for item in data[field]:
                if not isinstance(item, dict) or not str(item.get("id") or "").strip():
                    raise ExternalAgentError("INVALID_RESPONSE", f"目录字段 {field} 含无效条目")
        if "toolPolicies" not in data:
            data["toolPolicies"] = []
        if not isinstance(data.get("toolPolicies"), list):
            raise ExternalAgentError("INVALID_RESPONSE", "目录字段 toolPolicies 不是数组")
        for item in data["toolPolicies"]:
            if not isinstance(item, dict) or not str(item.get("id") or "").strip():
                raise ExternalAgentError("INVALID_RESPONSE", "目录字段 toolPolicies 含无效条目")
        if not isinstance(data.get("features"), dict):
            raise ExternalAgentError("INVALID_RESPONSE", "目录缺少 features")
        return data

    def run(self, message: str, *, instructions: str = "", agent_id: str = "",
            session_id: str = "",
            context: dict | None = None, selection: dict | None = None,
            on_event: Callable[[str, dict], None] | None = None) -> dict:
        text = str(message or "").strip()
        if not text:
            raise ExternalAgentError("INVALID_REQUEST", "运行消息不能为空")
        body = {"input": text, "context": context or {}, "selection": selection or {}}
        if str(instructions or "").strip():
            body["instructions"] = str(instructions).strip()
        if agent_id:
            body["agentId"] = agent_id
        if session_id:
            body["sessionId"] = session_id
        with self._client() as client:
            response = client.post(f"{self.base_url}/api/flow/v1/runs", json=body)
            admitted = self._json(response)
            run_id = _required_response_string(admitted, "runId")
            resolved_session = _required_response_string(admitted, "sessionId")
            events_url = self._events_url(_required_response_string(admitted, "eventsUrl"))
            final_text = ""
            tool_calls = 0
            last_event_id = ""
            with client.stream("GET", events_url, headers={"Accept": "text/event-stream"}) as stream:
                if stream.status_code >= 400:
                    self._raise_response(stream)
                for event_id, event_type, data in _iter_sse(stream.iter_lines()):
                    if event_id:
                        last_event_id = event_id
                    if on_event:
                        on_event(event_type, data)
                    if event_type == "tool.started":
                        tool_calls += 1
                    elif event_type == "assistant.delta":
                        final_text += str(data.get("text") or "")
                    elif event_type == "run.completed":
                        final_text = str(data.get("text") or final_text)
                        return {
                            "text": final_text,
                            "run_id": run_id,
                            "session_id": resolved_session,
                            "tool_calls": tool_calls,
                            "duration_ms": data.get("durationMs"),
                            "last_event_id": last_event_id,
                        }
                    elif event_type == "run.failed":
                        raise ExternalAgentError(
                            str(data.get("code") or "RUN_FAILED"),
                            str(data.get("message") or "Customer Agent 运行失败"),
                            remote_stack=str(data.get("stack") or ""))
        raise ExternalAgentError("RUN_FAILED", "Customer Agent 事件流提前结束")

    def cancel(self, run_id: str) -> dict:
        value = str(run_id or "").strip()
        if not value:
            raise ExternalAgentError("INVALID_REQUEST", "run_id 不能为空")
        with self._client(timeout=min(self.timeout, 30.0)) as client:
            return self._json(client.post(
                f"{self.base_url}/api/flow/v1/runs/{value}/cancel"))

    def _client(self, *, timeout: float | None = None) -> httpx.Client:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return httpx.Client(
            headers=headers, follow_redirects=False,
            timeout=httpx.Timeout(timeout or self.timeout, connect=10.0))
    def _json(self, response: httpx.Response) -> dict:
        if response.status_code >= 400:
            self._raise_response(response)
        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ExternalAgentError(
                "INVALID_RESPONSE", "Customer Agent 返回了无效 JSON",
                status=response.status_code) from exc
        if not isinstance(data, dict):
            raise ExternalAgentError("INVALID_RESPONSE", "Customer Agent 返回值不是对象")
        return data

    def _raise_response(self, response) -> None:
        try:
            data = response.json()
        except Exception:  # noqa: BLE001 - remote response may not be JSON
            data = {}
        code = str(data.get("code") or (
            "UNAUTHORIZED" if response.status_code in {401, 403} else
            "PROVIDER_UNAVAILABLE" if response.status_code >= 500 else
            "INVALID_REQUEST"))
        message = str(data.get("error") or data.get("detail") or
                      f"Customer Agent HTTP {response.status_code}")[:2000]
        raise ExternalAgentError(code, message, status=response.status_code)

    def _events_url(self, value: str) -> str:
        resolved = urljoin(self.base_url + "/", value)
        expected = urlsplit(self.base_url)
        actual = urlsplit(resolved)
        if (expected.scheme, expected.hostname, expected.port) != (
                actual.scheme, actual.hostname, actual.port):
            raise ExternalAgentError(
                "INVALID_RESPONSE", "Customer Agent 返回了跨域事件地址")
        return resolved


def resolve_external_agent_token(config: dict | None = None) -> str:
    """Resolve the server-side CA token without persisting it in Agent JSON."""
    config = config or {}
    return (str(config.get("agent_token") or config.get("token")
                or config.get("portfolio_token") or "").strip()
            or os.environ.get("AGENT_RUN_TOKEN", "").strip()
            or os.environ.get("AGENT_SDK_TOKEN", "").strip()
            or os.environ.get("PORTFOLIO_SKILL_TOKEN", "").strip())


def validate_provider_url(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Customer Agent 地址必须是有效的 HTTP/HTTPS URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Customer Agent 地址不能包含用户信息或 fragment")
    return text


def _is_loopback(base_url: str) -> bool:
    host = (urlsplit(base_url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _credential_ref(value: str) -> str:
    ref = str(value or "").strip()
    if not ref or len(ref) > 200 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for c in ref):
        raise ValueError("credential_ref 无效")
    return ref


def _required_response_string(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ExternalAgentError("INVALID_RESPONSE", f"Customer Agent 响应缺少 {key}")
    return value.strip()


def _iter_sse(lines):
    event_id = ""
    event_type = "message"
    data_lines: list[str] = []
    for raw in lines:
        line = raw.decode() if isinstance(raw, bytes) else str(raw)
        if not line:
            if data_lines:
                try:
                    data = json.loads("\n".join(data_lines))
                except json.JSONDecodeError as exc:
                    raise ExternalAgentError("INVALID_RESPONSE", "事件流包含无效 JSON") from exc
                if not isinstance(data, dict):
                    raise ExternalAgentError("INVALID_RESPONSE", "事件数据不是对象")
                yield event_id, event_type, data
            event_id, event_type, data_lines = "", "message", []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "id":
            event_id = value
        elif field == "event":
            event_type = value
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        try:
            data = json.loads("\n".join(data_lines))
        except json.JSONDecodeError as exc:
            raise ExternalAgentError("INVALID_RESPONSE", "事件流包含无效 JSON") from exc
        if isinstance(data, dict):
            yield event_id, event_type, data
