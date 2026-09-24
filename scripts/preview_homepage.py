"""Run Flow Studio and the portfolio gateway on their shared local ports."""
import argparse
from contextlib import nullcontext
import os
import plistlib
import secrets
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import uvicorn

from flow_studio.server import create_app


def load_agent_run_token():
    """Reuse the local Customer Agent run token without copying it into config."""
    current = (os.environ.get("AGENT_RUN_TOKEN", "").strip()
               or os.environ.get("AGENT_SDK_TOKEN", "").strip()
               or os.environ.get("PORTFOLIO_SKILL_TOKEN", "").strip())
    if current:
        return current
    plist = Path.home() / "Library/LaunchAgents/com.agentroam.customer-agent.webapp.plist"
    try:
        with plist.open("rb") as stream:
            payload = plistlib.load(stream)
        env = payload.get("EnvironmentVariables") or {}
        return str(env.get("AGENT_RUN_TOKEN") or env.get("AGENT_SDK_TOKEN")
                   or env.get("PORTFOLIO_SKILL_TOKEN") or "").strip()
    except (OSError, plistlib.InvalidFileException):
        return ""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--portfolio-dir", type=Path, required=True)
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument("--port", type=int, default=8801)
    parser.add_argument("--flow-port", type=int, default=8788)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--data-dir", type=Path,
                        help="使用现有 Flow Studio 数据目录；不传则使用隔离临时目录")
    args = parser.parse_args()
    token = secrets.token_urlsafe(32)
    os.environ["FLOW_HOMEPAGE_TOKEN"] = token
    agent_run_token = load_agent_run_token()
    if agent_run_token:
        os.environ["AGENT_RUN_TOKEN"] = agent_run_token
    data_context = nullcontext(args.data_dir.resolve()) if args.data_dir else \
        tempfile.TemporaryDirectory(prefix="homepage-preview-")
    with data_context as data:
        app = create_app(args.config_dir.resolve(), Path(data))
        server = uvicorn.Server(uvicorn.Config(
            app, host=args.host, port=args.flow_port, log_level="warning"))
        worker = threading.Thread(target=server.run, daemon=True)
        worker.start()
        deadline = time.monotonic() + 10
        while not server.started:
            if not worker.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Flow preview failed to start")
            time.sleep(0.05)
        env = {**os.environ, "PORT": str(args.port),
               "HOMEPAGE_FLOW_URL": f"http://127.0.0.1:{args.flow_port}/api/homepage/command"}
        process = subprocess.Popen(["node", str(args.portfolio_dir / "preview.js")], env=env)
        runtime_kind = "project data" if args.data_dir else "isolated runtime"
        print(f"Flow Studio: http://{args.host}:{args.flow_port} ({runtime_kind})", flush=True)
        try:
            process.wait()
        except KeyboardInterrupt:
            process.terminate()
            process.wait(timeout=10)
        finally:
            server.should_exit = True
            worker.join(timeout=10)


if __name__ == "__main__":
    main()
