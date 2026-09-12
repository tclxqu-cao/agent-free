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
from .assets import AssetStore
from .builtin_flows import builtin_flows
from .engine import FlowRunner
from .graph import NODE_TYPES, FlowGraph, graph_from_dict, graph_to_dict, start_inputs
from .intent import parse_tool_call, route, tools_manifest
from .llm import llm_chat
from .registry import AgentRegistry
from .store import FlowStore, RunStore
from .template import build_namespace, render
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
                          media=self.media, vmodels=self.vmodels)

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
