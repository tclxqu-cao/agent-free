"""FastAPI 服务：流程 CRUD、运行、意图触发（chat / tools）与画布静态页。

注意：本模块不能启用 `from __future__ import annotations`——create_app 内
定义的 Pydantic 请求模型依赖真实注解对象；字符串注解会让 FastAPI 把
body 参数识别成 query 参数。项目要求 Python ≥3.11，原生支持 `X | Y` 注解。
"""


import argparse
import logging
import webbrowser
from pathlib import Path

from .adapters import register_job_agent
from .agentrt import DEFAULT_AGENTS, AgentRuntime, AgentStore
from .assets import AssetStore
from .builtin_flows import builtin_flows
from .engine import FlowRunner
from .evals import EvalStore, TargetRunner, compare_runs, run_suite
from .graph import NODE_TYPES, FlowGraph, graph_from_dict, graph_to_dict, start_inputs
from .intent import parse_tool_call, route, tools_manifest
from .knowledge import KBStore
from .llm import llm_chat, llm_json
from .mcp_client import MCPError, MCPManager
from .memory import MemoryStore
from .observability import make_observer
from .registry import AgentRegistry
from .skills import DEFAULT_SKILLS, SkillStore
from .store import FlowStore, RunStore
from .template import build_namespace, render
from .tools import ToolRegistry, register_builtin_tools
from .video import VideoModels

log = logging.getLogger("flow_studio")

WEB_DIR = Path(__file__).resolve().parent / "web"
DEFAULT_PORT = 8788


class Studio:
    """应用上下文：存储 / 注册表 / 引擎 的组装点。"""

    def __init__(self, config_dir: Path, data_dir: Path | None = None):
        self.config_dir = Path(config_dir)
        cfg = self._load_config()
        self.llm_cfg = cfg.get("llm") or {}
        self.bridge_cfg = cfg.get("agent_bridge") or {}
        base = Path(data_dir) if data_dir else Path(
            cfg.get("data_dir") or self.config_dir.parent / "data")
        base = base.resolve()   # 相对路径一律转绝对：ffmpeg/子进程按各自 CWD 解析
        self.flows = FlowStore(base / "flows")
        self.runs = RunStore(base / "flows" / "runs.sqlite")
        self.media = AssetStore(base / "media")
        self.vmodels = VideoModels(base / "video_models.json")
        self.registry = AgentRegistry()
        register_job_agent(self.registry, self.config_dir)
        # ---- 智能体平台：知识库 / 记忆 / 技能 / MCP / 工具 / 智能体 ----
        self.kb = KBStore(base / "kb")
        self.memory = MemoryStore(base / "memory.sqlite")
        self.skills = SkillStore(base / "skills")
        self.skills.seed_if_empty(DEFAULT_SKILLS)
        self.mcp = MCPManager(base / "mcp.json")
        self.tools = ToolRegistry()
        register_builtin_tools(self.tools, kb=self.kb, memory=self.memory,
                               skills=self.skills)
        self.ai_agents = AgentStore(base / "ai_agents")
        self.ai_agents.seed_if_empty(DEFAULT_AGENTS)
        # ---- 可观测性（Langfuse，未配置则为空实现） ----
        self.obs = make_observer(cfg.get("observability") or {})
        self.agent_rt = AgentRuntime(self.llm_cfg, self.tools, skills=self.skills,
                                     memory=self.memory, kb=self.kb, mcp=self.mcp,
                                     obs=self.obs,
                                     flow_invoker=self._invoke_flow)
        # ---- 评测中心 ----
        self.evals = EvalStore(base / "evals")
        self.evals.seed_if_empty(DEFAULT_EVAL_SUITE)
        self.eval_runner = TargetRunner(agent_rt=self.agent_rt,
                                        agent_store=self.ai_agents)
        self.flows.seed_missing(builtin_flows())

    def _load_config(self) -> dict:
        import yaml

        path = self.config_dir / "config.yaml"
        if not path.exists():
            return {}
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def runner(self) -> FlowRunner:
        return FlowRunner(self.registry, self.llm_cfg, self.bridge_cfg,
                          media=self.media, vmodels=self.vmodels, kb=self.kb,
                          memory=self.memory, skills=self.skills, mcp=self.mcp,
                          tools=self.tools, ai_agents=self.ai_agents,
                          agent_rt=self.agent_rt, obs=self.obs)

    def _invoke_flow(self, flow_id: str, inputs: dict) -> dict:
        """供绑定流程的智能体调用：按 id 找流程并同步执行。

        输入名适配：消息固定传 input.message；若流程声明的输入变量里没有
        message，则把消息同时别名到声明的第一个输入（如 question）。
        """
        g = self.flows.get(flow_id)
        if g is None:
            raise ValueError(f"绑定的流程不存在：{flow_id}")
        merged = dict(inputs)
        declared = [i["key"] for i in start_inputs(g) if i.get("key")]
        if declared and "message" not in declared and declared[0] not in merged:
            merged[declared[0]] = inputs.get("message", "")
        return self.run_flow(g, merged)

    def run_flow(self, flow: FlowGraph, inputs: dict | None = None) -> dict:
        result = self.runner().run(flow, inputs)
        data = result.to_dict()
        self.runs.append(data)
        return data

    def chat(self, message: str) -> dict:
        graphs = self.flows.list()
        r = route(message, graphs, self.llm_cfg)
        if not r.matched:
            return {**r.to_dict(), "run": None}
        flow = next(g for g in graphs if g.id == r.flow_id)
        run = self.run_flow(flow, r.params)
        reply = run.get("output") or r.reply or (
            f"流程「{flow.name}」执行{ '失败' if run['status'] == 'failed' else '完成' }")
        return {**r.to_dict(), "run": run, "reply": reply}


