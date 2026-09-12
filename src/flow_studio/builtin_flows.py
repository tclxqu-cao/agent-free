"""内置流程：把应聘 agent（job_agent）在编排画布里配置出来。

服务启动且流程库为空时写入（见 store.seed_if_empty），画布打开即可运行验证。
"""

from __future__ import annotations

from .graph import FlowGraph, Node, graph_to_dict


def _demo_flow() -> FlowGraph:
    """模拟数据日检：seed → 匹配 → 条件分支 → LLM 点评 → 汇总（无凭据无 LLM 可跑通）。"""
    return FlowGraph(
        id="job-hunt-demo",
        name="应聘 Agent · 模拟数据日检",
        description="生成 10 天模拟岗位数据 → 规则匹配今日岗位 → 条件分支："
                    "有命中则 LLM 顾问点评并汇总，否则提示放宽规则。"
                    "无需招聘网站凭据与 LLM key 即可运行。",
        triggers=["跑一下应聘demo", "模拟一次应聘日检", "看看今天的模拟岗位",
                  "测试一下应聘agent"],
        nodes=[
            Node(id="start", type="start", label="开始", pos={"x": 40, "y": 200},
                 params={"inputs": []}),
            Node(id="seed", type="agent", label="生成模拟数据", pos={"x": 260, "y": 200},
                 params={"agent": "job_agent", "action": "demo_seed",
                         "args": {"days": 10, "reset": True}}),
            Node(id="match", type="agent", label="匹配今日岗位", pos={"x": 480, "y": 200},
                 params={"agent": "job_agent", "action": "match_today", "args": {}}),
            Node(id="branch", type="condition", label="有命中岗位？", pos={"x": 700, "y": 200}),
            Node(id="advisor", type="llm", label="顾问点评", pos={"x": 930, "y": 90},
                 params={
                     "system": "你是资深职业规划顾问。基于候选岗位清单，输出简短中文点评："
                               "1) 最值得投的 2-3 个岗位及理由；2) 一条行动建议。"
                               "总共不超过 300 字。",
                     "prompt": "今日命中岗位：\n{{match.text}}\n\n我的目标是 Java 架构师方向，请点评。",
                     "required": False}),
            Node(id="summary", type="template", label="汇总日报", pos={"x": 1160, "y": 90},
                 params={"template":
                         "🧭 应聘 Agent · 模拟数据日检（{{vars.today}}）\n"
                         "① 模拟数据：{{seed.text}}\n"
                         "② 今日在招 {{match.total}} 个，规则匹配命中 {{match.passed_count}} 个：\n"
                         "{{match.text}}\n"
                         "③ 顾问点评：\n{{advisor.text}}"}),
            Node(id="none", type="template", label="无命中提示", pos={"x": 930, "y": 330},
                 params={"template":
                         "🧭 应聘 Agent · 模拟数据日检（{{vars.today}}）\n"
                         "已生成 {{seed.days}} 天模拟数据，今日在招 {{match.total}} 个岗位"
                         "但 0 个命中你的规则（薪资 / 距离 / 学历 / 排除词），"
                         "可到 config/config.yaml 的 rules 里放宽条件。"}),
            Node(id="end_hit", type="end", label="结束（有岗位）", pos={"x": 1390, "y": 90},
                 params={"output": "{{summary.text}}"}),
            Node(id="end_none", type="end", label="结束（无岗位）", pos={"x": 1160, "y": 330},
                 params={"output": "{{none.text}}"}),
        ],
        edges=[
            {"from": "start", "to": "seed"},
            {"from": "seed", "to": "match"},
            {"from": "match", "to": "branch"},
            {"from": "branch", "to": "advisor", "branch": "match.passed_count > 0"},
            {"from": "branch", "to": "none", "branch": "else"},
            {"from": "advisor", "to": "summary"},
            {"from": "summary", "to": "end_hit"},
            {"from": "none", "to": "end_none"},
        ],
    )


def _daily_flow() -> FlowGraph:
    """每日真实流程：daily 全流程（抓取→匹配→建议→日报）→ 趋势分析 → 摘要。"""
    return FlowGraph(
        id="job-hunt-daily",
        name="应聘 Agent · 每日流程",
        description="执行 job-agent daily（抓取→匹配→建议→日报），"
                    "再叠加 14 天市场趋势分析，汇总为一条可读摘要。"
                    "抓取需要各站点登录态（job-agent login）。",
        triggers=["跑每日应聘流程", "抓一下招聘网站", "生成岗位日报",
                  "今天有什么好岗位", "分析一下岗位趋势"],
        nodes=[
            Node(id="start", type="start", label="开始", pos={"x": 40, "y": 180},
                 params={"inputs": [{"key": "skip_scrape", "required": False,
                                     "default": False}]}),
            Node(id="daily", type="agent", label="每日全流程", pos={"x": 260, "y": 180},
                 params={"agent": "job_agent", "action": "daily",
                         "args": {"skip_scrape": "{{input.skip_scrape}}"}}),
            Node(id="analyze", type="agent", label="市场趋势分析", pos={"x": 480, "y": 180},
                 params={"agent": "job_agent", "action": "analyze",
                         "args": {"days": 14}}),
            Node(id="summary", type="template", label="汇总", pos={"x": 700, "y": 180},
                 params={"template":
                         "✅ 应聘 Agent · 每日流程（{{daily.date}}）\n"
                         "① {{daily.text}}\n"
                         "② 市场趋势：{{analyze.text}}"}),
            Node(id="end", type="end", label="结束", pos={"x": 920, "y": 180},
                 params={"output": "{{summary.text}}"}),
        ],
        edges=[
            {"from": "start", "to": "daily"},
            {"from": "daily", "to": "analyze"},
            {"from": "analyze", "to": "summary"},
            {"from": "summary", "to": "end"},
        ],
    )


