"""图模型：FlowGraph / Node / 边，节点类型元数据与校验。

流程 = 节点（type + label + params）+ 有向边（from/to/branch 或结构化比较）。
条件节点可配置安全取值路径；出边使用结构化比较或旧 branch 表达式。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------- 节点类型元数据
# form 供画布动态渲染参数表单；widget: text / textarea / number / bool / code / json
NODE_TYPES: dict[str, dict] = {
    "start": {
        "label": "开始", "icon": "▶", "color": "#16a34a",
        "desc": "流程入口，声明输入变量（画布运行时填写，意图触发时由路由抽取）",
        "form": [{"key": "inputs", "widget": "json", "label": "输入变量",
                  "hint": '[{"key": "day", "required": false, "default": ""}]'}],
    },
    "end": {
        "label": "结束", "icon": "■", "color": "#64748b",
        "desc": "流程出口，output 模板的渲染结果 = 流程回复文本",
        "form": [{"key": "output", "widget": "textarea", "label": "回复模板",
                  "default": "流程完成：{{vars.today}}", "rows": 6}],
    },
    "llm": {
        "label": "大模型", "icon": "✦", "color": "#7c3aed",
        "desc": "OpenAI 兼容 API；未启用或失败时降级为 skipped，流程继续",
        "form": [{"key": "system", "widget": "textarea", "label": "System 提示词",
                  "default": "你是得力助手。", "rows": 3},
                 {"key": "prompt", "widget": "textarea", "label": "User 提示词（模板）",
                  "default": "{{input.message}}", "rows": 8},
                 {"key": "required", "widget": "bool", "label": "失败时中断流程",
                  "default": False}],
    },
    "agent": {
        "label": "Agent", "icon": "🤖", "color": "#0ea5e9",
        "desc": "调用已注册 agent 的一个能力（如 job_agent 的抓取 / 匹配 / 分析）",
        "form": [{"key": "agent", "widget": "text", "label": "Agent ID"},
                 {"key": "action", "widget": "text", "label": "Action ID"},
                 {"key": "args", "widget": "json", "label": "参数（值支持 {{模板}}）",
                  "default": "{}"},
                 {"key": "optional", "widget": "bool", "label": "失败时降级跳过",
                  "default": False}],
    },
    "subflow": {
        "label": "子流程", "icon": "↳", "color": "#0284c7",
        "desc": "调用同一 Workspace 内已配置的流程，并把子流程节点输出传给下游",
        "form": [{"key": "flow_id", "widget": "text", "label": "流程 ID"},
                 {"key": "inputs", "widget": "json", "label": "流程输入（值支持 {{模板}}）",
                  "default": "{}"},
                 {"key": "required", "widget": "bool", "label": "失败时中断流程",
                  "default": True}],
    },
    "brain": {
        "label": "我的 Agent", "icon": "🧠", "color": "#2563eb",
        "desc": "把话术交给自己的 agent（AgentRoam :3000）推理——带它的模型、工具、技能与记忆，回复作为节点输出",
        "form": [{"key": "prompt", "widget": "textarea", "label": "发给 Agent 的话（模板）",
                  "default": "{{input.message}}", "rows": 6},
                 {"key": "session_id", "widget": "text", "label": "会话 ID（留空=每次新会话）",
                  "default": ""},
                 {"key": "timeout", "widget": "number", "label": "超时秒", "default": 300},
                 {"key": "required", "widget": "bool", "label": "失败时中断流程",
                  "default": False}],
    },
    "condition": {
        "label": "条件", "icon": "◆", "color": "#f59e0b",
        "desc": "按表达式选择分支，或在节点内按顺序匹配并输出文本",
        "form": [{"key": "source", "widget": "text", "label": "取值路径",
                  "default": "input.message",
                  "hint": "如 input.message、match.total、route.intent"}],
    },
    "intent": {
        "label": "意图", "icon": "🎯", "color": "#e11d48",
        "desc": "识别话术属于哪个意图（LLM 优先，关键词降级），出边按意图名选分支（else 兜底）",
        "form": [{"key": "source", "widget": "text", "label": "话术来源（模板）",
                  "default": "{{input.message}}"},
                 {"key": "required", "widget": "bool", "label": "未命中时中断流程",
                  "default": False}],
    },
    "template": {
        "label": "模板", "icon": "¶", "color": "#6366f1",
        "desc": "把上下文渲染成一段文本（{{节点id.字段}} / {{input.x}} / {{vars.today}}）",
        "form": [{"key": "template", "widget": "textarea", "label": "模板内容",
                  "default": "", "rows": 10}],
    },
    "http": {
        "label": "HTTP", "icon": " ⇄", "color": "#0d9488",
        "desc": "发起一次 HTTP 请求，输出 {status, json/text}；optional 时失败降级",
        "form": [{"key": "method", "widget": "text", "label": "Method", "default": "GET"},
                 {"key": "url", "widget": "text", "label": "URL（支持模板）"},
                 {"key": "headers", "widget": "json", "label": "Headers", "default": "{}"},
                 {"key": "body", "widget": "textarea", "label": "Body（模板）", "rows": 4},
                 {"key": "timeout", "widget": "number", "label": "超时秒", "default": 30},
                 {"key": "optional", "widget": "bool", "label": "失败时降级跳过",
                  "default": False}],
    },
    # ---------------- 智能体平台 ----------------
    "ai_agent": {
        "label": "智能体", "icon": "✨", "color": "#8b5cf6",
        "desc": "运行一个可创建的智能体（自带提示词 + 知识库/技能/工具/MCP/记忆绑定），"
                "工具循环推理后输出 {text, steps, tool_calls, session_id}",
        "form": [{"key": "message", "widget": "textarea", "label": "发给智能体的话（模板）",
                  "default": "{{input.message}}", "rows": 6},
                 {"key": "context", "widget": "json", "label": "结构化上下文（可用模板）",
                  "default": "{}"},
                 {"key": "session_id", "widget": "text", "label": "会话 ID（留空=每次新会话）",
                  "default": ""},
                 {"key": "required", "widget": "bool", "label": "失败时中断流程",
                  "default": False}],
    },
    "kb": {
        "label": "知识库", "icon": "📚", "color": "#b45309",
        "desc": "在选定的知识库里全文检索，输出 {text（拼接片段）, chunks[], count}",
        "form": [{"key": "query", "widget": "textarea", "label": "检索问题（模板）",
                  "default": "{{input.message}}", "rows": 3},
                 {"key": "top_k", "widget": "number", "label": "返回片段数", "default": 5}],
    },
    "memory": {
        "label": "记忆", "icon": "💾", "color": "#0f766e",
        "desc": "读写长期记忆（键值存储）。session 作用域按会话隔离，global 全局共享；"
                "输出 {value/items/ok, text}",
        "form": [{"key": "op", "widget": "select", "label": "操作",
                  "options": ["get", "set", "search", "list", "delete"], "default": "get"},
                 {"key": "scope", "widget": "select", "label": "作用域",
                  "options": ["session", "global"], "default": "session"},
                 {"key": "session_id", "widget": "text", "label": "会话 ID（session 作用域用，模板）",
                  "default": "{{vars.run_id}}"},
                 {"key": "key", "widget": "text", "label": "Key（get/set/delete 用，模板）",
                  "default": ""},
                 {"key": "value", "widget": "textarea", "label": "Value（set 用，模板；JSON 自动解析）",
                  "default": "", "rows": 3},
                 {"key": "query", "widget": "text", "label": "搜索词（search 用，模板）",
                  "default": ""}],
    },
    "skill": {
        "label": "技能", "icon": "🛠", "color": "#c026d3",
        "desc": "加载技能指令（SKILL.md）。prompt 留空只输出指令内容；"
                "填了则用技能指令作为 system 调 LLM，输出 {instructions, text}",
        "form": [{"key": "prompt", "widget": "textarea", "label": "交给 LLM 的话（模板，可空）",
                  "default": "", "rows": 5},
                 {"key": "required", "widget": "bool", "label": "LLM 失败时中断流程",
                  "default": False}],
    },
    "mcp": {
        "label": "MCP", "icon": "🔌", "color": "#dc2626",
        "desc": "调用 MCP 服务器（stdio）的工具，输出 {text, json?}；"
                "服务器在「资源库 → MCP」里配置",
        "form": [{"key": "arguments", "widget": "json", "label": "工具参数（值支持 {{模板}}）",
                  "default": "{}"},
                 {"key": "timeout", "widget": "number", "label": "超时秒", "default": 120},
                 {"key": "optional", "widget": "bool", "label": "失败时降级跳过",
                  "default": False}],
    },
    "tool": {
        "label": "工具", "icon": "🧰", "color": "#4f46e5",
        "desc": "调用内置工具（HTTP / 计算 / 时间 / 知识库 / 记忆 / 技能…），输出工具返回的 dict",
        "form": [{"key": "arguments", "widget": "json", "label": "工具参数（值支持 {{模板}}）",
                  "default": "{}"},
                 {"key": "optional", "widget": "bool", "label": "失败时降级跳过",
                  "default": False}],
    },
    # ---------------- 视频制作 ----------------
    "storyboard": {
        "label": "分镜", "icon": "🎬", "color": "#db2777",
        "desc": "剧情提示词 → 分镜表（LLM 拆镜，无 LLM 按句切分降级）。"
                "输出 {shots[], count, aspect_ratio}，比例在此（项目级）一次设定并级联下游",
        "form": [{"key": "story", "widget": "textarea", "label": "剧情提示词（模板）",
                  "default": "{{input.story}}", "rows": 5},
                 {"key": "shot_count", "widget": "number", "label": "镜头数", "default": 4},
                 {"key": "target_duration", "widget": "number",
                  "label": "目标总时长（秒，0=自动）", "default": 0},
                 {"key": "aspect_ratio", "widget": "select", "label": "画面比例（项目级）",
                  "options": ["16:9", "9:16", "1:1", "4:3"], "default": "16:9"},
                 {"key": "style", "widget": "text", "label": "画面风格",
                  "default": "电影感，胶片质感"}],
    },
    "character": {
        "label": "角色", "icon": "👤", "color": "#d97706",
        "desc": "角色设定图：按方位（正面/左侧/右侧/背面…）各生成一张，"
                "作为角色资产供下游注入描述。模型未配置时生成占位图",
        "form": [{"key": "name", "widget": "text", "label": "角色名", "default": "主角"},
                 {"key": "description", "widget": "textarea", "label": "外貌描述",
                  "default": "", "rows": 3},
                 {"key": "views", "widget": "views", "label": "方位（多方位设定图）",
                  "default": ["正面", "左侧", "右侧", "背面"]},
                 {"key": "aspect_ratio", "widget": "select", "label": "画面比例",
                  "options": ["16:9", "9:16", "1:1", "4:3"], "default": "16:9"},
                 {"key": "style", "widget": "text", "label": "画风", "default": ""}],
    },
    "keyframe": {
        "label": "关键帧", "icon": "🖼", "color": "#0891b2",
        "desc": "逐镜头生成首帧图（文生图）。比例默认继承分镜层；"
                "角色描述注入提示词保证一致性",
        "form": [{"key": "shots_source", "widget": "text", "label": "镜头来源（模板）",
                  "default": "{{storyboard.shots}}"},
                 {"key": "characters_source", "widget": "textarea",
                  "label": "角色描述注入（模板，可空）", "default": "", "rows": 2},
                 {"key": "aspect_ratio", "widget": "select", "label": "画面比例（留空继承分镜）",
                  "options": ["", "16:9", "9:16", "1:1", "4:3"], "default": ""},
                 {"key": "style", "widget": "text", "label": "画风（留空用分镜风格）",
                  "default": ""}],
    },
    "shot_video": {
        "label": "镜头视频", "icon": "🎥", "color": "#7c3aed",
        "desc": "逐关键帧图生视频（含运镜）。比例默认继承分镜层；"
                "模型未配置时生成占位片段（ffmpeg / GIF）",
        "form": [{"key": "frames_source", "widget": "text", "label": "关键帧来源（模板）",
                  "default": "{{keyframe.frames}}"},
                 {"key": "shots_source", "widget": "text",
                  "label": "镜头描述来源（运镜/画面，模板）",
                  "default": "{{storyboard.shots}}"},
                 {"key": "duration", "widget": "number", "label": "每镜秒数", "default": 3},
                 {"key": "aspect_ratio", "widget": "select", "label": "画面比例（留空继承分镜）",
                  "options": ["", "16:9", "9:16", "1:1", "4:3"], "default": ""},
                 {"key": "optional", "widget": "bool", "label": "失败时降级跳过",
                  "default": False}],
    },
    "merge_video": {
        "label": "合成", "icon": "🧩", "color": "#059669",
        "desc": "按顺序把多个镜头片段合成长片（ffmpeg concat，统一 H.264）。"
                "校验比例一致（enforce_ratio）",
        "form": [{"key": "clips_source", "widget": "text", "label": "片段来源（模板）",
                  "default": "{{shot_video.clips}}"},
                 {"key": "title", "widget": "text", "label": "成片名称",
                  "default": "成片-{{vars.today}}"},
                 {"key": "enforce_ratio", "widget": "bool",
                  "label": "强制比例一致（不一致时报错）", "default": True},
                 {"key": "optional", "widget": "bool", "label": "失败时降级跳过",
                  "default": False}],
    },
    "voiceover": {
        "label": "配音", "icon": "🔊", "color": "#2563eb",
        "desc": "调用 team-agent voice-service，按分镜 narration 逐镜生成 WAV 配音",
        "form": [{"key": "shots_source", "widget": "textarea", "label": "分镜列表来源",
                  "default": "{{storyboard.shots}}"},
                 {"key": "voice", "widget": "text", "label": "音色（留空继承模型设置）",
                  "default": ""},
                 {"key": "speed", "widget": "number", "label": "语速（0.5–2.0）",
                  "default": 1.0}],
    },
    "video_compose": {
        "label": "成片", "icon": "🎞", "color": "#0f766e",
        "desc": "对齐逐镜视频和配音，生成字幕并合成带旁白的 MP4",
        "form": [{"key": "clips_source", "widget": "textarea", "label": "镜头视频来源",
                  "default": "{{shot_video.clips}}"},
                 {"key": "voiceovers_source", "widget": "textarea", "label": "逐镜配音来源",
                  "default": "{{voiceover.tracks}}"},
                 {"key": "subtitles_source", "widget": "textarea", "label": "字幕来源",
                  "default": "{{voiceover.subtitles}}"},
                 {"key": "title", "widget": "text", "label": "成片名称",
                  "default": "项目介绍-{{vars.today}}"},
                 {"key": "burn_subtitles", "widget": "bool", "label": "烧录中文字幕",
                  "default": True}],
    },
    "asset": {
        "label": "素材", "icon": "📦", "color": "#64748b",
        "desc": "引用素材库中的一个素材（图片/视频/音频），输出 {asset_id, kind, path, url}",
        "form": [{"key": "asset_id", "widget": "asset", "label": "选择素材"}],
    },
}


# ---------------------------------------------------------------- 数据模型
@dataclass
class Node:
    id: str
    type: str
    label: str = ""
    params: dict = field(default_factory=dict)
    pos: dict = field(default_factory=dict)   # 画布坐标 {x, y}

    def display_label(self) -> str:
        return self.label or NODE_TYPES.get(self.type, {}).get("label") or self.type


@dataclass
class FlowGraph:
    id: str
    name: str
    nodes: list[Node] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)   # {from,to,branch?} 或结构化比较
    description: str = ""
    triggers: list[str] = field(default_factory=list)  # 意图触发示例话术
    version: int = 1

    # ---- 便捷访问
    def node(self, node_id: str) -> Node | None:
        return self._by_id.get(node_id)

    @property
    def _by_id(self) -> dict[str, Node]:
        return {n.id: n for n in self.nodes}

    def out_edges(self, node_id: str) -> list[dict]:
        return [e for e in self.edges if e["from"] == node_id]

    def start(self) -> Node | None:
        starts = [n for n in self.nodes if n.type == "start"]
        return starts[0] if starts else None

    def meta(self) -> dict:
        return {"id": self.id, "name": self.name, "description": self.description,
                "triggers": self.triggers, "nodes": len(self.nodes),
                "edges": len(self.edges)}


# ---------------------------------------------------------------- 序列化
def graph_to_dict(g: FlowGraph) -> dict:
    return {
        "version": g.version, "id": g.id, "name": g.name,
        "description": g.description, "triggers": list(g.triggers),
        "nodes": [{"id": n.id, "type": n.type, "label": n.label,
                   "params": n.params, "pos": n.pos} for n in g.nodes],
        "edges": [dict(e) for e in g.edges],
    }


def graph_from_dict(data: dict) -> FlowGraph:
    g = FlowGraph(
        id=str(data.get("id") or "").strip(),
        name=str(data.get("name") or "").strip() or "未命名流程",
        description=str(data.get("description") or ""),
        triggers=[str(t) for t in (data.get("triggers") or [])],
        version=int(data.get("version") or 1),
        nodes=[Node(id=str(n["id"]), type=str(n["type"]),
                    label=str(n.get("label") or ""),
                    params=dict(n.get("params") or {}),
                    pos=dict(n.get("pos") or {}))
               for n in (data.get("nodes") or []) if n.get("id") and n.get("type")],
        edges=[{"from": str(e["from"]), "to": str(e["to"]),
                **({"branch": str(e["branch"])} if e.get("branch") else {}),
                **({"operator": str(e["operator"])} if e.get("operator") else {}),
                **({"value": e.get("value")} if "value" in e else {}),
                **({"value_type": str(e["value_type"])} if e.get("value_type") else {})}
               for e in (data.get("edges") or []) if e.get("from") and e.get("to")],
    )
    errors = validate(g)
    if errors:
        raise ValueError("流程校验失败：" + "；".join(errors))
    return g


# ---------------------------------------------------------------- 校验
def validate(g: FlowGraph) -> list[str]:
    """返回错误列表；空列表 = 合法。"""
    errors: list[str] = []
    if not g.id:
        errors.append("缺少流程 id")
    if not g.name:
        errors.append("缺少流程名")
    ids = [n.id for n in g.nodes]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        errors.append(f"节点 id 重复：{sorted(dup)}")
    for n in g.nodes:
        if n.type not in NODE_TYPES:
            errors.append(f"节点「{n.id}」类型未知：{n.type}")
        if n.type == "subflow" and not str(n.params.get("flow_id") or "").strip():
            errors.append(f"子流程节点「{n.id}」缺少流程 ID")
    starts = [n for n in g.nodes if n.type == "start"]
    if len(starts) != 1:
        errors.append(f"开始节点必须恰好 1 个，当前 {len(starts)}")
    if not any(n.type == "end" for n in g.nodes):
        errors.append("至少需要 1 个结束节点")
    for e in g.edges:
        if e["from"] not in ids:
            errors.append(f"连线起点不存在：{e['from']}")
        if e["to"] not in ids:
            errors.append(f"连线终点不存在：{e['to']}")
    condition_operators = {"equals", "not_equals", "contains", "not_contains",
                           "greater_than", "greater_or_equal",
                           "less_than", "less_or_equal"}
    condition_value_types = {"string", "number", "boolean", "null"}
    for node in (n for n in g.nodes if n.type == "condition" and "outputs" in n.params):
        outputs = node.params.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            errors.append(f"条件节点「{node.id}」的输出规则必须是非空数组")
            continue
        names: list[str] = []
        for index, rule in enumerate(outputs, start=1):
            if not isinstance(rule, dict):
                errors.append(f"条件节点「{node.id}」的第 {index} 条输出规则必须是对象")
                continue
            name = str(rule.get("name") or "").strip()
            expression = str(rule.get("expression") or "").strip()
            if not name:
                errors.append(f"条件节点「{node.id}」的第 {index} 条输出规则缺少名称")
            elif name in names:
                errors.append(f"条件节点「{node.id}」的输出规则名称重复：{name}")
            else:
                names.append(name)
            if not expression:
                errors.append(f"条件节点「{node.id}」的输出规则「{name or index}」缺少表达式")
            if not isinstance(rule.get("output"), str):
                errors.append(f"条件节点「{node.id}」的输出规则「{name or index}」缺少文本输出")
        if not isinstance(node.params.get("default_output"), str):
            errors.append(f"条件节点「{node.id}」缺少默认输出文本")
        outgoing = [edge for edge in g.edges if edge["from"] == node.id]
        if len(outgoing) != 1:
            errors.append(f"条件节点「{node.id}」的输出模式必须恰好连接 1 个下游节点")
        elif any(outgoing[0].get(key) for key in ("branch", "operator")):
            errors.append(f"条件节点「{node.id}」的输出模式下游连线不能配置分支规则")
    for e in g.edges:  # 条件出边必须带表达式、结构化比较或 else
        src = next((n for n in g.nodes if n.id == e["from"]), None)
        if src and src.type == "condition" and "outputs" not in src.params:
            branch = str(e.get("branch") or "").strip()
            operator = str(e.get("operator") or "").strip()
            if not branch and not operator:
                errors.append(f"条件节点「{src.id}」的出边缺少比较规则或 branch 表达式")
            if operator and operator not in condition_operators:
                errors.append(f"条件节点「{src.id}」使用未知运算符：{operator}")
            value_type = str(e.get("value_type") or "string").strip()
            if operator and value_type not in condition_value_types:
                errors.append(f"条件节点「{src.id}」使用未知比较值类型：{value_type}")
        if src and src.type == "intent":
            declared = {str(i.get("name") or "").strip()
                        for i in (src.params.get("intents") or []) if i.get("name")}
            b = (e.get("branch") or "").strip()
            if not b:
                errors.append(f"意图节点「{src.id}」的出边缺少 branch（应为意图名或 else）")
            elif b.lower() != "else" and b not in declared:
                errors.append(f"意图节点「{src.id}」的分支「{b}」不在意图清单中")
    return errors


def start_inputs(g: FlowGraph) -> list[dict]:
    """读取 start 节点声明的输入变量 [{key, required, default}]。"""
    s = g.start()
    raw = (s.params.get("inputs") if s else None) or []
    out = []
    for item in raw:
        if isinstance(item, str):
            out.append({"key": item, "required": False, "default": ""})
        elif isinstance(item, dict) and item.get("key"):
            out.append({"key": str(item["key"]),
                        "required": bool(item.get("required")),
                        "default": item.get("default", "")})
    return out
