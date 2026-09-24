"""Workspace-scoped Flow Studio runtime assembly and legacy data migration."""

from __future__ import annotations

import json
import datetime as dt
import logging
import shutil
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .agentrt import DEFAULT_AGENTS, AgentRuntime, AgentStore
from .assets import AssetStore
from .builtin_flows import builtin_flows
from .engine import FlowRunner, RunResult
from .external_agent import ExternalAgentCredentialStore
from .evals import DEFAULT_EVAL_SUITE, EvalStore, TargetRunner
from .graph import FlowGraph, graph_from_dict, graph_to_dict, start_inputs
from .intent import route
from .knowledge import KBStore
from .mcp_client import MCPManager
from .memory import MemoryStore
from .registry import AgentRegistry
from .run_events import redact_log
from .skills import DEFAULT_SKILLS, SkillStore
from .store import FlowStore, RunStore
from .tools import ToolRegistry, register_builtin_tools
from .video import VideoModels


_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="flow-run")
_run_slots = threading.BoundedSemaphore(16)
log = logging.getLogger(__name__)


class RunQueueFull(Exception):
    pass


class WorkspaceRuntime:
    """All mutable resources and execution services for one Workspace."""

    def __init__(self, workspace_id: str, root: Path, config: dict,
                 registry: AgentRegistry, observer):
        self.workspace_id = workspace_id
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.llm_cfg = config.get("llm") or {}
        self.bridge_cfg = config.get("agent_bridge") or {}
        self.registry = registry
        self.obs = observer

        self.flows = FlowStore(self.root / "flows")
        self.runs = RunStore(self.root / "flows" / "runs.sqlite")
        self.runs.interrupt_pending()
        self.media = AssetStore(self.root / "media")
        self.vmodels = VideoModels(self.root / "video_models.json")
        self.kb = KBStore(self.root / "kb")
        self.memory = MemoryStore(self.root / "memory.sqlite")
        self.skills = SkillStore(self.root / "skills")
        self.skills.seed_if_empty(DEFAULT_SKILLS)
        self.mcp = MCPManager(self.root / "mcp.json")
        self.tools = ToolRegistry()
        register_builtin_tools(self.tools, kb=self.kb, memory=self.memory,
                               skills=self.skills)
        self.ai_agents = AgentStore(self.root / "ai_agents")
        self.ai_agents.seed_missing(DEFAULT_AGENTS)
        self.external_credentials = ExternalAgentCredentialStore(
            self.root / "external_agent_credentials.json")
        self.agent_rt = AgentRuntime(
            self.llm_cfg, self.tools, skills=self.skills, memory=self.memory,
            kb=self.kb, mcp=self.mcp, obs=self.obs, flow_invoker=self._invoke_flow,
            bridge_cfg=self.bridge_cfg,
            credential_resolver=self.external_credentials.resolve)
        self.evals = EvalStore(self.root / "evals")
        self.evals.seed_if_empty(DEFAULT_EVAL_SUITE)
        self.eval_runner = TargetRunner(
            agent_rt=self.agent_rt, agent_store=self.ai_agents,
            flow_runner=self._eval_flow)
        self.flows.seed_missing(builtin_flows())

    def runner(self) -> FlowRunner:
        return FlowRunner(
            self.registry, self.llm_cfg, self.bridge_cfg, media=self.media,
            vmodels=self.vmodels, kb=self.kb, memory=self.memory, skills=self.skills,
            mcp=self.mcp, tools=self.tools, ai_agents=self.ai_agents,
            agent_rt=self.agent_rt, obs=self.obs)

    def _invoke_flow(self, flow_id: str, inputs: dict) -> dict:
        graph = self.flows.get(flow_id)
        if graph is None:
            raise ValueError(f"绑定的流程不存在：{flow_id}")
        merged = dict(inputs)
        declared = [item["key"] for item in start_inputs(graph) if item.get("key")]
        if declared and "message" not in declared and declared[0] not in merged:
            merged[declared[0]] = inputs.get("message", "")
        return self.run_flow(graph, merged)

    def _eval_flow(self, flow_id: str, question: str) -> dict:
        graph = self.flows.get(flow_id)
        if graph is None:
            raise ValueError(f"流程不存在：{flow_id}")
        declared = [item["key"] for item in start_inputs(graph) if item.get("key")]
        key = declared[0] if declared else "message"
        return self.run_flow(graph, {key: question, "message": question})

    def run_flow(self, flow: FlowGraph, inputs: dict | None = None, *,
                 run_id: str | None = None, metadata: dict | None = None) -> dict:
        def record(snapshot, event):
            self.runs.record({**snapshot, **(metadata or {})}, event)

        result = self.runner().run(flow, inputs, run_id=run_id, event_sink=record)
        data = result.to_dict()
        data.update(metadata or {})
        self.runs.append(data)
        return data

    def start_flow(self, flow: FlowGraph, inputs: dict | None = None,
                   on_complete=None, metadata: dict | None = None) -> dict:
        if not _run_slots.acquire(blocking=False):
            raise RunQueueFull("运行队列已满，请稍后再试")
        queued = RunResult(
            run_id=uuid.uuid4().hex[:12], flow_id=flow.id, flow_name=flow.name,
            status="queued", input=dict(inputs or {}), graph=graph_to_dict(flow),
            started_at=dt.datetime.now().isoformat(timespec="seconds")).to_dict()
        queued.update(metadata or {})

        def execute():
            try:
                try:
                    result = self.run_flow(flow, inputs, run_id=queued["run_id"], metadata=metadata)
                except Exception as exc:
                    result = self.runs.get(queued["run_id"]) or dict(queued)
                    result.update(status="failed", error=redact_log(f"{type(exc).__name__}: {exc}"),
                                  traceback=redact_log(traceback.format_exc()),
                                  finished_at=dt.datetime.now().isoformat(timespec="seconds"))
                    self.runs.record(result, {"type": "run.finished", "level": "error",
                                             "message": result["error"],
                                             "traceback": result["traceback"]})
                if on_complete:
                    try:
                        on_complete(result)
                    except Exception:
                        log.exception("运行 %s 完成审计写入失败", queued["run_id"])
            finally:
                _run_slots.release()

        try:
            self.runs.record(queued, {"type": "run.queued", "level": "info",
                                      "message": "流程已加入执行队列"})
            _executor.submit(execute)
        except Exception:
            _run_slots.release()
            raise
        return queued

    def chat(self, message: str) -> dict:
        graphs = self.flows.list()
        routed = route(message, graphs, self.llm_cfg)
        if not routed.matched:
            return {**routed.to_dict(), "run": None}
        flow = next(graph for graph in graphs if graph.id == routed.flow_id)
        run = self.run_flow(flow, routed.params)
        reply = run.get("output") or routed.reply or (
            f"流程「{flow.name}」执行{'失败' if run['status'] == 'failed' else '完成'}")
        return {**routed.to_dict(), "run": run, "reply": reply}


