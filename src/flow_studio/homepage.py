"""Authenticated public homepage entry backed by one orchestrating Agent."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from .agentrt import AgentRuntime, AgentStore, DEFAULT_AGENTS
from .engine import FlowRunner
from .graph import graph_from_dict, graph_to_dict
from .homepage_artifacts import HomepageArtifactStore, validate_artifact
from .homepage_flows import (COMMAND_SKILLS, HOMEPAGE_AGENT_ID,
                             HOMEPAGE_FLOW_REVISION, homepage_flows)
from .store import FlowStore, RunStore
from .tools import ToolRegistry

MAIN_FLOW_ID = "homepage-main"
DEPRECATED_HOMEPAGE_AGENT_IDS = {
    "homepage-chat-agent",
    "portfolio-content-agent",
}


class HomepageService:
    def __init__(self, studio):
        self.studio = studio
        self.config = studio.config.get("homepage") or {}
        self.token = os.environ.get("FLOW_HOMEPAGE_TOKEN") or self.config.get("token") or ""
        self.llm_cfg = {**studio.llm_cfg, "timeout": 8}
        self.slots = threading.BoundedSemaphore(4)
        self.resources_lock = threading.RLock()
        self.legacy_flows = FlowStore(studio.data_root / "flows")
        self.legacy_runs = RunStore(studio.data_root / "flows" / "runs.sqlite")
        self.legacy_agents = AgentStore(studio.data_root / "ai_agents")
        self.legacy_agents.seed_missing(DEFAULT_AGENTS)
        self._remove_deprecated_agents(self.legacy_agents)
        self.legacy_agent_rt = AgentRuntime(
            self.llm_cfg, ToolRegistry(), bridge_cfg=studio.config.get("agent_bridge") or {})
        self.artifacts = HomepageArtifactStore(studio.data_root / "homepage_artifacts")
        self._ensure_legacy_flow()
        if self.studio.governance.is_initialized():
            runtime = self.studio.workspaces.get("default")
            self._ensure_governed_resources(runtime)

    def authorized(self, authorization):
        return bool(self.token) and secrets.compare_digest(
            str(authorization), f"Bearer {self.token}")

    @staticmethod
    def _desired_graph():
        return graph_from_dict(homepage_flows()[0])

    def _ensure_legacy_flow(self):
        desired = self._desired_graph()
        current = self.legacy_flows.get(MAIN_FLOW_ID)
        if current is None or HOMEPAGE_FLOW_REVISION not in current.description:
            self.legacy_flows.save(desired)

    def _ensure_governed_flow(self, runtime):
        desired = self._desired_graph()
        release = self.studio.governance.get_release("default", "flow", MAIN_FLOW_ID)
        snapshot = (release or {}).get("snapshot") or {}
        if HOMEPAGE_FLOW_REVISION in str(snapshot.get("description") or ""):
            return
        actor = self.studio.governance.workspace_creator("default")
        version = self.studio.governance.save_version(
            "default", "flow", MAIN_FLOW_ID, graph_to_dict(desired), "upsert", actor)
        version = self.studio.governance.set_version_status(
            version["version_id"], ("draft",), "pending", actor,
            "Migrate homepage to one orchestrating Agent")
        version = self.studio.governance.set_version_status(
            version["version_id"], ("pending",), "approved", actor,
            "Migrate homepage to one orchestrating Agent")
        self.studio.governance.set_release(
            version["version_id"], actor, "Migrate homepage to one orchestrating Agent")
        runtime.flows.save(desired)

    @staticmethod
    def _remove_deprecated_agents(agents):
        for agent_id in DEPRECATED_HOMEPAGE_AGENT_IDS:
            agents.delete(agent_id)

    def _remove_deprecated_governed_agents(self, runtime):
        actor = self.studio.governance.workspace_creator("default")
        for agent_id in DEPRECATED_HOMEPAGE_AGENT_IDS:
            release = self.studio.governance.get_release(
                "default", "agent", agent_id)
            if release and release.get("action") != "delete":
                version = self.studio.governance.save_version(
                    "default", "agent", agent_id, None, "delete", actor)
                version = self.studio.governance.set_version_status(
                    version["version_id"], ("draft",), "pending", actor,
                    "Remove deprecated homepage Agent")
                version = self.studio.governance.set_version_status(
                    version["version_id"], ("pending",), "approved", actor,
                    "Remove deprecated homepage Agent")
                self.studio.governance.set_release(
                    version["version_id"], actor,
                    "Remove deprecated homepage Agent")
            runtime.ai_agents.delete(agent_id)

    def _ensure_governed_resources(self, runtime):
        self._remove_deprecated_governed_agents(runtime)
        self._ensure_governed_flow(runtime)

    def _resources(self):
        if self.studio.governance.is_initialized():
            runtime = self.studio.workspaces.get("default")
            with self.resources_lock:
                self._ensure_governed_resources(runtime)
            return runtime.flows, runtime.runs, runtime.ai_agents, runtime.agent_rt
        return (self.legacy_flows, self.legacy_runs,
                self.legacy_agents, self.legacy_agent_rt)

    def _graph(self):
        flows, _runs, agents, _agent_rt = self._resources()
        if self.studio.governance.is_initialized():
            version = self.studio.versions.published_snapshot(
                "default", "flow", MAIN_FLOW_ID)
            graph = graph_from_dict(version["snapshot"])
        else:
            graph = flows.get(MAIN_FLOW_ID)
        if graph is None:
            raise ValueError("主页流程未配置")
        homepage_agent = agents.get(HOMEPAGE_AGENT_ID)
        orchestration = (homepage_agent or {}).get("orchestration") or {}
        selection = orchestration.get("selection") or {}
        selected_skills = set(selection.get("skill_ids") or [])
        homepage_safe = (
            orchestration.get("mode") == "external_agent"
            and orchestration.get("provider") == "customer-agent"
            and not orchestration.get("agent_id")
            and bool(selection.get("model_id"))
            and bool(selection.get("tool_policy_id"))
            and selection.get("memory_enabled") is False
        )
        for node in graph.nodes:
            if node.type not in {"start", "end", "condition", "ai_agent"}:
                raise ValueError("主页流程包含未授权节点")
            if node.type == "ai_agent":
                skill = str(node.params.get("skill_id") or "")
                agent_id = node.params.get("ai_agent_id")
                if (agent_id == HOMEPAGE_AGENT_ID and homepage_safe
                        and skill and skill in selected_skills):
                    continue
                raise ValueError("主页流程包含未授权的智能体能力")
        return graph

    def _execute(self, message, expected_skill=None, session_id=""):
        graph = self._graph()
        _flows, runs, agents, agent_rt = self._resources()
        flow_input = {"message": message,
                      "is_slash": self._normalized(message).startswith("/"),
                      "job_snapshot": self._job_snapshot(),
                      "session_id": f"homepage-{session_id}" if session_id else ""}
        result = FlowRunner(
            ai_agents=agents, agent_rt=agent_rt).run(
                graph, flow_input, event_sink=runs.record).to_dict()
        runs.append(result)
        if result["status"] != "success":
            raise RuntimeError("主页流程执行失败")
        payload = json.loads(result["output"])
        artifact = validate_artifact(payload, expected_skill)
        return {**artifact, "run_id": result["run_id"], "flow_id": MAIN_FLOW_ID}

    def run(self, message, *, refresh=False, session_id=""):
        if not self.slots.acquire(blocking=False):
            raise RuntimeError("主页流程正忙")
        try:
            skill = self._cache_skill(message)
            # Validate the complete published graph before entering the fallback
            # boundary. A poisoned graph must fail closed, never serve cached data.
            self._graph()
            cached = self.artifacts.get(skill) if skill else None
            if cached is not None and not refresh:
                return {**cached, "flow_id": MAIN_FLOW_ID, "cached": True}
            try:
                artifact = self._execute(message, skill, session_id)
                if skill:
                    self.artifacts.save(artifact, skill)
                return artifact
            except Exception:
                if cached is None:
                    raise
                return {**cached, "flow_id": MAIN_FLOW_ID, "fallback": True,
                        "notice": "Live Skill is unavailable; showing the latest generated snapshot."}
        finally:
            self.slots.release()

    @staticmethod
    def _normalized(message):
        return " ".join(str(message or "").strip().lower().split())

    def _cache_skill(self, message):
        normalized = self._normalized(message)
        if not normalized.startswith("/"):
            return None
        command = normalized[1:]
        if command in COMMAND_SKILLS:
            return COMMAND_SKILLS[command]
        if command.startswith("project "):
            project = command.removeprefix("project ")
            if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", project):
                return f"portfolio-project-{project}"
        return None

    def _job_snapshot(self):
        from job_agent.db import row_to_job
        from job_agent.match import filter_and_score
        from job_agent.models import load_config, load_profile

        config = load_config(self.studio.config_dir)
        db_path = Path(config.get("data_dir") or self.studio.config_dir.parent / "data") / "job_agent.db"
        empty = {"snapshotDate": None, "count": 0, "jobs": [],
                 "warning": "暂无可用的真实岗位快照。"}
        if not db_path.exists():
            return empty
        with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            day = conn.execute(
                "SELECT MAX(date) FROM job_daily WHERE job_id IN "
                "(SELECT job_id FROM jobs WHERE site NOT LIKE 'demo%')").fetchone()[0]
            if not day:
                return empty
            rows = conn.execute(
                "SELECT j.*, d.distance_km AS snapshot_distance FROM jobs j "
                "JOIN job_daily d ON d.job_id=j.job_id WHERE d.date=? "
                "AND d.status='active' AND j.site NOT LIKE 'demo%'", (day,)).fetchall()
        jobs = []
        for row in rows:
            job = row_to_job(row)
            job.raw["distance_km"] = row["snapshot_distance"]
            jobs.append(job)
        matched = [item for item in filter_and_score(
            jobs, config.get("rules") or {}, load_profile(self.studio.config_dir))
                   if item.passed]
        public_jobs = [{
            "title": item.job.title, "company": item.job.company,
            "salary": item.job.salary_text or "", "city": item.job.city or "",
            "experience": item.job.experience_text or "",
            "education": item.job.education or "", "url": item.job.url,
            "responsibilities": item.job.responsibilities[:5],
            "requirements": item.job.requirements_extra[:5],
        } for item in matched[:20]]
        return {
            "snapshotDate": day, "count": len(matched), "jobs": public_jobs,
            "warning": ("该快照不是今天生成的，请在查看时核实岗位当前状态。"
                        if day != dt.date.today().isoformat() else ""),
        }
