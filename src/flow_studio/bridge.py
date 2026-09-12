"""Agent 桥：把 customer-agent（AgentRoam）的推理接口接进流程图。

协议（customer-agent packages/server）：
- POST /api/agent/run  {input, sessionId?} -> {sessionId, streamUrl, runId}
- GET  /api/agent/stream?sessionId=...  (SSE)  终态事件：
    {type:"done", finalText} / {type:"error", message}
推理由用户自己的 agent 完成（带其配置的模型、工具、技能与记忆）。
"""

from __future__ import annotations

import json

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:3000"


def agent_reason(cfg: dict, prompt: str, session_id: str | None = None,
                 timeout: float = 300.0) -> dict:
    """跑一轮自己的 agent 推理，返回 {text, session_id, run_id, tool_calls,
    asked_user, duration_ms}。接口不可达 / agent 报错时抛异常（调用方决定降级）。"""
    cfg = cfg or {}
    base = str(cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if cfg.get("token"):
        headers["Authorization"] = f"Bearer {cfg['token']}"

    body: dict = {"input": prompt}
    if session_id:
        body["sessionId"] = session_id

    with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
        resp = client.post(f"{base}/api/agent/run", headers=headers, json=body)
        if resp.status_code == 409:
            raise RuntimeError("该会话正在运行中（409），请稍后或换一个 sessionId")
        resp.raise_for_status()
        data = resp.json()
        sid = data["sessionId"]
        stream_path = data.get("streamUrl") or f"/api/agent/stream?sessionId={sid}"

        final_text: str | None = None
        chunks: list[str] = []
        error: str | None = None
        tool_calls = 0
        duration_ms = None

        with client.stream("GET", f"{base}{stream_path}",
                           headers={"Accept": "text/event-stream"}) as stream:
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
                        break
                    if etype == "text_chunk" and \
                            ev.get("messagePhase") != "commentary":
                        chunks.append(str(ev.get("text") or ""))
                    elif etype == "tool_call":
                        tool_calls += 1
                    elif etype == "ask_user":
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
        return {"text": final_text, "session_id": sid,
                "run_id": data.get("runId"), "tool_calls": tool_calls,
                "asked_user": False, "duration_ms": duration_ms}


def _parse(lines: list[str]) -> dict | None:
    raw = "".join(lines).strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