class WorkspaceManager:
    """Lazy WorkspaceRuntime cache with idempotent legacy migration."""

    MIGRATION_KEY = "legacy_to_default_v1"
    LEGACY_ITEMS = (
        "flows", "ai_agents", "kb", "skills", "evals", "media",
        "memory.sqlite", "mcp.json", "video_models.json",
        "external_agent_credentials.json",
    )

    def __init__(self, data_root: Path, config: dict, registry: AgentRegistry,
                 observer, governance):
        self.data_root = Path(data_root).resolve()
        self.workspaces_root = self.data_root / "workspaces"
        self.workspaces_root.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.registry = registry
        self.observer = observer
        self.governance = governance
        self._cache: dict[str, WorkspaceRuntime] = {}
        self._lock = threading.RLock()

    def _build(self, workspace_id: str) -> WorkspaceRuntime:
        return WorkspaceRuntime(
            workspace_id, self.workspaces_root / workspace_id, self.config,
            self.registry, self.observer)

    def get(self, workspace_id: str) -> WorkspaceRuntime:
        with self._lock:
            runtime = self._cache.get(workspace_id)
            if runtime is None:
                runtime = self._build(workspace_id)
                self._cache[workspace_id] = runtime
                self._register_materialized(workspace_id, runtime)
            return runtime

    def invalidate(self, workspace_id: str) -> None:
        with self._lock:
            runtime = self._cache.pop(workspace_id, None)
            if runtime:
                runtime.mcp.close_all()

    def migrate_legacy_default(self) -> dict:
        if not self.governance.is_initialized():
            return {"migrated": False, "reason": "setup_required"}
        if self.governance.migration_applied(self.MIGRATION_KEY):
            return {"migrated": False, "reason": "already_applied"}
        destination = self.workspaces_root / "default"
        destination.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        for name in self.LEGACY_ITEMS:
            source = self.data_root / name
            target = destination / name
            if not source.exists() or target.exists():
                continue
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
            copied.append(name)
        with self._lock:
            runtime = self._cache.get("default") or self._build("default")
            self._cache["default"] = runtime
        registered = self._register_materialized("default", runtime)
        details = {"copied": copied, "registered": registered}
        self.governance.mark_migration(self.MIGRATION_KEY, details)
        return {"migrated": True, **details}

    def _register_materialized(self, workspace_id: str,
                               runtime: WorkspaceRuntime) -> int:
        creator = self.governance.workspace_creator(workspace_id)
        count = 0
        for resource_type, items in (
            ("flow", [(g.id, graph_to_dict(g)) for g in runtime.flows.list()]),
            ("agent", [(a["id"], runtime.ai_agents.get(a["id"]))
                       for a in runtime.ai_agents.list()]),
        ):
            for resource_id, snapshot in items:
                if self.governance.get_release(workspace_id, resource_type, resource_id):
                    continue
                version = self.governance.save_version(
                    workspace_id, resource_type, resource_id, snapshot, "upsert", creator)
                version = self.governance.set_version_status(
                    version["version_id"], ("draft",), "pending", creator,
                    "Imported materialized resource")
                version = self.governance.set_version_status(
                    version["version_id"], ("pending",), "approved", creator,
                    "Imported materialized resource")
                self.governance.set_release(
                    version["version_id"], creator, "Imported materialized resource")
                count += 1
        return count
