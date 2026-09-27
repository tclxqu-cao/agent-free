"""FastAPI 服务：流程 CRUD、运行、意图触发（chat / tools）与画布静态页。

注意：本模块不能启用 `from __future__ import annotations`——create_app 内
定义的 Pydantic 请求模型依赖真实注解对象；字符串注解会让 FastAPI 把
body 参数识别成 query 参数。项目要求 Python ≥3.11，原生支持 `X | Y` 注解。
"""


import argparse
import logging
import os
import re
import secrets
import uuid
import webbrowser
from pathlib import Path

from .adapters import register_job_agent
from .agentrt import normalize_agent
from .board import BoardError
from .bus import BROADCAST, HUMAN_ACTOR
from .crew import CrewError
from .evals import EvalStore, TargetRunner, compare_runs, run_suite
from .external_agent import CustomerAgentProvider, ExternalAgentError
from .graph import NODE_TYPES, FlowGraph, graph_from_dict, graph_to_dict, start_inputs
from .governance import (SESSION_COOKIE, GovernanceError, GovernanceStore,
                         Principal, ROLE_CAPABILITIES)
from .intent import parse_tool_call, route, tools_manifest
from .llm import llm_chat, llm_json
from .mcp_client import MCPError
from .observability import make_observer
from .policy import PolicyEngine
from .registry import AgentRegistry
from .template import build_namespace, render
from .versioning import VersionService
from .workforce import ROLE_TEMPLATES, build_agent_spec, compose, normalize_plan
from .workspace import WorkspaceManager, RunQueueFull
from .homepage import HomepageService

log = logging.getLogger("flow_studio")

WEB_DIR = Path(__file__).resolve().parent / "web"
DIGITAL_HUMAN_ASSET_DIR = WEB_DIR / "assets" / "digital-humans"
DIGITAL_HUMAN_ASSET_RE = re.compile(
    r"^employee-(?:0[1-6])-(?:seated|standing)\.webp$")
DEFAULT_PORT = 8788


class Studio:
    """应用上下文：存储 / 注册表 / 引擎 的组装点。"""

    def __init__(self, config_dir: Path, data_dir: Path | None = None):
        self.config_dir = Path(config_dir)
        cfg = self._load_config()
        self.config = cfg
        self.llm_cfg = cfg.get("llm") or {}
        base = Path(data_dir) if data_dir else Path(
            cfg.get("data_dir") or self.config_dir.parent / "data")
        self.data_root = base.resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.governance = GovernanceStore(self.data_root / "governance.sqlite")
        self.registry = AgentRegistry()
        register_job_agent(self.registry, self.config_dir)
        self.obs = make_observer(cfg.get("observability") or {})
        self.workspaces = WorkspaceManager(
            self.data_root, cfg, self.registry, self.obs, self.governance)
        self.policy = PolicyEngine()
        self.versions = VersionService(self.governance, self.workspaces, self.policy)
        self.homepage = HomepageService(self)

    def _load_config(self) -> dict:
        import yaml

        path = self.config_dir / "config.yaml"
        if not path.exists():
            return {}
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def runtime(self, principal: Principal):
        return self.workspaces.get(principal.workspace_id)


