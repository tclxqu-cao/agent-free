"""Langfuse 可观测性对接：零重依赖，直连 ingestion API 异步上报。

config.yaml 开关（也可用环境变量 LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY /
LANGFUSE_SECRET_KEY）：

    observability:
      langfuse:
        enabled: true
        host: https://cloud.langfuse.com
        public_key: pk-lf-...
        secret_key: sk-lf-...

映射关系：
- 流程 run      → trace（每节点一个 span，失败节点 level=ERROR）
- 智能体执行    → trace（RAG/工具步骤为 span，LLM 调用为 generation 含 token 用量）
- 评测结果      → 每个用例×目标一个 trace + score（评测通过率可在 Langfuse 看板统计）

上报走后台线程批量 POST {host}/api/public/ingestion（Basic 认证），
任何失败只记日志并丢弃，绝不阻塞或打断流程执行。
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import queue
import threading
import time
import uuid
from pathlib import Path

import httpx

log = logging.getLogger("flow_studio.observability")

_BATCH_MAX = 100
_FLUSH_WAIT = 2.0        # worker 攒批等待秒
_LOG_THROTTLE = 30.0     # 上报失败的日志节流秒


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _uid() -> str:
    return str(uuid.uuid4())


class LangfuseSink:
    """HTTP 上报通道：后台线程攒批发送，fire-and-forget。"""

    def __init__(self, host: str, public_key: str, secret_key: str):
        self.url = f"{(host or 'https://cloud.langfuse.com').rstrip('/')}/api/public/ingestion"
        token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}",
                         "Content-Type": "application/json"}
        self._queue: queue.Queue = queue.Queue()
        self._last_err = 0.0
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def emit(self, events: list[dict]) -> None:
        try:
            for e in events:
                self._queue.put(e)
        except Exception:  # noqa: BLE001 上报永不抛出
            pass

    def flush(self, timeout: float = 5.0) -> bool:
        """等待队列清空（测试/优雅退出用），超时返回 False。"""
        deadline = dt.datetime.now().timestamp() + timeout
        while self._queue.unfinished_tasks > 0:
            if dt.datetime.now().timestamp() > deadline:
                return False
            time.sleep(0.02)
        return True

    def _loop(self) -> None:
        batch: list[dict] = []
        while True:
            try:
                batch.append(self._queue.get(timeout=_FLUSH_WAIT))
                while len(batch) < _BATCH_MAX:
                    batch.append(self._queue.get_nowait())
            except queue.Empty:
                pass
            if batch:
                self._post(batch)
                for _ in batch:
                    self._queue.task_done()
                batch = []

    def _post(self, batch: list[dict]) -> None:
        try:
            r = httpx.post(self.url, json={"batch": batch, "metadata": {}},
                           headers=self._headers, timeout=10)
            if r.status_code not in (200, 201, 207):
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:  # noqa: BLE001 丢弃并节流记日志
            now = dt.datetime.now().timestamp()
            if now - self._last_err > _LOG_THROTTLE:
                self._last_err = now
                log.warning("Langfuse 上报失败（%s 条已丢弃）：%s", len(batch), e)


class NullObserver:
    """未启用时的空实现（所有方法零开销）。"""

    def flow_run(self, result: dict) -> None:  # noqa: D401
        pass

    def agent_run(self, agent: dict, message: str, out: dict,
                  session_id: str | None = None, flow_run_id: str | None = None,
                  llm_calls: list[dict] | None = None) -> None:
        pass

    def eval_run(self, run: dict) -> None:
        pass


class LangfuseObserver:
    def __init__(self, sink: LangfuseSink):
        self.sink = sink

    # ---------------------------------------------------------------- 流程 run
    def flow_run(self, result: dict) -> None:
        """流程 run → trace + 每节点 span。"""
        trace_id = _uid()
        started = result.get("started_at") or _now()
        events: list[dict] = [{
            "id": trace_id, "type": "trace", "timestamp": started,
            "name": f"flow:{result.get('flow_name') or result.get('flow_id')}",
            "input": result.get("input"),
            "output": (result.get("output") or "")[:4000],
            "metadata": {"run_id": result.get("run_id"),
                         "flow_id": result.get("flow_id"),
                         "status": result.get("status"),
                         "error": result.get("error")},
            "sessionId": result.get("run_id"),
        }]
        t0 = started
        for nrun in result.get("node_runs") or []:
            level = ("ERROR" if nrun.get("status") == "failed"
                     else "WARNING" if nrun.get("status") == "skipped"
                     else "DEFAULT")
            end = _now()
            events.append({
                "id": _uid(), "type": "span", "timestamp": t0, "traceId": trace_id,
                "name": f"{nrun.get('type')}:{nrun.get('label')}",
                "startTime": t0, "endTime": end,
                "metadata": {"node_id": nrun.get("node_id"), "status": nrun.get("status"),
                             "ms": nrun.get("ms"), "error": nrun.get("error"),
                             "output": _clip(nrun.get("output"))},
                "level": level,
                "statusMessage": (nrun.get("error") or "")[:500] or None,
            })
            t0 = end
        self.sink.emit(events)

    # ---------------------------------------------------------------- 智能体
    def agent_run(self, agent: dict, message: str, out: dict,
                  session_id: str | None = None, flow_run_id: str | None = None,
                  llm_calls: list[dict] | None = None) -> None:
        """智能体执行 → trace；rag/工具步骤为 span，LLM 调用为 generation。"""
        trace_id = _uid()
        started = _now()
        events: list[dict] = [{
            "id": trace_id, "type": "trace", "timestamp": started,
            "name": f"agent:{agent.get('name') or agent.get('id')}",
            "input": message[:4000],
            "output": (out.get("text") or "")[:4000],
            "metadata": {"agent_id": agent.get("id"), "session_id": session_id,
                         "flow_run_id": flow_run_id,
                         "tool_calls": out.get("tool_calls"),
                         "error": out.get("error")},
            "sessionId": session_id or out.get("session_id"),
        }]

        def _span_event(step: dict) -> dict:
            start = _now()
            return {"id": _uid(), "type": "span", "traceId": trace_id,
                    "name": step.get("name") or step.get("type"),
                    "startTime": start, "endTime": _now(), "timestamp": start,
                    "level": "DEFAULT" if step.get("ok") else "ERROR",
                    "statusMessage": None if step.get("ok")
                    else str(step.get("result") or "")[:500],
                    "metadata": {"type": step.get("type"),
                                 "args": _clip(step.get("args")),
                                 "result": _clip(step.get("result")),
                                 "ms": step.get("ms")}}

        for step in out.get("steps") or []:
            events.append(_span_event(step))
        for call in llm_calls or []:
            start = _now()
            events.append({
                "id": _uid(), "type": "generation", "traceId": trace_id,
                "name": f"llm:{call.get('model') or 'chat'}",
                "startTime": start, "endTime": _now(), "timestamp": start,
                "model": call.get("model"),
                "input": _clip(call.get("messages"), 4000),
                "output": _clip(call.get("output")),
                "usage": call.get("usage") or None,
                "metadata": {"ms": call.get("ms")},
                "level": "DEFAULT" if call.get("ok") is not False else "ERROR",
            })
        self.sink.emit(events)

    # ---------------------------------------------------------------- 评测
    def eval_run(self, run: dict) -> None:
        """评测结果 → 每个用例×目标一个 trace + score（pass=1/0）。"""
        events: list[dict] = []
        for r in run.get("results") or []:
            trace_id = _uid()
            name = f"eval:{run.get('suite_id')}:{r.get('case_id')}"
            events.append({
                "id": trace_id, "type": "trace", "timestamp": _now(),
                "name": name,
                "input": r.get("question"),
                "output": _clip(r.get("answer"), 2000),
                "metadata": {"suite_id": run.get("suite_id"),
                             "target": r.get("target_key"),
                             "case_id": r.get("case_id"),
                             "checks": r.get("checks"), "error": r.get("error"),
                             "ms": r.get("ms")},
                "sessionId": run.get("run_id"),
            })
            events.append({
                "id": _uid(), "type": "score", "timestamp": _now(),
                "traceId": trace_id, "name": "eval_pass",
                "value": 1 if r.get("pass") else 0, "dataType": "NUMERIC",
                "comment": r.get("error") or None,
            })
        self.sink.emit(events)


def _clip(value, limit: int = 1000):
    """metadata 里的对象截断（Langfuse metadata 值需为标量或可序列化）。"""
    if value is None:
        return None
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > limit:
        text = text[:limit] + "…"
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def make_observer(cfg: dict):
    """按 config 构造 observer；未启用返回 NullObserver。"""
    lf = (cfg or {}).get("langfuse") or {}
    import os

    host = lf.get("host") or os.environ.get("LANGFUSE_HOST")
    pk = lf.get("public_key") or os.environ.get("LANGFUSE_PUBLIC_KEY")
    sk = lf.get("secret_key") or os.environ.get("LANGFUSE_SECRET_KEY")
    if not (lf.get("enabled") or os.environ.get("LANGFUSE_ENABLED")):
        return NullObserver()
    if not (pk and sk):
        log.warning("Langfuse 已启用但缺少 public_key/secret_key，可观测性未上报")
        return NullObserver()
    log.info("Langfuse 可观测性已启用：%s", host or "https://cloud.langfuse.com")
    return LangfuseObserver(LangfuseSink(host, pk, sk))
