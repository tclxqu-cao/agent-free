"""测试夹具：最小 stdio MCP 服务器（echo / add 两个工具）。

协议与 mcp_client.py 对应：JSON-RPC 2.0，换行分隔帧。
"""

import json
import sys


def reply(msg_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id,
                                 "result": result}) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method", "")
        if method == "initialize":
            reply(msg["id"], {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "echo-mcp", "version": "0.1"}})
        elif method == "tools/list":
            reply(msg["id"], {"tools": [
                {"name": "echo", "description": "原样返回输入文本",
                 "inputSchema": {"type": "object",
                                 "properties": {"text": {"type": "string"}},
                                 "required": ["text"]}},
                {"name": "add", "description": "两数相加",
                 "inputSchema": {"type": "object",
                                 "properties": {"a": {"type": "number"},
                                                "b": {"type": "number"}},
                                 "required": ["a", "b"]}}]})
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo":
                text = f"echo: {args.get('text', '')}"
            elif name == "add":
                text = str(float(args.get("a", 0)) + float(args.get("b", 0)))
            else:
                reply(msg["id"], {"content": [{"type": "text",
                                               "text": f"未知工具 {name}"}],
                                  "isError": True})
                continue
            reply(msg["id"], {"content": [{"type": "text", "text": text}],
                              "isError": False})
        # notifications（无 id）静默忽略


if __name__ == "__main__":
    main()
