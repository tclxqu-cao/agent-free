"""MCP stdio 客户端：纯 Python 实现（JSON-RPC 2.0，换行分隔帧）。

只实现画布需要的最小面：initialize 握手 → tools/list → tools/call。
服务清单存 data/mcp.json：{"servers": {"<名>": {"command", "args", "env"}}}；
进程按名懒启动并复用，宕掉后下次调用自动重启。
"""

from __future__ import annotations

import json
import threading
import subprocess
from pathlib import Path

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "flow-studio", "version": "1.0"}


class MCPError(RuntimeError):
    """MCP 调用失败（连接 / 超时 / 工具报错）。"""


class MCPStdioClient:
    """单个 stdio MCP 服务进程的封装；线程安全（内部串行化请求）。"""

    def __init__(self, command: str, args: list[str] | None = None,
                 env: dict | None = None, cwd: str | None = None):
        self.command, self.args, self.env, self.cwd = command, args or [], env or {}, cwd
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}      # rpc id → {"event", "resp"}
        self._req_id = 0
        self._init_done = False

    # ---------------------------------------------------------------- 生命周期
    def _start(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return
        import os

        env = {**os.environ, **{str(k): str(v) for k, v in self.env.items()}}
        try:
            self.proc = subprocess.Popen(
                [self.command, *self.args], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=env, cwd=self.cwd or None, text=True, encoding="utf-8",
                bufsize=1)
        except OSError as e:
            raise MCPError(f"MCP 服务启动失败（{self.command}）：{e}") from e
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._init_done = False

    def _read_loop(self) -> None:
        proc = self.proc
        assert proc and proc.stdout
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            rpc_id = msg.get("id")
            if rpc_id is not None and ("result" in msg or "error" in msg):
                waiter = self._pending.pop(str(rpc_id), None)
                if waiter:
                    waiter["resp"] = msg
                    waiter["event"].set()
            # notification / 请求（服务端→客户端）当前忽略

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        self.proc = None
        self._init_done = False

    # ---------------------------------------------------------------- 协议
    def _send(self, method: str, params: dict | None, timeout: float,
              notify: bool = False) -> dict | None:
        self._start()
        proc = self.proc
        assert proc and proc.stdin
        if notify:
            msg = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                msg["params"] = params
            proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            return None
        self._req_id += 1
        rpc_id = str(self._req_id)
        msg = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
        if params is not None:
            msg["params"] = params
        waiter = {"event": threading.Event(), "resp": None}
        self._pending[rpc_id] = waiter
        try:
            proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError) as e:
            self._pending.pop(rpc_id, None)
            self.close()
            raise MCPError(f"MCP 服务管道已断开：{e}") from e
        if not waiter["event"].wait(timeout):
            self._pending.pop(rpc_id, None)
            self.close()
            raise MCPError(f"MCP 请求超时（{timeout}s）：{method}")
        resp = waiter["resp"] or {}
        if "error" in resp and resp["error"]:
            raise MCPError(f"MCP 错误：{resp['error'].get('message')}")
        return resp.get("result") or {}

    def _ensure_init(self, timeout: float) -> None:
        if self._init_done and self.proc and self.proc.poll() is None:
            return
        self._send("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": CLIENT_INFO}, timeout)
        self._send("notifications/initialized", None, timeout, notify=True)
        self._init_done = True

    # ---------------------------------------------------------------- 公开 API
    def list_tools(self, timeout: float = 30.0) -> list[dict]:
        with self._lock:
            self._ensure_init(timeout)
            result = self._send("tools/list", {}, timeout)
        return result.get("tools") or []

    def call_tool(self, name: str, arguments: dict | None = None,
                  timeout: float = 120.0) -> dict:
        with self._lock:
            self._ensure_init(min(timeout, 30.0))
            result = self._send("tools/call",
                                {"name": name,
                                 "arguments": arguments or {}}, timeout)
        text_parts = [c.get("text", "") for c in (result.get("content") or [])
                      if isinstance(c, dict) and c.get("type") == "text"]
        out: dict = {"text": "\n".join(t for t in text_parts if t)}
        if result.get("structuredContent") is not None:
            out["json"] = result["structuredContent"]
        if result.get("isError"):
            raise MCPError(out["text"] or "MCP 工具执行失败")
        return out


class MCPManager:
    """服务配置 + 进程复用：mcp.json 的加载 / 保存 / 按名取客户端。"""

    def __init__(self, config_path: Path):
        self.path = Path(config_path)
        self._clients: dict[str, MCPStdioClient] = {}

    # ---------------------------------------------------------------- 配置
    def load(self) -> dict:
        if not self.path.exists():
            return {"servers": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"servers": {}}
        return {"servers": dict(data.get("servers") or {})}

    def save(self, config: dict) -> dict:
        servers = {str(k): v for k, v in (config.get("servers") or {}).items()
                   if isinstance(v, dict) and v.get("command")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"servers": servers}, ensure_ascii=False,
                                        indent=2), encoding="utf-8")
        for name in list(self._clients):     # 配置变更后全部重连
            if name not in servers:
                self._clients.pop(name).close()
        return {"servers": servers}

    # ---------------------------------------------------------------- 客户端
    def client(self, name: str) -> MCPStdioClient:
        cfg = self.load()["servers"].get(name)
        if not cfg:
            raise MCPError(f"未配置的 MCP 服务：{name}")
        cli = self._clients.get(name)
        if cli is None:
            cli = MCPStdioClient(cfg["command"], cfg.get("args") or [],
                                 cfg.get("env") or {}, cfg.get("cwd"))
            self._clients[name] = cli
        return cli

    def list_tools(self, name: str) -> list[dict]:
        return self.client(name).list_tools()

    def call(self, name: str, tool: str, arguments: dict | None = None,
             timeout: float = 120.0) -> dict:
        return self.client(name).call_tool(tool, arguments, timeout)

    def close_all(self) -> None:
        for cli in self._clients.values():
            cli.close()
        self._clients.clear()