def _intent_flow() -> FlowGraph:
    """意图分流演示：一个意图节点把话术分到 应聘 demo / 趋势分析 / 闲聊 三条支路。"""
    return FlowGraph(
        id="job-intent-demo",
        name="应聘助手 · 意图分流",
        description="意图节点识别话术：想看岗位 → 生成模拟数据并匹配今日岗位；"
                    "想看趋势 → 生成数据并做市场分析；其它 → 走 else 兜底闲聊回复。"
                    "无凭据无 LLM 可跑通（关键词降级）。",
        triggers=["意图分流", "意图测试"],
        nodes=[
            Node(id="start", type="start", label="开始", pos={"x": 40, "y": 200},
                 params={"inputs": [{"key": "message", "required": True,
                                     "default": "看看今天的岗位"}]}),
            Node(id="router", type="intent", label="识别意图", pos={"x": 260, "y": 200},
                 params={"source": "{{input.message}}", "required": False,
                         "intents": [
                             {"name": "find_jobs", "description": "想看今天的岗位、跑应聘流程",
                              "samples": ["看看今天的岗位", "跑一下应聘demo", "有什么好岗位"]},
                             {"name": "trend_analysis", "description": "想了解市场趋势与技能热度",
                              "samples": ["分析一下岗位趋势", "技能热度怎么样"]},
                         ]}),
            Node(id="seed1", type="agent", label="生成模拟数据", pos={"x": 520, "y": 60},
                 params={"agent": "job_agent", "action": "demo_seed",
                         "args": {"days": 10, "reset": True}}),
            Node(id="match", type="agent", label="匹配今日岗位", pos={"x": 740, "y": 60},
                 params={"agent": "job_agent", "action": "match_today", "args": {}}),
            Node(id="jobs_tpl", type="template", label="岗位回复", pos={"x": 960, "y": 60},
                 params={"template":
                         "🎯 意图[find_jobs] · 应聘日检（{{vars.today}}）\n"
                         "你说：「{{router.text}}」\n"
                         "今日在招 {{match.total}} 个，命中 {{match.passed_count}} 个：\n"
                         "{{match.text}}"}),
            Node(id="seed2", type="agent", label="生成模拟数据", pos={"x": 520, "y": 200},
                 params={"agent": "job_agent", "action": "demo_seed",
                         "args": {"days": 14, "reset": True}}),
            Node(id="analyze", type="agent", label="市场趋势分析", pos={"x": 740, "y": 200},
                 params={"agent": "job_agent", "action": "analyze", "args": {"days": 14}}),
            Node(id="trend_tpl", type="template", label="趋势回复", pos={"x": 960, "y": 200},
                 params={"template":
                         "🎯 意图[trend_analysis] · 市场分析（{{vars.today}}）\n"
                         "你说：「{{router.text}}」\n{{analyze.text}}"}),
            Node(id="chat_tpl", type="template", label="兜底回复", pos={"x": 520, "y": 340},
                 params={"template":
                         "🎯 未命中业务意图（else 兜底）。我是应聘助手，可以试试说：\n"
                         "「看看今天的岗位」或「分析一下岗位趋势」。\n"
                         "你说的是：「{{router.text}}」"}),
            Node(id="end_jobs", type="end", label="结束（岗位）", pos={"x": 1180, "y": 60},
                 params={"output": "{{jobs_tpl.text}}"}),
            Node(id="end_trend", type="end", label="结束（趋势）", pos={"x": 1180, "y": 200},
                 params={"output": "{{trend_tpl.text}}"}),
            Node(id="end_chat", type="end", label="结束（兜底）", pos={"x": 740, "y": 340},
                 params={"output": "{{chat_tpl.text}}"}),
        ],
        edges=[
            {"from": "start", "to": "router"},
            {"from": "router", "to": "seed1", "branch": "find_jobs"},
            {"from": "seed1", "to": "match"},
            {"from": "match", "to": "jobs_tpl"},
            {"from": "jobs_tpl", "to": "end_jobs"},
            {"from": "router", "to": "seed2", "branch": "trend_analysis"},
            {"from": "seed2", "to": "analyze"},
            {"from": "analyze", "to": "trend_tpl"},
            {"from": "trend_tpl", "to": "end_trend"},
            {"from": "router", "to": "chat_tpl", "branch": "else"},
            {"from": "chat_tpl", "to": "end_chat"},
        ],
    )


