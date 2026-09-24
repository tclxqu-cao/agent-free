"""Agent 桥：把 customer-agent（AgentRoam）的推理接口接进流程图。

协议（customer-agent packages/server）：
- POST /api/agent/run  {input, sessionId?} -> {sessionId, streamUrl, runId}
- GET  /api/agent/stream?sessionId=...  (SSE)  终态事件：
    {type:"done", finalText} / {type:"error", message}
推理由用户自己的 agent 完成（带其配置的模型、工具、技能与记忆）。
"""

from __future__ import annotations

import json
import logging

import httpx

from .external_agent import resolve_external_agent_token

DEFAULT_BASE_URL = "http://127.0.0.1:3000"
log = logging.getLogger(__name__)


def agent_reason(cfg: dict, prompt: str, session_id: str | None = None, *,
                 agent_id: str | None = None, skill_name: str | None = None,
                 profile_id: str | None = None, project_id: str | None = None,
                 title: str | None = None, metadata: dict | None = None,
                 context: dict | None = None, source: str | None = None,
                 timeout: float = 300.0) -> dict:
    """跑一轮自己的 agent 推理，返回 {text, session_id, run_id, tool_calls,
    asked_user, duration_ms}。接口不可达 / agent 报错时抛异常（调用方决定降级）。"""
    cfg = cfg or {}
    base = str(cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    token = _agent_token(cfg)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    body: dict = {"input": prompt}
    if session_id:
        body["sessionId"] = session_id
    for key, value in {
        "agentId": agent_id, "skillName": skill_name,
        "profileId": profile_id, "projectId": project_id,
        "title": title, "metadata": metadata, "context": context,
        "source": source,
    }.items():
        if value is not None:
            body[key] = value

    log.info("向 team-agent 提交推理请求")
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
        resp = client.post(f"{base}/api/agent/run", headers=headers, json=body)
        if resp.status_code == 409:
            raise RuntimeError("该会话正在运行中（409），请稍后或换一个 sessionId")
        resp.raise_for_status()
        data = resp.json()
        sid = data["sessionId"]
        log.info("team-agent 已受理：session_id=%s，run_id=%s",
                 sid, data.get("runId"))
        stream_path = data.get("streamUrl") or f"/api/agent/stream?sessionId={sid}"

        final_text: str | None = None
        chunks: list[str] = []
        error: str | None = None
        tool_calls = 0
        tool_names: dict[str, str] = {}
        duration_ms = None

        stream_headers = {"Accept": "text/event-stream"}
        if token:
            stream_headers["Authorization"] = f"Bearer {token}"
        with client.stream("GET", f"{base}{stream_path}",
                           headers=stream_headers) as stream:
            stream.raise_for_status()
            buffered: list[str] = []
            for line in stream.iter_lines():
                if line.startswith("data: "):
                    buffered.append(line[6:])
                    continue
                if not line.strip() and buffered:      # 空行 = 事件边界
                    ev = _parse(buffered)
                    buffered = []
                    if ev is None:
                        continue
                    etype = ev.get("type")
                    if etype == "done":
                        final_text = str(ev.get("finalText") or "")
                        duration_ms = ev.get("durationMs")
                        break
                    if etype == "error":
                        error = str(ev.get("message") or "agent run error")
                        log.error("team-agent 返回执行错误，code=%s",
                                  ev.get("code") or "unknown")
                        break
                    if etype == "text_chunk" and \
                            ev.get("messagePhase") != "commentary":
                        chunks.append(str(ev.get("text") or ""))
                    elif etype == "tool_call":
                        tool_calls += 1
                        call = ev.get("toolCall") or {}
                        name = str(call.get("name") or "unknown")
                        tool_names[str(call.get("id") or "")] = name
                        log.info("team-agent 工具开始：%s", name)
                    elif etype == "tool_result":
                        result = ev.get("result") or {}
                        name = tool_names.get(str(result.get("toolCallId") or ""),
                                              "unknown")
                        if result.get("isError"):
                            log.warning("team-agent 工具失败：%s", name)
                        else:
                            log.info("team-agent 工具完成：%s", name)
                    elif etype == "ask_user":
                        log.info("team-agent 请求用户输入，本轮结束")
                        # 流程场景无人应答：把问题本身当作回复并标记
                        final_text = str(ev.get("question") or "agent 请求用户输入")
                        chunks.append(f"（ask_user）{final_text}")
                        break
                # 忽略注释行（: connected）与 id: 行
        if error:
            raise RuntimeError(f"Agent 推理失败：{error}")
        if final_text is None:
            final_text = "".join(chunks).strip()
        if not final_text:
            raise RuntimeError("Agent 推理结束但没有产出文本")
        log.info("team-agent 推理完成：工具调用 %s 次，耗时 %s ms",
                 tool_calls, duration_ms if duration_ms is not None else "未知")
        return {"text": final_text, "session_id": sid,
                "run_id": data.get("runId"), "tool_calls": tool_calls,
                "asked_user": False, "duration_ms": duration_ms}


def _agent_token(cfg: dict) -> str:
    return resolve_external_agent_token(cfg)


def _parse(lines: list[str]) -> dict | None:
    raw = "".join(lines).strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