def _principal(request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise GovernanceError("authentication_required", "请先登录", 401)
    return principal


def _runtime(request):
    runtime = getattr(request.state, "runtime", None)
    if runtime is None:
        raise GovernanceError("workspace_required", "请选择 Workspace", 403)
    return runtime


def _required_capability(method: str, path: str) -> str | None:
    """Central route-to-capability policy; business state checks stay in services."""
    if path == "/api/auth/logout":
        return None
    if path.startswith("/api/workspaces/") and "/members" in path:
        return "member.manage"
    if path == "/api/workspaces" and method == "POST":
        return "workspace.manage"
    if path.startswith("/api/governance/audit"):
        return "audit.read"
    if path == "/api/ai-agents/external/catalog":
        return "resource.read"
    if path == "/api/governance/policy" and method != "GET":
        return "policy.manage"
    if path.startswith("/api/governance/resources/"):
        action = path.rsplit("/", 1)[-1]
        return {
            "submit": "resource.write", "approve": "release.approve",
            "reject": "release.approve", "publish": "release.publish",
            "rollback": "release.rollback", "preview": "runtime.preview",
        }.get(action, "resource.read" if method == "GET" else "resource.write")
    if path.startswith("/api/governance/approvals"):
        return "release.approve"
    if path.startswith("/api/evals/suites/") and path.endswith("/run"):
        return "eval.execute"
    if path.startswith("/api/workforce/"):
        if path.endswith("/message"):
            return "resource.write"
        if any(path.endswith(suffix) for suffix in
               ("/start", "/resume", "/pause", "/stop", "/cards")):
            return "runtime.execute" if path.endswith(
                ("/start", "/resume", "/pause", "/stop")) else "resource.write"
        if path.endswith("/step"):
            return "runtime.preview"
        return "resource.read" if method == "GET" else "resource.write"
    if ((path.startswith("/api/flows/") and path.endswith("/run"))
            or (path.startswith("/api/ai-agents/") and path.endswith("/invoke"))
            or path == "/api/agent/chat"
            or (path.startswith("/api/agent/tools/") and method == "POST")
            or path == "/api/kb/search"
            or (path.startswith("/api/mcp/") and path.endswith("/call"))):
        return "runtime.execute"
    if method == "GET":
        return "resource.read"
    if path.startswith("/api/mcp/") and path.endswith("/tools"):
        return "resource.read"
    if path.startswith("/api/"):
        return "resource.write"
    return "resource.read"


def _pos_int(value, default: int) -> int:
    """请求体里的正整数参数：非法值退回默认，不让脏输入变成 500。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _version_payload(version: dict) -> dict:
    payload = dict(version.get("snapshot") or {})
    payload["_governance"] = {
        "version": version.get("version_no"), "status": version.get("status"),
        "action": version.get("action"),
        "published_version": version.get("published_version"),
        "created_by": version.get("created_by"),
    }
    pending_delete = version.get("pending_delete")
    if isinstance(pending_delete, dict):
        payload["_governance"]["pending_delete"] = {
            "version": pending_delete.get("version_no"),
            "status": pending_delete.get("status"),
            "action": pending_delete.get("action"),
            "created_by": pending_delete.get("created_by"),
        }
    return payload


def _agent_credential_status(payload: dict, runtime) -> dict:
    orchestration = payload.get("orchestration") or {}
    if orchestration.get("mode") != "external_agent":
        return payload
    connection = orchestration.get("connection") or {}
    credential_ref = str(connection.get("credential_ref")
                         or "customer-agent-default")
    return {**payload,
            "credential_configured": runtime.external_credentials.configured(
                credential_ref)}


def _external_agent_error_response(error):
    from fastapi.responses import JSONResponse

    if isinstance(error, ExternalAgentError):
        status = error.status or (401 if error.code == "UNAUTHORIZED" else 422)
        if status >= 500:
            status = 504 if status == 504 else 502
        return JSONResponse({"detail": str(error), "code": error.code},
                            status_code=status)
    return JSONResponse({"detail": str(error), "code": "INVALID_REQUEST"},
                        status_code=422)


def create_app(config_dir: Path = Path("config"),
               data_dir: Path | None = None):
    """应用工厂（测试直接用 tmp 目录）。"""
    from fastapi import FastAPI, HTTPException, Request, Query
    from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
    from pydantic import BaseModel

    studio = Studio(Path(config_dir), data_dir)
    app = FastAPI(title="Flow Studio", docs_url=None, redoc_url=None)
    app.state.studio = studio

    @app.exception_handler(BoardError)
    async def board_error(request: Request, error: BoardError):
        request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
        return JSONResponse({"detail": str(error), "code": error.code,
                             "request_id": request_id}, status_code=409,
                            headers={"X-Request-ID": request_id})

    @app.exception_handler(CrewError)
    async def crew_error(request: Request, error: CrewError):
        request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
        return JSONResponse({"detail": str(error), "code": error.code,
                             "request_id": request_id}, status_code=409,
                            headers={"X-Request-ID": request_id})

    @app.exception_handler(RunQueueFull)
    async def queue_full(request: Request, error: RunQueueFull):
        return JSONResponse({"detail": str(error), "code": "run_queue_full"}, status_code=429)

    def error_response(error: GovernanceError, request_id: str):
        return JSONResponse(
            {"detail": error.message, "code": error.code,
             "request_id": request_id, **({"policy": error.details}
                                            if error.details else {})},
            status_code=error.status,
            headers={"X-Request-ID": request_id})

    @app.exception_handler(GovernanceError)
    async def governance_error(request: Request, error: GovernanceError):
        request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
        principal = getattr(request.state, "principal", None)
        if principal and error.details and error.details.get("decision") == "deny":
            studio.governance.audit(
                "policy.denied", workspace_id=principal.workspace_id,
                user_id=principal.user_id, username=principal.username,
                request_id=request_id, outcome="failed",
                details={"code": error.code, "path": request.url.path,
                         "policy": error.details})
        return error_response(error, request_id)

    @app.middleware("http")
    async def governance_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id
        path = request.url.path
        public = path in {
            "/", "/style.css", "/app.js", "/api/health", "/api/setup/status",
            "/api/setup", "/api/auth/login",
        }
        governed = path.startswith("/api/") or path.startswith("/media/")
        try:
            if path == "/api/homepage/command":
                if not studio.homepage.authorized(request.headers.get("Authorization", "")):
                    raise GovernanceError("authentication_required", "主页入口认证失败", 401)
            elif governed and not public:
                if not studio.governance.is_initialized():
                    raise GovernanceError(
                        "setup_required", "请先初始化 owner 账号", 503)
                principal = studio.governance.resolve_session(
                    request.cookies.get(SESSION_COOKIE, ""),
                    request.headers.get("X-Workspace-ID"), request_id)
                request.state.principal = principal
                request.state.runtime = studio.runtime(principal)
                if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                    supplied = request.headers.get("X-CSRF-Token", "")
                    if not supplied or supplied != principal.csrf_token:
                        raise GovernanceError(
                            "csrf_failed", "请求安全校验失败，请刷新后重试", 403)
                capability = _required_capability(request.method, path)
                if capability and not principal.can(capability):
                    raise GovernanceError(
                        "permission_denied", f"当前角色缺少权限：{capability}", 403)
            response = await call_next(request)
        except GovernanceError as error:
            return error_response(error, request_id)
        response.headers["X-Request-ID"] = request_id
        return response

    class FlowBody(BaseModel):
        id: str = ""
        name: str = ""
        description: str = ""
        triggers: list[str] = []
        nodes: list[dict] = []
        edges: list[dict] = []

    class RunBody(BaseModel):
        inputs: dict = {}
        background: bool = False

    class ChatBody(BaseModel):
        message: str
        background: bool = False

    class HomepageBody(BaseModel):
        message: str
        refresh: bool = False
        sessionId: str = ""

    @app.post("/api/homepage/command")
    def homepage_command(body: HomepageBody):
        message = body.message.strip()
        if not message or len(message) > 2000:
            raise HTTPException(422, "命令须为 1–2000 个字符")
        session_id = body.sessionId.strip()
        if session_id and (len(session_id) != 32
                           or any(char not in "0123456789abcdef" for char in session_id)):
            raise HTTPException(422, "页面会话标识无效")
        try:
            return JSONResponse(
                studio.homepage.run(
                    message, refresh=body.refresh, session_id=session_id),
                headers={"Cache-Control": "no-store"})
        except Exception:
            log.exception("主页主流程执行失败")
            return JSONResponse({"detail": "主页流程暂不可用"}, status_code=503,
                                headers={"Cache-Control": "no-store"})

    class LlmBody(BaseModel):
        system: str = "你是得力助手。"
        prompt: str

    def execute_flow(runtime, graph, inputs, principal, version_no,
                     background=False, event_type="runtime.flow", details=None,
                     metadata=None):
        metadata = {"version_no": version_no, **(metadata or {})}

        def completed(result):
            studio.governance.audit(
                event_type, workspace_id=principal.workspace_id,
                user_id=principal.user_id, username=principal.username,
                resource_type="flow", resource_id=graph.id, version_no=version_no,
                request_id=principal.request_id,
                outcome="success" if result.get("status") == "success" else "failed",
                details={"run_id": result["run_id"], "status": result.get("status"),
                         **(details or {})})

        if background:
            return runtime.start_flow(graph, inputs, on_complete=completed, metadata=metadata)
        result = runtime.run_flow(graph, inputs, metadata=metadata)
        completed(result)
        return result

    @app.get("/api/setup/status")
    def setup_status():
        return {"initialized": studio.governance.is_initialized()}

    @app.post("/api/setup")
    def setup(request: Request, body: dict):
        user = studio.governance.setup_owner(
            str(body.get("username") or ""), str(body.get("password") or ""),
            str(body.get("display_name") or ""))
        migration = studio.workspaces.migrate_legacy_default()
        session = studio.governance.create_session(user["user_id"])
        studio.governance.audit(
            "setup.completed", workspace_id="default", user_id=user["user_id"],
            username=user["username"], request_id=request.state.request_id,
            ip_address=request.client.host if request.client else "",
            details={"migration": migration})
        response = JSONResponse({"ok": True, "user": user,
                                 "workspace_id": "default",
                                 "csrf_token": session["csrf_token"]})
        response.set_cookie(
            SESSION_COOKIE, session["token"], httponly=True, samesite="strict",
            secure=request.url.scheme == "https", max_age=12 * 60 * 60, path="/")
        return response

    @app.post("/api/auth/login")
    def login(request: Request, body: dict):
        ip = request.client.host if request.client else ""
        username = str(body.get("username") or "").strip().lower()
        try:
            user = studio.governance.authenticate(
                username, str(body.get("password") or ""), ip)
        except GovernanceError as error:
            studio.governance.audit(
                "auth.login_failed", username=username,
                request_id=request.state.request_id, outcome="failed",
                ip_address=ip, details={"code": error.code})
            raise
        session = studio.governance.create_session(user["user_id"])
        workspaces = studio.governance.list_workspaces(user["user_id"])
        studio.governance.audit(
            "auth.login", workspace_id=workspaces[0]["workspace_id"] if workspaces else None,
            user_id=user["user_id"], username=user["username"],
            request_id=request.state.request_id, ip_address=ip)
        response = JSONResponse({"ok": True, "user": user,
                                 "workspaces": workspaces,
                                 "csrf_token": session["csrf_token"]})
        response.set_cookie(
            SESSION_COOKIE, session["token"], httponly=True, samesite="strict",
            secure=request.url.scheme == "https", max_age=12 * 60 * 60, path="/")
        return response

    @app.post("/api/auth/logout")
    def logout(request: Request):
        principal = _principal(request)
        studio.governance.revoke_session(request.cookies.get(SESSION_COOKIE, ""))
        studio.governance.audit(
            "auth.logout", workspace_id=principal.workspace_id,
            user_id=principal.user_id, username=principal.username,
            request_id=principal.request_id)
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    ENTRY_VIEWS = {"agents", "flows", "dh"}

    @app.get("/auth/entry")
    def auth_entry(request: Request, token: str = "", view: str = ""):
        """带共享 token 的免登录入口：验证 token 后以首个 owner 建会话并重定向。

        供外部入口（如 AgentRoam 的 Flow 按钮）跳转使用；链接即凭证，
        FLOW_STUDIO_ENTRY_TOKEN 未设置时入口整体关闭。
        """
        expected = (os.environ.get("FLOW_STUDIO_ENTRY_TOKEN") or "").strip()
        if not expected:
            raise HTTPException(404, "入口未启用")
        if not secrets.compare_digest(token.strip(), expected):
            raise HTTPException(403, "入口 token 无效")
        if not studio.governance.is_initialized():
            return RedirectResponse("/", status_code=303)
        owner = studio.governance.first_owner()
        if owner is None:
            return RedirectResponse("/", status_code=303)
        session = studio.governance.create_session(owner["user_id"])
        workspaces = studio.governance.list_workspaces(owner["user_id"])
        ip = request.client.host if request.client else ""
        studio.governance.audit(
            "auth.entry", workspace_id=workspaces[0]["workspace_id"] if workspaces else None,
            user_id=owner["user_id"], username=owner["username"],
            request_id=uuid.uuid4().hex, ip_address=ip)
        target = "/"
        if view in ENTRY_VIEWS:
            target = f"/?view={view}"
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            SESSION_COOKIE, session["token"], httponly=True, samesite="strict",
            secure=request.url.scheme == "https", max_age=12 * 60 * 60, path="/")
        return response

    @app.get("/api/me")
    def me(request: Request):
        principal = _principal(request)
        capabilities = sorted(ROLE_CAPABILITIES[principal.role])
        return {
            "user": {"user_id": principal.user_id, "username": principal.username,
                     "display_name": principal.display_name},
            "workspace": {"workspace_id": principal.workspace_id,
                          "name": principal.workspace_name,
                          "role": principal.role},
            "workspaces": studio.governance.list_workspaces(principal.user_id),
            "capabilities": capabilities, "csrf_token": principal.csrf_token,
        }

    # ---------------- Workspace / members / governance ----------------

    @app.get("/api/workspaces")
    def list_workspaces(request: Request):
        return studio.governance.list_workspaces(_principal(request).user_id)

    @app.post("/api/workspaces")
    def create_workspace(request: Request, body: dict):
        principal = _principal(request)
        created = studio.governance.create_workspace(
            str(body.get("id") or ""), str(body.get("name") or ""), principal.user_id)
        runtime = studio.workspaces.get(created["workspace_id"])
        studio.governance.audit(
            "workspace.created", workspace_id=created["workspace_id"],
            user_id=principal.user_id, username=principal.username,
            request_id=principal.request_id,
            details={"name": created["name"], "seeded_flows": len(runtime.flows.list())})
        return created

    def ensure_workspace_path(principal: Principal, workspace_id: str) -> None:
        if principal.workspace_id != workspace_id:
            raise GovernanceError(
                "workspace_mismatch", "路径 Workspace 与当前 Workspace 不一致", 403)

    @app.get("/api/workspaces/{workspace_id}/members")
    def list_members(workspace_id: str, request: Request):
        principal = _principal(request)
        ensure_workspace_path(principal, workspace_id)
        return studio.governance.list_members(workspace_id)

    @app.post("/api/workspaces/{workspace_id}/members")
    def add_member(workspace_id: str, request: Request, body: dict):
        principal = _principal(request)
        ensure_workspace_path(principal, workspace_id)
        member = studio.governance.add_member(
            workspace_id, str(body.get("username") or ""),
            str(body.get("display_name") or ""), str(body.get("password") or ""),
            str(body.get("role") or "viewer"), principal.role)
        studio.governance.audit(
            "member.added", workspace_id=workspace_id, user_id=principal.user_id,
            username=principal.username, request_id=principal.request_id,
            details={"member_user_id": member["user_id"], "role": member["role"]})
        return member

    @app.put("/api/workspaces/{workspace_id}/members/{user_id}")
    def update_member(workspace_id: str, user_id: str, request: Request, body: dict):
        principal = _principal(request)
        ensure_workspace_path(principal, workspace_id)
        member = studio.governance.update_member_role(
            workspace_id, user_id, str(body.get("role") or ""), principal.role)
        studio.governance.audit(
            "member.role_changed", workspace_id=workspace_id,
            user_id=principal.user_id, username=principal.username,
            request_id=principal.request_id,
            details={"member_user_id": user_id, "role": member["role"]})
        return member

    @app.delete("/api/workspaces/{workspace_id}/members/{user_id}")
    def remove_member(workspace_id: str, user_id: str, request: Request):
        principal = _principal(request)
        ensure_workspace_path(principal, workspace_id)
        studio.governance.remove_member(workspace_id, user_id, principal.role)
        studio.governance.audit(
            "member.removed", workspace_id=workspace_id,
            user_id=principal.user_id, username=principal.username,
            request_id=principal.request_id,
            details={"member_user_id": user_id})
        return {"deleted": user_id}

    @app.get("/api/governance/policy")
    def get_policy(request: Request):
        # 返回归一化策略：旧数据缺字段时前端也能看到完整可编辑的默认值
        return studio.policy.normalize(
            studio.governance.get_policy(_principal(request).workspace_id))

    @app.put("/api/governance/policy")
    def save_policy(request: Request, body: dict):
        principal = _principal(request)
        saved = studio.governance.save_policy(
            principal.workspace_id, studio.policy.normalize(body), principal.user_id)
        studio.governance.audit(
            "policy.updated", workspace_id=principal.workspace_id,
            user_id=principal.user_id, username=principal.username,
            request_id=principal.request_id, details={"policy": saved})
        return saved

    @app.get("/api/governance/audit")
    def governance_audit(request: Request, limit: int = 100):
        return studio.governance.list_audit(_principal(request).workspace_id, limit)

    @app.get("/api/governance/approvals")
    def governance_approvals(request: Request):
        principal = _principal(request)
        return [studio.versions.decorate(v) for v in
                studio.governance.list_pending(principal.workspace_id)]

    @app.get("/api/governance/resources/{resource_type}/{resource_id}/versions")
    def resource_versions(resource_type: str, resource_id: str, request: Request):
        principal = _principal(request)
        return [studio.versions.decorate(v) for v in studio.governance.list_versions(
            principal.workspace_id, resource_type, resource_id)]

    @app.post("/api/governance/resources/{resource_type}/{resource_id}/versions/{version_no}/submit")
    def submit_version(resource_type: str, resource_id: str, version_no: int,
                       request: Request, body: dict):
        return studio.versions.submit(
            _principal(request), resource_type, resource_id, version_no,
            str(body.get("reason") or ""))

    @app.post("/api/governance/resources/{resource_type}/{resource_id}/versions/{version_no}/approve")
    def approve_version(resource_type: str, resource_id: str, version_no: int,
                        request: Request, body: dict):
        return studio.versions.approve(
            _principal(request), resource_type, resource_id, version_no,
            str(body.get("reason") or ""))

    @app.post("/api/governance/resources/{resource_type}/{resource_id}/versions/{version_no}/reject")
    def reject_version(resource_type: str, resource_id: str, version_no: int,
                       request: Request, body: dict):
        return studio.versions.reject(
            _principal(request), resource_type, resource_id, version_no,
            str(body.get("reason") or ""))

    @app.post("/api/governance/resources/{resource_type}/{resource_id}/versions/{version_no}/publish")
    def publish_version(resource_type: str, resource_id: str, version_no: int,
                        request: Request, body: dict):
        return studio.versions.publish(
            _principal(request), resource_type, resource_id, version_no,
            str(body.get("reason") or ""))

    @app.post("/api/governance/resources/{resource_type}/{resource_id}/versions/{version_no}/rollback")
    def rollback_version(resource_type: str, resource_id: str, version_no: int,
                         request: Request, body: dict):
        return studio.versions.rollback(
            _principal(request), resource_type, resource_id, version_no,
            str(body.get("reason") or ""))

    @app.post("/api/governance/resources/{resource_type}/{resource_id}/versions/{version_no}/preview")
    def preview_version(resource_type: str, resource_id: str, version_no: int,
                        request: Request, body: dict):
        principal = _principal(request)
        version = studio.versions.preview_snapshot(
            principal, resource_type, resource_id, version_no)
        runtime = _runtime(request)
        if version.get("action") == "delete":
            raise GovernanceError("preview_delete", "删除版本不能预览运行", 409)
        if resource_type == "flow":
            graph = graph_from_dict(version["snapshot"])
            result = execute_flow(
                runtime, graph, body.get("inputs") or {}, principal, version_no,
                background=body.get("background") is True, event_type="runtime.preview",
                metadata={"preview": True})
            return JSONResponse(result, status_code=202) if body.get("background") is True else result
        else:
            result = runtime.agent_rt.run(
                version["snapshot"], str(body.get("message") or ""),
                session_id=body.get("session_id") or None,
                skill_id=body.get("skill_id") or None,
                context=body.get("context") if isinstance(body.get("context"), dict) else None)
        result["preview"] = True
        result["version_no"] = version_no
        studio.governance.audit(
            "runtime.preview", workspace_id=principal.workspace_id,
            user_id=principal.user_id, username=principal.username,
            resource_type=resource_type, resource_id=resource_id,
            version_no=version_no, request_id=principal.request_id,
            details={"status": result.get("status") or ("failed" if result.get("error") else "success")})
        return result

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/style.css")
    def style_css():
        return FileResponse(WEB_DIR / "style.css", media_type="text/css",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/app.js")
    def app_js():
        return FileResponse(WEB_DIR / "app.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/assets/digital-humans/{filename}")
    def digital_human_asset(filename: str):
        if not DIGITAL_HUMAN_ASSET_RE.fullmatch(filename):
            raise HTTPException(404, "数字人素材不存在")
        asset = DIGITAL_HUMAN_ASSET_DIR / filename
        if not asset.is_file():
            raise HTTPException(404, "数字人素材不存在")
        return FileResponse(
            asset,
            media_type="image/webp",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/node-types")
    def node_types(request: Request):
        return NODE_TYPES

    @app.get("/api/agents")
    def agents(request: Request):
        return studio.registry.agents()

    @app.get("/api/flows")
    def list_flows(request: Request):
        principal = _principal(request)
        out = []
        for version in studio.versions.list_effective(principal, "flow"):
            snapshot = version.get("snapshot") or {}
            ai_agent_ids = sorted({
                str((node.get("params") or {}).get("ai_agent_id") or "").strip()
                for node in snapshot.get("nodes") or []
                if node.get("type") == "ai_agent"
                and str((node.get("params") or {}).get("ai_agent_id") or "").strip()
            })
            out.append({"id": version["resource_id"],
                        "name": snapshot.get("name") or version["resource_id"],
                        "description": snapshot.get("description") or "",
                        "triggers": snapshot.get("triggers") or [],
                        "node_count": len(snapshot.get("nodes") or []),
                        "ai_agent_ids": ai_agent_ids,
                        "_governance": _version_payload(version)["_governance"]})
        return out

    @app.get("/api/flows/{flow_id}")
    def get_flow(flow_id: str, request: Request):
        version = studio.versions.effective_version(_principal(request), "flow", flow_id)
        if version is None:
            raise GovernanceError("flow_not_found", f"流程不存在：{flow_id}", 404)
        return _version_payload(studio.versions.decorate(version))

    @app.put("/api/flows/{flow_id}")
    def save_flow(flow_id: str, body: FlowBody, request: Request):
        data = body.model_dump()
        data["id"] = flow_id
        try:
            g = graph_from_dict(data)
        except ValueError as e:
            raise HTTPException(422, str(e))
        version = studio.versions.save_draft(
            _principal(request), "flow", flow_id, graph_to_dict(g))
        return _version_payload(version)

    @app.post("/api/flows")
    def create_flow(body: FlowBody, request: Request):
        principal = _principal(request)
        data = body.model_dump()
        current = studio.versions.list_effective(principal, "flow")
        data["id"] = data["id"] or _slug(body.name) or f"flow-{len(current) + 1}"
        if studio.governance.latest_version(principal.workspace_id, "flow", data["id"]):
            raise GovernanceError("flow_exists", f"流程 id 已存在：{data['id']}", 409)
        try:
            g = graph_from_dict(data)
        except ValueError as e:
            raise HTTPException(422, str(e))
        version = studio.versions.save_draft(
            principal, "flow", data["id"], graph_to_dict(g))
        return _version_payload(version)

    @app.delete("/api/flows/{flow_id}")
    def delete_flow(flow_id: str, request: Request):
        principal = _principal(request)
        if studio.versions.effective_version(principal, "flow", flow_id) is None:
            raise GovernanceError("flow_not_found", f"流程不存在：{flow_id}", 404)
        version = studio.versions.save_draft(
            principal, "flow", flow_id, None, action="delete")
        return {"delete_draft": flow_id, "_governance":
                _version_payload(version)["_governance"]}

    @app.post("/api/flows/{flow_id}/run")
    def run_flow(flow_id: str, body: RunBody, request: Request):
        principal = _principal(request)
        version = studio.versions.published_snapshot(
            principal.workspace_id, "flow", flow_id)
        g = graph_from_dict(version["snapshot"])
        missing = [i["key"] for i in start_inputs(g)
                   if i.get("required") and not body.inputs.get(i["key"])]
        if missing:
            raise HTTPException(422, f"缺少必填输入：{'、'.join(missing)}")
        result = execute_flow(_runtime(request), g, body.inputs, principal,
                              version["version_no"], background=body.background)
        return JSONResponse(result, status_code=202) if body.background else result

    @app.post("/api/agent/chat")
    def agent_chat(body: ChatBody, request: Request):
        principal = _principal(request)
        runtime = _runtime(request)
        graphs = runtime.flows.list()
        routed = route(body.message, graphs, runtime.llm_cfg)
        if not routed.matched:
            return {**routed.to_dict(), "run": None}
        version = studio.versions.published_snapshot(
            principal.workspace_id, "flow", routed.flow_id)
        graph = graph_from_dict(version["snapshot"])
        run = execute_flow(runtime, graph, routed.params, principal, version["version_no"],
                           background=body.background, event_type="runtime.chat",
                           details={"matched": True})
        reply = run.get("output") or routed.reply or f"流程「{graph.name}」执行完成"
        if body.background:
            reply = "流程已开始，正在执行"
        payload = {**routed.to_dict(), "run": run, "reply": reply}
        return JSONResponse(payload, status_code=202) if body.background else payload

    @app.get("/api/agent/tools")
    def agent_tools(request: Request):
        return tools_manifest(_runtime(request).flows.list())

    @app.post("/api/agent/tools/{tool_name}")
    def agent_tool_call(tool_name: str, body: dict, request: Request):
        principal = _principal(request)
        runtime = _runtime(request)
        flow, params = parse_tool_call(tool_name, body.get("arguments") or {},
                                       runtime.flows.list())
        if flow is None:
            raise HTTPException(404, f"工具不存在：{tool_name}")
        version = studio.versions.published_snapshot(
            principal.workspace_id, "flow", flow.id)
        return runtime.run_flow(graph_from_dict(version["snapshot"]), params)

    @app.get("/api/runs")
    def list_runs(request: Request, flow_id: str | None = None, limit: int = 30):
        return _runtime(request).runs.list(flow_id, limit=min(limit, 200))

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, request: Request):
        run = _runtime(request).runs.get(run_id)
        if run is None:
            raise HTTPException(404, f"运行记录不存在：{run_id}")
        return run

    @app.get("/api/runs/{run_id}/events")
    def run_events(run_id: str, request: Request,
                   after: int = Query(default=0, ge=0),
                   limit: int = Query(default=200, ge=1, le=500)):
        page = _runtime(request).runs.events(run_id, after=after, limit=limit)
        if page["run"] is None:
            raise HTTPException(404, "运行记录不存在")
        return JSONResponse(page, headers={"Cache-Control": "no-store"})

    # ---------------- 智能体平台：Agent / 知识库 / 记忆 / 技能 / MCP / 工具 ----------------

    @app.get("/api/ai-agents")
    def ai_agents(request: Request):
        runtime = _runtime(request)
        return [_agent_credential_status(
            _version_payload(studio.versions.decorate(v)), runtime) for v in
                studio.versions.list_effective(_principal(request), "agent")]

    @app.post("/api/ai-agents")
    def create_ai_agent(body: dict, request: Request):
        principal = _principal(request)
        try:
            snapshot = normalize_agent(body)
        except ValueError as e:
            raise HTTPException(422, str(e))
        if studio.governance.latest_version(
                principal.workspace_id, "agent", snapshot["id"]):
            raise GovernanceError("agent_exists", "智能体 id 已存在", 409)
        return _version_payload(studio.versions.save_draft(
            principal, "agent", snapshot["id"], snapshot))

    @app.post("/api/ai-agents/external/catalog")
    def external_agent_catalog(body: dict, request: Request):
        runtime = _runtime(request)
        provider = str(body.get("provider") or "customer-agent").strip()
        if provider != "customer-agent":
            return JSONResponse(
                {"detail": f"暂不支持第三方智能体：{provider}",
                 "code": "FEATURE_UNSUPPORTED"}, status_code=422)
        credential_ref = str(body.get("credential_ref")
                             or "customer-agent-default").strip()
        token = str(body.get("token") or "").strip()
        if not token:
            token = runtime.external_credentials.resolve(credential_ref)
        if not token:
            from .bridge import _agent_token
            token = _agent_token(runtime.bridge_cfg)
        try:
            catalog = CustomerAgentProvider(
                str(body.get("base_url") or "http://127.0.0.1:3000"), token,
                timeout=float(body.get("timeout") or 30)).catalog()
            return JSONResponse(catalog, headers={"Cache-Control": "no-store"})
        except (ExternalAgentError, ValueError) as error:
            return _external_agent_error_response(error)

    @app.get("/api/ai-agents/external/credentials/{credential_ref}")
    def external_agent_credential_status(credential_ref: str, request: Request):
        return {"credential_ref": credential_ref,
                "configured": _runtime(request).external_credentials.configured(
                    credential_ref)}

    @app.put("/api/ai-agents/external/credentials/{credential_ref}")
    def save_external_agent_credential(credential_ref: str, body: dict,
                                       request: Request):
        store = _runtime(request).external_credentials
        try:
            if body.get("clear") is True:
                store.clear(credential_ref)
            else:
                store.replace(credential_ref, str(body.get("token") or ""))
        except ValueError as error:
            return JSONResponse({"detail": str(error), "code": "INVALID_REQUEST"},
                                status_code=422)
        return {"credential_ref": credential_ref,
                "configured": store.configured(credential_ref)}

    @app.get("/api/ai-agents/{agent_id}")
    def get_ai_agent(agent_id: str, request: Request):
        version = studio.versions.effective_version(_principal(request), "agent", agent_id)
        if version is None:
            raise GovernanceError("agent_not_found", f"智能体不存在：{agent_id}", 404)
        return _agent_credential_status(
            _version_payload(studio.versions.decorate(version)), _runtime(request))

    @app.put("/api/ai-agents/{agent_id}")
    def save_ai_agent(agent_id: str, body: dict, request: Request):
        body["id"] = agent_id
        try:
            snapshot = normalize_agent(body)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return _version_payload(studio.versions.save_draft(
            _principal(request), "agent", agent_id, snapshot))

    @app.delete("/api/ai-agents/{agent_id}")
    def delete_ai_agent(agent_id: str, request: Request):
        principal = _principal(request)
        if studio.versions.effective_version(principal, "agent", agent_id) is None:
            raise GovernanceError("agent_not_found", f"智能体不存在：{agent_id}", 404)
        version = studio.versions.save_draft(
            principal, "agent", agent_id, None, action="delete")
        return {"delete_draft": agent_id, "_governance":
                _version_payload(version)["_governance"]}

    @app.post("/api/ai-agents/{agent_id}/invoke")
    def invoke_ai_agent(agent_id: str, body: dict, request: Request):
        """直接调试运行一个智能体（不走流程）。"""
        principal = _principal(request)
        version = studio.versions.published_snapshot(
            principal.workspace_id, "agent", agent_id)
        message = str(body.get("message") or "").strip()
        if not message:
            raise HTTPException(422, "message 不能为空")
        out = _runtime(request).agent_rt.run(
            version["snapshot"], message, session_id=body.get("session_id") or None,
            skill_id=body.get("skill_id") or None,
            context=body.get("context") if isinstance(body.get("context"), dict) else None)
        out["ok"] = not out.get("error")
        studio.governance.audit(
            "runtime.agent", workspace_id=principal.workspace_id,
            user_id=principal.user_id, username=principal.username,
            resource_type="agent", resource_id=agent_id,
            version_no=version["version_no"], request_id=principal.request_id,
            outcome="failed" if out.get("error") else "success",
            details={"session_id_present": bool(out.get("session_id")),
                     "tool_calls": out.get("tool_calls")})
        return out

    # ---------------- 知识库 ----------------

    @app.get("/api/kb")
    def kb_list(request: Request):
        return _runtime(request).kb.list_kbs()

    @app.post("/api/kb")
    def kb_create(body: dict, request: Request):
        try:
            return _runtime(request).kb.create_kb(
                str(body.get("id") or ""), str(body.get("name") or ""),
                str(body.get("description") or ""))
        except ValueError as e:
            raise HTTPException(409 if "已存在" in str(e) else 422, str(e))

    @app.delete("/api/kb/{kb_id}")
    def kb_delete(kb_id: str, request: Request):
        if not _runtime(request).kb.delete_kb(kb_id):
            raise HTTPException(404, f"知识库不存在：{kb_id}")
        return {"deleted": kb_id}

    @app.get("/api/kb/{kb_id}/docs")
    def kb_docs(kb_id: str, request: Request):
        return _runtime(request).kb.list_docs(kb_id)

    @app.post("/api/kb/{kb_id}/docs")
    def kb_add_doc(kb_id: str, body: dict, request: Request):
        try:
            return _runtime(request).kb.add_doc(
                kb_id, str(body.get("name") or "未命名文档"),
                str(body.get("text") or ""))
        except ValueError as e:
            raise HTTPException(404 if "不存在" in str(e) else 422, str(e))

    @app.post("/api/kb/{kb_id}/docs/upload")
    async def kb_upload_doc(kb_id: str, request: Request, name: str = ""):
        data = await request.body()
        if not data:
            raise HTTPException(422, "上传内容为空")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise HTTPException(422, "仅支持 UTF-8 文本文件（.md/.txt/.json/.csv/.html）") from e
        try:
            return _runtime(request).kb.add_doc(kb_id, name or "上传文档", text)
        except ValueError as e:
            raise HTTPException(404 if "不存在" in str(e) else 422, str(e))

    @app.delete("/api/kb/{kb_id}/docs/{doc_id}")
    def kb_delete_doc(kb_id: str, doc_id: str, request: Request):
        if not _runtime(request).kb.delete_doc(kb_id, doc_id):
            raise HTTPException(404, "文档不存在")
        return {"deleted": doc_id}

    @app.post("/api/kb/search")
    def kb_search(body: dict, request: Request):
        return _runtime(request).kb.search(
            str(body.get("query") or ""), kb_ids=body.get("kb_ids") or None,
            top_k=int(body.get("top_k") or 5))

    # ---------------- 记忆 ----------------

    @app.get("/api/memory/scopes")
    def memory_scopes(request: Request):
        return _runtime(request).memory.scopes()

    @app.get("/api/memory")
    def memory_list(request: Request, scope: str | None = None,
                    q: str | None = None, limit: int = 100):
        return _runtime(request).memory.list(
            scope=scope, q=q, limit=min(limit, 500))

    @app.post("/api/memory")
    def memory_set(body: dict, request: Request):
        try:
            return _runtime(request).memory.set(
                str(body.get("scope") or "global"),
                str(body.get("key") or ""), body.get("value"))
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.delete("/api/memory")
    def memory_delete(scope: str, key: str, request: Request):
        if not _runtime(request).memory.delete(scope, key):
            raise HTTPException(404, "记忆条目不存在")
        return {"deleted": key}

    # ---------------- 技能 ----------------

    @app.get("/api/skills")
    def skills_list(request: Request):
        return _runtime(request).skills.list()

    @app.get("/api/skills/{skill_id}")
    def skill_get(skill_id: str, request: Request):
        skill = _runtime(request).skills.get(skill_id)
        if skill is None:
            raise HTTPException(404, f"技能不存在：{skill_id}")
        return skill

    @app.put("/api/skills/{skill_id}")
    def skill_save(skill_id: str, body: dict, request: Request):
        try:
            return _runtime(request).skills.save(
                skill_id, str(body.get("name") or ""),
                str(body.get("description") or ""), str(body.get("content") or ""))
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.delete("/api/skills/{skill_id}")
    def skill_delete(skill_id: str, request: Request):
        if not _runtime(request).skills.delete(skill_id):
            raise HTTPException(404, f"技能不存在：{skill_id}")
        return {"deleted": skill_id}

    # ---------------- MCP ----------------

    @app.get("/api/mcp")
    def mcp_config(request: Request):
        return _runtime(request).mcp.load()

    @app.put("/api/mcp")
    def mcp_save(body: dict, request: Request):
        return _runtime(request).mcp.save(body)

    @app.post("/api/mcp/{name}/tools")
    def mcp_tools(name: str, request: Request):
        try:
            return _runtime(request).mcp.list_tools(name)
        except MCPError as e:
            raise HTTPException(502, str(e))

    @app.post("/api/mcp/{name}/call")
    def mcp_call(name: str, body: dict, request: Request):
        tool = str(body.get("tool") or "").strip()
        if not tool:
            raise HTTPException(422, "缺少 tool")
        try:
            return _runtime(request).mcp.call(
                name, tool, body.get("arguments") or {},
                timeout=float(body.get("timeout") or 120))
        except MCPError as e:
            raise HTTPException(502, str(e))

    # ---------------- 工具 ----------------

    @app.get("/api/tools")
    def tools_list(request: Request):
        runtime = _runtime(request)
        return [{"name": t.name, "description": t.description}
                for t in (runtime.tools.get(n) for n in runtime.tools.names()) if t]

    # ---------------- 评测中心 ----------------

    @app.get("/api/evals/suites")
    def eval_suites(request: Request):
        return _runtime(request).evals.list_suites()

    @app.post("/api/evals/suites")
    def eval_create_suite(body: dict, request: Request):
        try:
            return _runtime(request).evals.save_suite(body)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.get("/api/evals/suites/{suite_id}")
    def eval_get_suite(suite_id: str, request: Request):
        suite = _runtime(request).evals.get_suite(suite_id)
        if suite is None:
            raise HTTPException(404, f"评测集不存在：{suite_id}")
        return suite

    @app.put("/api/evals/suites/{suite_id}")
    def eval_save_suite(suite_id: str, body: dict, request: Request):
        body["id"] = suite_id
        try:
            return _runtime(request).evals.save_suite(body)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.delete("/api/evals/suites/{suite_id}")
    def eval_delete_suite(suite_id: str, request: Request):
        if not _runtime(request).evals.delete_suite(suite_id):
            raise HTTPException(404, f"评测集不存在：{suite_id}")
        return {"deleted": suite_id}

    @app.post("/api/evals/suites/{suite_id}/run")
    def eval_run(suite_id: str, body: dict, request: Request):
        runtime = _runtime(request)
        suite = runtime.evals.get_suite(suite_id)
        if suite is None:
            raise HTTPException(404, f"评测集不存在：{suite_id}")
        targets = body.get("targets") or []
        if not targets:
            raise HTTPException(422, "至少选择一个评测目标")
        try:
            run = run_suite(suite, targets, runtime.evals, runtime.eval_runner,
                            llm_judge=_make_llm_judge(studio.llm_cfg))
        except ValueError as e:
            raise HTTPException(422, str(e))
        try:
            studio.obs.eval_run(run)   # 评测结果上报 Langfuse（score 事件）
        except Exception:  # noqa: BLE001
            pass
        return run

    @app.get("/api/evals/runs")
    def eval_runs(request: Request, suite_id: str | None = None, limit: int = 20):
        return _runtime(request).evals.list_runs(suite_id, limit=min(limit, 100))

    @app.get("/api/evals/runs/{run_id}")
    def eval_get_run(run_id: str, request: Request):
        run = _runtime(request).evals.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"评测记录不存在：{run_id}")
        return run

    @app.get("/api/evals/compare")
    def eval_compare(request: Request, run_a: str, run_b: str):
        evals = _runtime(request).evals
        ra, rb = evals.get_run(run_a), evals.get_run(run_b)
        if ra is None or rb is None:
            raise HTTPException(404, "评测记录不存在")
        return compare_runs(ra, rb)

    @app.post("/api/llm/test")
    def llm_test(body: LlmBody, request: Request):
        """画布里快速验证 LLM 连通性。"""
        text = llm_chat(studio.llm_cfg, body.system, body.prompt)
        if text is None:
            return JSONResponse({"ok": False,
                                 "error": "LLM 未启用或调用失败（config.llm）"},
                                status_code=200)
        return {"ok": True, "text": text}

    @app.get("/api/template/preview")
    def template_preview(q: str, request: Request):
        """模板预览：仅内置 vars（today 等），无节点上下文。"""
        import datetime as dt

        ns = build_namespace({"today": dt.date.today().isoformat()}, {}, {})
        return {"rendered": render(q, ns)}

    # ---------------- 素材库 / 视频模型 ----------------
    @app.get("/api/assets")
    def list_assets(request: Request, kind: str | None = None,
                    flow_id: str | None = None, limit: int = 200):
        return _runtime(request).media.list(
            kind=kind or None, flow_id=flow_id or None, limit=min(limit, 1000))

    @app.post("/api/assets/upload")
    async def upload_asset(request: Request, name: str = "", kind: str = "file",
                           flow_id: str = "", ext: str = ""):
        """raw body 上传（不依赖 python-multipart）：前端 fetch 直接传 File。"""
        data = await request.body()
        if not data:
            raise HTTPException(422, "上传内容为空")
        if len(data) > 200 * 1024 * 1024:
            raise HTTPException(413, "文件超过 200MB 上限")
        suffix = (ext or "").strip()
        rec = _runtime(request).media.add_bytes(
            data, suffix, kind=kind or "file", name=name or "", flow_id=flow_id or "")
        return rec

    @app.delete("/api/assets/{asset_id}")
    def delete_asset(asset_id: str, request: Request):
        if not _runtime(request).media.delete(asset_id):
            raise HTTPException(404, f"素材不存在：{asset_id}")
        return {"deleted": asset_id}

    @app.get("/media/files/{fname}")
    def media_file(fname: str, request: Request):
        p = _runtime(request).media.file_path(fname)
        if p is None:
            raise HTTPException(404, "文件不存在")
        return FileResponse(p, headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/api/video/models")
    def get_video_models(request: Request):
        return _runtime(request).vmodels.load()

    @app.put("/api/video/models")
    def put_video_models(body: dict, request: Request):
        return _runtime(request).vmodels.save(body)


    # ---------------------------------------------------------- 数字员工编制
    def workforce_gate(principal: Principal, runtime, snapshot: dict,
                      policy: dict) -> None:
        result = studio.policy.evaluate("start", "workforce", snapshot,
                                        {"policy": policy})
        if not result.allowed:
            raise GovernanceError(result.primary_code, "策略门禁拒绝了该操作",
                                  403, result.to_dict())

    @app.get("/api/workforce/roles")
    def workforce_roles():
        return [{"key": role["key"], "name": role["name"], "icon": role["icon"],
                 "color": role["color"], "charter": role["charter"],
                 "deliverables": role["deliverables"],
                 "downstream": role["downstream"]}
                for role in ROLE_TEMPLATES.values()]

    @app.get("/api/workforce/teams")
    def workforce_teams(request: Request):
        runtime = _runtime(request)
        teams = runtime.teams.list_teams(_principal(request).workspace_id)
        return [dict(team, employee_count=len(runtime.teams.employees(team["id"])),
                     running=runtime.crew.running(team["id"])) for team in teams]

    @app.post("/api/workforce/teams")
    def workforce_create_team(body: dict, request: Request):
        principal = _principal(request)
        runtime = _runtime(request)
        task = str(body.get("task") or "").strip()
        if not task:
            raise HTTPException(422, "任务描述不能为空")
        policy = studio.governance.get_policy(principal.workspace_id)
        workforce_policy = studio.policy.normalize(policy)["workforce"]
        allowed = workforce_policy["allowed_roles"] or None
        try:
            plan = compose(task, studio.llm_cfg, allowed_roles=allowed)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        max_rounds = _pos_int(body.get("max_rounds"),
                              workforce_policy["max_rounds"])
        workforce_gate(principal, runtime, {
            "members": len(plan["roster"]), "cards": len(plan["milestones"]),
            "roles": [entry["role"] for entry in plan["roster"]],
            "max_rounds": max_rounds,
            "team_count": runtime.teams.count_teams(principal.workspace_id)},
            policy)
        team = runtime.teams.create_team(
            workspace_id=principal.workspace_id, task=task, plan=plan,
            created_by=principal.username, max_rounds=max_rounds)
        studio.governance.audit(
            "workforce.team.create", workspace_id=principal.workspace_id,
            user_id=principal.user_id, username=principal.username,
            resource_type="workforce", resource_id=team["id"],
            request_id=request.state.request_id,
            details={"via": plan.get("via"), "roles": [e["role"] for e in plan["roster"]],
                     "cards": len(plan["milestones"])})
        if workforce_policy["auto_create_agents"]:
            for entry in plan["roster"]:
                spec = build_agent_spec(entry["role"], task, entry.get("focus") or "")
                studio.versions.save_draft(principal, "agent", spec["id"], spec)
                studio.versions.submit(principal, "agent", spec["id"], 1,
                                       reason="数字员工编制自动创建")
        if body.get("start"):
            runtime.crew.start(team["id"], actor=principal.username,
                               max_rounds=max_rounds)
        return workforce_snapshot_payload(request, team["id"])

    @app.get("/api/workforce/teams/{team_id}/snapshot")
    def workforce_snapshot(team_id: str, request: Request):
        return workforce_snapshot_payload(request, team_id)

    @app.patch("/api/workforce/teams/{team_id}")
    def workforce_update_team(team_id: str, body: dict, request: Request):
        principal = _principal(request)
        runtime = _runtime(request)
        team = _require_team(runtime, team_id)
        if team["status"] not in ("draft", "paused"):
            raise GovernanceError("team_locked",
                                  f"团队当前 {team['status']}，不能修改编制", 409)
        task = team["task"]
        if "task" in body:
            task = str(body.get("task") or "").strip()
            if not task:
                raise HTTPException(422, "任务描述不能为空")
            runtime.teams.set_task(team_id, task)
        if isinstance(body.get("plan"), dict):
            try:
                plan = normalize_plan(task, body["plan"])
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            workforce_gate(principal, runtime, {
                "members": len(plan["roster"]), "cards": len(plan["milestones"]),
                "roles": [entry["role"] for entry in plan["roster"]],
                "max_rounds": team["max_rounds"], "team_count": 0},
                studio.governance.get_policy(principal.workspace_id))
            runtime.teams.set_team_plan(team_id, plan)
        studio.governance.audit(
            "workforce.team.update", workspace_id=principal.workspace_id,
            user_id=principal.user_id, username=principal.username,
            resource_type="workforce", resource_id=team_id,
            request_id=request.state.request_id, details={"fields": sorted(body)})
        return workforce_snapshot_payload(request, team_id)

    @app.post("/api/workforce/teams/{team_id}/start")
    def workforce_start(team_id: str, request: Request, body: dict | None = None):
        principal = _principal(request)
        runtime = _runtime(request)
        team = _require_team(runtime, team_id)
        max_rounds = (body or {}).get("max_rounds")
        policy = studio.governance.get_policy(principal.workspace_id)
        employees = runtime.teams.employees(team_id)
        # draft 团队还没 provision，编制只存在于计划里；按空编制过门禁等于放行
        roster = employees or team["plan"]["roster"]
        workforce_gate(principal, runtime, {
            "members": len(roster),
            "cards": max(len(runtime.board.list(team_id)),
                         len(team["plan"]["milestones"])),
            "roles": [entry["role"] for entry in roster],
            "max_rounds": int(max_rounds or team["max_rounds"]),
            "team_count": 0}, policy)
        return runtime.crew.start(team_id, actor=principal.username,
                                  max_rounds=max_rounds)

    @app.post("/api/workforce/teams/{team_id}/step")
    def workforce_step(team_id: str, request: Request):
        principal = _principal(request)
        runtime = _runtime(request)
        _require_team(runtime, team_id)
        return runtime.crew.step(team_id, actor=principal.username)

    @app.post("/api/workforce/teams/{team_id}/pause")
    def workforce_pause(team_id: str, request: Request):
        return _crew_action(request, team_id, "pause")

    @app.post("/api/workforce/teams/{team_id}/resume")
    def workforce_resume(team_id: str, request: Request):
        return _crew_action(request, team_id, "start")

    @app.post("/api/workforce/teams/{team_id}/stop")
    def workforce_stop(team_id: str, request: Request):
        return _crew_action(request, team_id, "stop")

    @app.post("/api/workforce/teams/{team_id}/message")
    def workforce_message(team_id: str, body: dict, request: Request):
        principal = _principal(request)
        runtime = _runtime(request)
        team = _require_team(runtime, team_id)
        policy = studio.policy.normalize(
            studio.governance.get_policy(principal.workspace_id))["workforce"]
        if not policy["allow_human_messages"]:
            raise GovernanceError("human_message_denied", "策略已禁止人工介入", 403)
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(422, "消息内容不能为空")
        employees = runtime.teams.employees(team_id)
        token = str(body.get("to") or "*").strip()
        recipient = next((e["id"] for e in employees
                          if token in (e["id"], e["role"], e["name"])), BROADCAST
                         if token in ("", "*", "all") else None)
        if recipient is None:
            raise HTTPException(422, f"收件人不存在：{token}")
        message = runtime.bus.post(
            workspace_id=principal.workspace_id, team_id=team_id,
            from_actor=HUMAN_ACTOR, to_actor=recipient, kind="human",
            subject=str(body.get("subject") or "老板指示")[:200],
            body={"text": text[:4000], "from_role": "human"},
            parent_seq=(int(body["parent_seq"])
                        if str(body.get("parent_seq") or "").isdigit() else None),
            round_no=int(team["round_no"]))
        studio.governance.audit(
            "workforce.message.human", workspace_id=principal.workspace_id,
            user_id=principal.user_id, username=principal.username,
            resource_type="workforce", resource_id=team_id,
            request_id=request.state.request_id,
            details={"to": recipient, "seq": message["seq"]})
        resumed = False
        if team["status"] in ("waiting_human", "paused") and body.get("resume", True):
            runtime.crew.start(team_id, actor=principal.username)
            resumed = True
        return {"ok": True, "seq": message["seq"], "resumed": resumed}

    @app.post("/api/workforce/teams/{team_id}/cards")
    def workforce_add_card(team_id: str, body: dict, request: Request):
        principal = _principal(request)
        runtime = _runtime(request)
        team = _require_team(runtime, team_id)
        role = str(body.get("role") or "").strip()
        if role and role not in ROLE_TEMPLATES:
            raise HTTPException(422, f"未知岗位：{role}")
        card = runtime.board.add(
            workspace_id=principal.workspace_id, team_id=team_id,
            title=str(body.get("title") or ""), role=role,
            detail=str(body.get("detail") or ""),
            depends_on=[str(d) for d in (body.get("depends_on") or [])],
            created_by=principal.username)
        runtime.bus.post(workspace_id=principal.workspace_id, team_id=team_id,
                         from_actor=HUMAN_ACTOR,
                         to_actor=next((e["id"] for e in runtime.teams.employees(team_id)
                                        if e["role"] == role), BROADCAST),
                         kind="assign", subject=f"老板加卡：{card['title'][:60]}",
                         body={"text": card["detail"] or "见看板"},
                         round_no=int(team["round_no"]))
        return {"ok": True, "card": card}

    @app.put("/api/workforce/teams/{team_id}/cards/{card_id}")
    def workforce_update_card(team_id: str, card_id: str, body: dict,
                              request: Request):
        principal = _principal(request)
        runtime = _runtime(request)
        _require_team(runtime, team_id)
        card = runtime.board.get(card_id)
        if card is None or card["team_id"] != team_id:
            raise HTTPException(404, "卡片不存在")
        if str(body.get("status") or "").strip():
            runtime.board.move(card_id, str(body["status"]).strip(),
                               principal.username, str(body.get("reason") or "人工迁移"))
        fields = {key: body[key] for key in
                  ("title", "detail", "role", "priority", "depends_on")
                  if key in body}
        if fields:
            card = runtime.board.update(card_id, principal.username, **fields)
        return {"ok": True, "card": runtime.board.get(card_id)}

    @app.get("/api/workforce/teams/{team_id}/inbox/{employee_id}")
    def workforce_inbox(team_id: str, employee_id: str, request: Request):
        runtime = _runtime(request)
        _require_team(runtime, team_id)
        # 广播对任意 actor 都命中：不校验工号，查无此人也能读到全团队广播并写已读
        if not any(e["id"] == employee_id
                   for e in runtime.teams.employees(team_id)):
            raise HTTPException(404, "员工不在本团队编制内")
        messages = runtime.bus.list(team_id, actor=employee_id, limit=100)
        return {"messages": messages,
                "unread": runtime.bus.mark_read_inbox(team_id, employee_id)}

    def workforce_snapshot_payload(request: Request, team_id: str) -> dict:
        runtime = _runtime(request)
        team = _require_team(runtime, team_id)
        employees = runtime.teams.employees(team["id"])
        rounds = runtime.teams.rounds(team["id"], limit=20)
        return {
            "team": team,
            "employees": [dict(emp, unread=runtime.bus.unread(team["id"], emp["id"]),
                               role_meta=ROLE_TEMPLATES.get(emp["role"], {}))
                          for emp in employees],
            "cards": runtime.board.list(team["id"]),
            "moves": runtime.board.history(team["id"], limit=80),
            "messages": runtime.bus.list(team["id"], limit=120),
            "edges": runtime.bus.pairs(team["id"]),
            "progress": runtime.board.progress(team["id"]),
            "rounds": rounds,
            "running": runtime.crew.running(team["id"]),
            "stats": runtime.bus.stats(team["id"]),
        }

    @app.get("/api/workforce/events")
    def workforce_events(request: Request, after: int = 0):
        import queue

        from fastapi.responses import StreamingResponse

        principal = _principal(request)
        runtime = _runtime(request)
        # Workspace 只能来自会话解析结果：接受客户端入参等于允许订阅他人工作区
        scope = principal.workspace_id

        def stream():
            sub = runtime.hub.subscribe()
            try:
                for message in runtime.bus.recent(workspace_id=scope,
                                                  after_seq=after):
                    yield _sse({"type": "message", "team_id": message["team_id"],
                                "id": message["seq"], "payload": message})
                idle = 0
                while True:
                    try:
                        event = sub.get(timeout=15)
                    except queue.Empty:
                        idle += 1
                        yield ": ping\n\n"
                        if idle > 40:
                            return
                        continue
                    idle = 0
                    if event.get("workspace_id") not in (scope, None):
                        continue
                    yield _sse(event)
            finally:
                runtime.hub.unsubscribe(sub)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    def _sse(event: dict) -> str:
        import json

        payload = json.dumps(event, ensure_ascii=False, default=str)
        ident = event.get("id")
        # 只有总线 seq 是单调游标；实体 id 当游标会让 Last-Event-ID 无法解析
        cursor = f"id: {ident}\n" if isinstance(ident, int) else ""
        return cursor + f"data: {payload}\n\n"

    def _crew_action(request: Request, team_id: str, action: str) -> dict:
        principal = _principal(request)
        runtime = _runtime(request)
        _require_team(runtime, team_id)
        handler = getattr(runtime.crew, action)
        return handler(team_id, actor=principal.username)

    def _require_team(runtime, team_id: str) -> dict:
        team = runtime.teams.get_team(team_id)
        if team is None:
            raise HTTPException(404, "团队不存在")
        return team

    return app


def _slug(name: str) -> str:
    import re

    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", (name or "").strip().lower()).strip("-")
    return s[:48]


def _make_llm_judge(llm_cfg: dict):
    """LLM 评判器：返回 fn(criteria, answer) -> (score, reason)；未启用返回 None。"""
    if not llm_cfg.get("enabled") or not llm_cfg.get("api_key"):
        return None

    def judge(criteria: str, answer: str) -> tuple[int, str]:
        out = llm_json(llm_cfg,
                       "你是严格的评测员。依据评分标准给回答打分（0-100）。"
                       "只输出 JSON：{\"score\": 数字, \"reason\": \"一句话理由\"}",
                       f"【评分标准】{criteria}\n【回答】{answer[:4000]}")
        if not out or "score" not in out:
            raise RuntimeError("LLM 评判返回异常")
        return int(out["score"]), str(out.get("reason") or "")

    return judge


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="flow-studio", description="Flow Studio：Agent 编排画布服务")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--data-dir", default=None,
                        help="覆盖运行数据目录（用于隔离部署或验收）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        app = create_app(Path(args.config_dir),
                         Path(args.data_dir) if args.data_dir else None)
    except ImportError:
        import sys

        sys.exit("缺少 Web 依赖，请先安装：uv sync --extra studio "
                 "（或 pip install 'fastapi>=0.110' 'uvicorn>=0.29'）")

    url = f"http://{args.host}:{args.port}"
    log.info("Flow Studio 画布：%s", url)
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