def _brain_flow() -> FlowGraph:
    """我的 Agent 推理测试：话术直接交给本机 AgentRoam 推理并原样返回。"""
    return FlowGraph(
        id="agent-brain-test",
        name="我的 Agent · 推理测试",
        description="把输入话术交给本机 AgentRoam（:3000）的 agent 推理——"
                    "带它配置的模型、工具、技能与记忆，回复原样返回。"
                    "需本机 customer-agent 服务在跑。",
        triggers=["测试agent推理", "agent大脑"],
        nodes=[
            Node(id="start", type="start", label="开始", pos={"x": 40, "y": 160},
                 params={"inputs": [{"key": "message", "required": True,
                                     "default": "你好，请用一句话介绍你自己"}]}),
            Node(id="brain", type="brain", label="我的 Agent 推理", pos={"x": 280, "y": 160},
                 params={"prompt": "{{input.message}}", "session_id": "",
                         "timeout": 300, "required": False}),
            Node(id="end", type="end", label="结束", pos={"x": 520, "y": 160},
                 params={"output": "🧠 {{brain.text}}\n（工具调用 {{brain.tool_calls}} 次）"}),
        ],
        edges=[
            {"from": "start", "to": "brain"},
            {"from": "brain", "to": "end"},
        ],
    )


def _video_flow() -> FlowGraph:
    """电影短片流水线：剧情 → 分镜 → 角色设定图 → 关键帧 → 镜头视频 → 合成。

    无 LLM / 无模型 key / 全占位降级也能端到端跑通（占位图 + 占位片段 + ffmpeg 合成）。
    """
    return FlowGraph(
        id="video-demo",
        name="AI 短片 · 分镜流水线",
        description="剧情提示词 → 分镜（4 镜 16:9）→ 角色多方位设定图 → 逐镜关键帧"
                    " → 图生视频片段 → ffmpeg 合成长片。比例在分镜层一次设定级联下游；"
                    "无 LLM 无模型 key 时全占位降级可跑通（合成需 ffmpeg）。",
        triggers=["制作一个短片", "跑一下视频流水线", "生成分镜视频", "video demo"],
        nodes=[
            Node(id="start", type="start", label="开始", pos={"x": 40, "y": 220},
                 params={"inputs": [{"key": "story", "required": False,
                                     "default": "清晨，少年背起行囊告别山村。"
                                                "他穿过薄雾笼罩的森林，惊起一群飞鸟。"
                                                "黄昏时登上山顶，看见云海翻涌的壮丽日落。"
                                                "夜晚他在星空下扎营，火光映着微笑。"}]}),
            Node(id="storyboard", type="storyboard", label="生成分镜", pos={"x": 280, "y": 220},
                 params={"story": "{{input.story}}", "shot_count": 4,
                         "aspect_ratio": "16:9", "style": "电影感，吉卜力风插画"}),
            Node(id="character", type="character", label="主角设定图", pos={"x": 520, "y": 220},
                 params={"name": "少年", "views": ["正面", "侧面", "背面"],
                         "description": "十五岁少年，短发，背着土黄色行囊，穿蓝色粗布外套",
                         "aspect_ratio": "1:1", "style": "吉卜力风插画"}),
            Node(id="keyframe", type="keyframe", label="逐镜关键帧", pos={"x": 760, "y": 220},
                 params={"shots_source": "{{storyboard.shots}}",
                         "characters_source": "主角是{{character.name}}：{{character.description}}",
                         "aspect_ratio": "", "style": ""}),
            Node(id="shot_video", type="shot_video", label="图生视频", pos={"x": 1000, "y": 220},
                 params={"frames_source": "{{keyframe.frames}}",
                         "shots_source": "{{storyboard.shots}}",
                         "duration": 2, "aspect_ratio": "", "optional": False}),
            Node(id="merge", type="merge_video", label="合成长片", pos={"x": 1240, "y": 220},
                 params={"clips_source": "{{shot_video.clips}}",
                         "title": "少年与山-{{vars.today}}",
                         "enforce_ratio": True, "optional": False}),
            Node(id="end", type="end", label="结束", pos={"x": 1480, "y": 220},
                 params={"output":
                         "🎬 AI 短片完成（{{vars.today}}）\n"
                         "① 分镜：{{storyboard.text}}\n"
                         "② 角色：{{character.text}}\n"
                         "③ 关键帧：{{keyframe.text}}\n"
                         "④ 镜头视频：{{shot_video.text}}\n"
                         "⑤ 成片：{{merge.text}}"}),
        ],
        edges=[
            {"from": "start", "to": "storyboard"},
            {"from": "storyboard", "to": "character"},
            {"from": "character", "to": "keyframe"},
            {"from": "keyframe", "to": "shot_video"},
            {"from": "shot_video", "to": "merge"},
            {"from": "merge", "to": "end"},
        ],
    )


def builtin_flows() -> list[dict]:
    return [graph_to_dict(_demo_flow()), graph_to_dict(_daily_flow()),
            graph_to_dict(_intent_flow()), graph_to_dict(_brain_flow()),
            graph_to_dict(_video_flow())]