def create_app(config_dir: Path = Path("config"),
               data_dir: Path | None = None):
    """应用工厂（测试直接用 tmp 目录）。"""
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, JSONResponse
    from pydantic import BaseModel

    studio = Studio(Path(config_dir), data_dir)
    app = FastAPI(title="Flow Studio", docs_url=None, redoc_url=None)

    class FlowBody(BaseModel):
        id: str = ""
        name: str = ""
        description: str = ""
        triggers: list[str] = []
        nodes: list[dict] = []
        edges: list[dict] = []

    class RunBody(BaseModel):
        inputs: dict = {}

    class ChatBody(BaseModel):
        message: str

    class LlmBody(BaseModel):
        system: str = "你是得力助手。"
        prompt: str

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

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/node-types")
    def node_types():
        return NODE_TYPES

    @app.get("/api/agents")
    def agents():
        return studio.registry.agents()

    @app.get("/api/flows")
    def list_flows():
        return [g.meta() for g in studio.flows.list()]

    @app.get("/api/flows/{flow_id}")
    def get_flow(flow_id: str):
        g = studio.flows.get(flow_id)
        if g is None:
            raise HTTPException(404, f"流程不存在：{flow_id}")
        return graph_to_dict(g)

    @app.put("/api/flows/{flow_id}")
    def save_flow(flow_id: str, body: FlowBody):
        data = body.model_dump()
        data["id"] = flow_id
        try:
            g = graph_from_dict(data)
        except ValueError as e:
            raise HTTPException(422, str(e))
        studio.flows.save(g)
        return graph_to_dict(g)

    @app.post("/api/flows")
    def create_flow(body: FlowBody):
        data = body.model_dump()
        data["id"] = data["id"] or _slug(body.name) or f"flow-{len(studio.flows.list()) + 1}"
        if studio.flows.get(data["id"]) is not None:
            raise HTTPException(409, f"流程 id 已存在：{data['id']}")
        try:
            g = graph_from_dict(data)
        except ValueError as e:
            raise HTTPException(422, str(e))
        studio.flows.save(g)
        return graph_to_dict(g)

    @app.delete("/api/flows/{flow_id}")
    def delete_flow(flow_id: str):
        if not studio.flows.delete(flow_id):
            raise HTTPException(404, f"流程不存在：{flow_id}")
        return {"deleted": flow_id}

    @app.post("/api/flows/{flow_id}/run")
    def run_flow(flow_id: str, body: RunBody):
        g = studio.flows.get(flow_id)
        if g is None:
            raise HTTPException(404, f"流程不存在：{flow_id}")
        missing = [i["key"] for i in start_inputs(g)
                   if i.get("required") and not body.inputs.get(i["key"])]
        if missing:
            raise HTTPException(422, f"缺少必填输入：{'、'.join(missing)}")
        return studio.run_flow(g, body.inputs)

    @app.post("/api/agent/chat")
    def agent_chat(body: ChatBody):
        return studio.chat(body.message)

    @app.get("/api/agent/tools")
    def agent_tools():
        return tools_manifest(studio.flows.list())

    @app.post("/api/agent/tools/{tool_name}")
    def agent_tool_call(tool_name: str, body: dict):
        flow, params = parse_tool_call(tool_name, body.get("arguments") or {},
                                       studio.flows.list())
        if flow is None:
            raise HTTPException(404, f"工具不存在：{tool_name}")
        return studio.run_flow(flow, params)

    @app.get("/api/runs")
    def list_runs(flow_id: str | None = None, limit: int = 30):
        return studio.runs.list(flow_id, limit=min(limit, 200))

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        run = studio.runs.get(run_id)
        if run is None:
            raise HTTPException(404, f"运行记录不存在：{run_id}")
        return run

    # ---------------- 智能体平台：Agent / 知识库 / 记忆 / 技能 / MCP / 工具 ----------------

    @app.get("/api/ai-agents")
    def ai_agents():
        return studio.ai_agents.list()

    @app.post("/api/ai-agents")
    def create_ai_agent(body: dict):
        try:
            saved = studio.ai_agents.save(body)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return saved

    @app.get("/api/ai-agents/{agent_id}")
    def get_ai_agent(agent_id: str):
        agent = studio.ai_agents.get(agent_id)
        if agent is None:
            raise HTTPException(404, f"智能体不存在：{agent_id}")
        return agent

    @app.put("/api/ai-agents/{agent_id}")
    def save_ai_agent(agent_id: str, body: dict):
        body["id"] = agent_id
        try:
            return studio.ai_agents.save(body)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.delete("/api/ai-agents/{agent_id}")
    def delete_ai_agent(agent_id: str):
        if not studio.ai_agents.delete(agent_id):
            raise HTTPException(404, f"智能体不存在：{agent_id}")
        return {"deleted": agent_id}

    @app.post("/api/ai-agents/{agent_id}/invoke")
    def invoke_ai_agent(agent_id: str, body: dict):
        """直接调试运行一个智能体（不走流程）。"""
        agent = studio.ai_agents.get(agent_id)
        if agent is None:
            raise HTTPException(404, f"智能体不存在：{agent_id}")
        message = str(body.get("message") or "").strip()
        if not message:
            raise HTTPException(422, "message 不能为空")
        out = studio.agent_rt.run(agent, message,
                                  session_id=body.get("session_id") or None)
        out["ok"] = not out.get("error")
        return out

    # ---------------- 知识库 ----------------

    @app.get("/api/kb")
    def kb_list():
        return studio.kb.list_kbs()

    @app.post("/api/kb")
    def kb_create(body: dict):
        try:
            return studio.kb.create_kb(str(body.get("id") or ""),
                                       str(body.get("name") or ""),
                                       str(body.get("description") or ""))
        except ValueError as e:
            raise HTTPException(409 if "已存在" in str(e) else 422, str(e))

    @app.delete("/api/kb/{kb_id}")
    def kb_delete(kb_id: str):
        if not studio.kb.delete_kb(kb_id):
            raise HTTPException(404, f"知识库不存在：{kb_id}")
        return {"deleted": kb_id}

    @app.get("/api/kb/{kb_id}/docs")
    def kb_docs(kb_id: str):
        return studio.kb.list_docs(kb_id)

    @app.post("/api/kb/{kb_id}/docs")
    def kb_add_doc(kb_id: str, body: dict):
        try:
            return studio.kb.add_doc(kb_id, str(body.get("name") or "未命名文档"),
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
            return studio.kb.add_doc(kb_id, name or "上传文档", text)
        except ValueError as e:
            raise HTTPException(404 if "不存在" in str(e) else 422, str(e))

    @app.delete("/api/kb/{kb_id}/docs/{doc_id}")
    def kb_delete_doc(kb_id: str, doc_id: str):
        if not studio.kb.delete_doc(kb_id, doc_id):
            raise HTTPException(404, "文档不存在")
        return {"deleted": doc_id}

    @app.post("/api/kb/search")
    def kb_search(body: dict):
        return studio.kb.search(str(body.get("query") or ""),
                                kb_ids=body.get("kb_ids") or None,
                                top_k=int(body.get("top_k") or 5))

    # ---------------- 记忆 ----------------

    @app.get("/api/memory/scopes")
    def memory_scopes():
        return studio.memory.scopes()

    @app.get("/api/memory")
    def memory_list(scope: str | None = None, q: str | None = None,
                    limit: int = 100):
        return studio.memory.list(scope=scope, q=q, limit=min(limit, 500))

    @app.post("/api/memory")
    def memory_set(body: dict):
        try:
            return studio.memory.set(str(body.get("scope") or "global"),
                                     str(body.get("key") or ""), body.get("value"))
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.delete("/api/memory")
    def memory_delete(scope: str, key: str):
        if not studio.memory.delete(scope, key):
            raise HTTPException(404, "记忆条目不存在")
        return {"deleted": key}

    # ---------------- 技能 ----------------

    @app.get("/api/skills")
    def skills_list():
        return studio.skills.list()

    @app.get("/api/skills/{skill_id}")
    def skill_get(skill_id: str):
        skill = studio.skills.get(skill_id)
        if skill is None:
            raise HTTPException(404, f"技能不存在：{skill_id}")
        return skill

    @app.put("/api/skills/{skill_id}")
    def skill_save(skill_id: str, body: dict):
        try:
            return studio.skills.save(skill_id, str(body.get("name") or ""),
                                      str(body.get("description") or ""),
                                      str(body.get("content") or ""))
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.delete("/api/skills/{skill_id}")
    def skill_delete(skill_id: str):
        if not studio.skills.delete(skill_id):
            raise HTTPException(404, f"技能不存在：{skill_id}")
        return {"deleted": skill_id}

    # ---------------- MCP ----------------

    @app.get("/api/mcp")
    def mcp_config():
        return studio.mcp.load()

    @app.put("/api/mcp")
    def mcp_save(body: dict):
        return studio.mcp.save(body)

    @app.post("/api/mcp/{name}/tools")
    def mcp_tools(name: str):
        try:
            return studio.mcp.list_tools(name)
        except MCPError as e:
            raise HTTPException(502, str(e))

    @app.post("/api/mcp/{name}/call")
    def mcp_call(name: str, body: dict):
        tool = str(body.get("tool") or "").strip()
        if not tool:
            raise HTTPException(422, "缺少 tool")
        try:
            return studio.mcp.call(name, tool, body.get("arguments") or {},
                                   timeout=float(body.get("timeout") or 120))
        except MCPError as e:
            raise HTTPException(502, str(e))

    # ---------------- 工具 ----------------

    @app.get("/api/tools")
    def tools_list():
        return [{"name": t.name, "description": t.description}
                for t in (studio.tools.get(n) for n in studio.tools.names()) if t]

    # ---------------- 评测中心 ----------------

    @app.get("/api/evals/suites")
    def eval_suites():
        return studio.evals.list_suites()

    @app.post("/api/evals/suites")
    def eval_create_suite(body: dict):
        try:
            return studio.evals.save_suite(body)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.get("/api/evals/suites/{suite_id}")
    def eval_get_suite(suite_id: str):
        suite = studio.evals.get_suite(suite_id)
        if suite is None:
            raise HTTPException(404, f"评测集不存在：{suite_id}")
        return suite

    @app.put("/api/evals/suites/{suite_id}")
    def eval_save_suite(suite_id: str, body: dict):
        body["id"] = suite_id
        try:
            return studio.evals.save_suite(body)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.delete("/api/evals/suites/{suite_id}")
    def eval_delete_suite(suite_id: str):
        if not studio.evals.delete_suite(suite_id):
            raise HTTPException(404, f"评测集不存在：{suite_id}")
        return {"deleted": suite_id}

    @app.post("/api/evals/suites/{suite_id}/run")
    def eval_run(suite_id: str, body: dict):
        suite = studio.evals.get_suite(suite_id)
        if suite is None:
            raise HTTPException(404, f"评测集不存在：{suite_id}")
        targets = body.get("targets") or []
        if not targets:
            raise HTTPException(422, "至少选择一个评测目标")
        try:
            run = run_suite(suite, targets, studio.evals, studio.eval_runner,
                            llm_judge=_make_llm_judge(studio.llm_cfg))
        except ValueError as e:
            raise HTTPException(422, str(e))
        try:
            studio.obs.eval_run(run)   # 评测结果上报 Langfuse（score 事件）
        except Exception:  # noqa: BLE001
            pass
        return run

    @app.get("/api/evals/runs")
    def eval_runs(suite_id: str | None = None, limit: int = 20):
        return studio.evals.list_runs(suite_id, limit=min(limit, 100))

    @app.get("/api/evals/runs/{run_id}")
    def eval_get_run(run_id: str):
        run = studio.evals.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"评测记录不存在：{run_id}")
        return run

    @app.get("/api/evals/compare")
    def eval_compare(run_a: str, run_b: str):
        ra, rb = studio.evals.get_run(run_a), studio.evals.get_run(run_b)
        if ra is None or rb is None:
            raise HTTPException(404, "评测记录不存在")
        return compare_runs(ra, rb)

    @app.post("/api/llm/test")
    def llm_test(body: LlmBody):
        """画布里快速验证 LLM 连通性。"""
        text = llm_chat(studio.llm_cfg, body.system, body.prompt)
        if text is None:
            return JSONResponse({"ok": False,
                                 "error": "LLM 未启用或调用失败（config.llm）"},
                                status_code=200)
        return {"ok": True, "text": text}

    @app.get("/api/template/preview")
    def template_preview(q: str):
        """模板预览：仅内置 vars（today 等），无节点上下文。"""
        import datetime as dt

        ns = build_namespace({"today": dt.date.today().isoformat()}, {}, {})
        return {"rendered": render(q, ns)}

    # ---------------- 素材库 / 视频模型 ----------------
    @app.get("/api/assets")
    def list_assets(kind: str | None = None, flow_id: str | None = None,
                    limit: int = 200):
        return studio.media.list(kind=kind or None, flow_id=flow_id or None,
                                 limit=min(limit, 1000))

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
        rec = studio.media.add_bytes(data, suffix, kind=kind or "file",
                                     name=name or "", flow_id=flow_id or "")
        return rec

    @app.delete("/api/assets/{asset_id}")
    def delete_asset(asset_id: str):
        if not studio.media.delete(asset_id):
            raise HTTPException(404, f"素材不存在：{asset_id}")
        return {"deleted": asset_id}

    @app.get("/media/files/{fname}")
    def media_file(fname: str):
        p = studio.media.file_path(fname)
        if p is None:
            raise HTTPException(404, "文件不存在")
        return FileResponse(p, headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/api/video/models")
    def get_video_models():
        return studio.vmodels.load()

    @app.put("/api/video/models")
    def put_video_models(body: dict):
        return studio.vmodels.save(body)

    return app


def _slug(name: str) -> str:
    import re

    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", (name or "").strip().lower()).strip("-")
    return s[:48]


DEFAULT_EVAL_SUITE = {
    "id": "smoke",
    "name": "基础能力冒烟",
    "description": "标准问题集：算术 / 指令遵循 / 记忆。可直接跑平台智能体并与 codex 等外部 agent 对比",
    "cases": [
        {"id": "calc", "question": "计算 12*12 等于多少，只回答数字。",
         "expect": {"contains": ["144"]}, "note": "算术"},
        {"id": "rewrite", "question": "把「我今天很想吃火锅」改写成更书面的表达。",
         "expect": {"contains": ["火锅"]}, "note": "改写不得丢失关键信息"},
        {"id": "remember", "question": "请记住：我最喜欢的颜色是蓝色。然后只用一个词回答我最喜欢的颜色是什么。",
         "expect": {"contains": ["蓝"]}, "note": "指令遵循与上下文记忆"},
    ],
}


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
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        app = create_app(Path(args.config_dir))
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
