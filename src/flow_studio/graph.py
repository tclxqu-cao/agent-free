"""图模型：FlowGraph / Node / 边，节点类型元数据与校验。

流程 = 节点（type + label + params）+ 有向边（from/to/branch）。
条件分支写在 condition 节点的出边 branch 上（表达式或 "else"），节点本身无参数。
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
        "desc": "按出边顺序求值 branch 表达式，走第一条为真的（else 兜底）",
        "form": [],
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
    # ---------------- 视频制作 ----------------
    "storyboard": {
        "label": "分镜", "icon": "🎬", "color": "#db2777",
        "desc": "剧情提示词 → 分镜表（LLM 拆镜，无 LLM 按句切分降级）。"
                "输出 {shots[], count, aspect_ratio}，比例在此（项目级）一次设定并级联下游",
        "form": [{"key": "story", "widget": "textarea", "label": "剧情提示词（模板）",
                  "default": "{{input.story}}", "rows": 5},
                 {"key": "shot_count", "widget": "number", "label": "镜头数", "default": 4},
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
    edges: list[dict] = field(default_factory=list)   # {from, to, branch?}
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
                **({"branch": str(e["branch"])} if e.get("branch") else {})}
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
    for e in g.edges:  # 条件出边必须带 branch
        src = next((n for n in g.nodes if n.id == e["from"]), None)
        if src and src.type == "condition" and not e.get("branch"):
            errors.append(f"条件节点「{src.id}」的出边缺少 branch 表达式")
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
