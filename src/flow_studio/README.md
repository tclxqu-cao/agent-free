# Flow Studio · Agent 编排画布

可视化 **workflow / agent 编排**模块：拖拽节点连线成流程，Agent 和大模型都是节点，
条件节点负责分支；通过 HTTP API 用一句自然语言按意图触发整个流程。
内置把本仓库的应聘 agent（`job_agent`）完整配置出来的示例流程，开箱可跑。

## 快速开始

```bash
uv sync --extra studio          # 安装 web 依赖（fastapi/uvicorn）
uv run flow-studio              # 启动画布 http://127.0.0.1:8788 并自动打开浏览器
```

首次打开会进入实例初始化页，需要创建首个 `owner`（密码不能为空）。后续所有
API、媒体文件和 Web 操作都要求本地登录会话；写操作还必须携带当前会话的 CSRF token。

画布里：

1. 左上选择内置流程 **「应聘 Agent · 模拟数据日检」**，点 **▶ 运行**——
   无需招聘网站凭据和 LLM key，即可看到 seed → 匹配 → 条件分支 → LLM 点评（无 key 自动降级）→ 汇总 逐节点点亮；
2. 右上输入框说 **「跑一下应聘demo」**——意图路由自动选中流程并执行，弹窗返回完整应聘日报；
3. 点节点在右侧编辑参数（Agent 节点二级下拉选 `job_agent` 的能力），拖端口连线、
   条件连线双击改分支表达式，⌘S 保存。

## 小团队治理

Flow Studio 按单实例、小团队场景提供完整治理闭环：

- **登录与 RBAC**：`owner / admin / editor / viewer` 四级角色；连续 5 次登录失败会短时锁定账号与来源地址，会话默认 12 小时，可主动退出撤销。
- **Workspace 多租户**：每个请求显式选择 Workspace，成员、策略、资产、运行记录和审计按 Workspace 隔离。`owner` 可创建 Workspace，`owner/admin` 可管理成员。
- **不可变版本**：Agent 和 Flow 的每次保存或删除都会产生新草稿版本，历史快照不覆盖。
- **审批与发布**：编辑者提交，管理员批准/拒绝并发布；存在多个审批人时禁止提交人自批。正式运行始终解析到已发布版本，编辑者可指定版本预览。
- **回滚**：选择历史已发布版本回滚时会创建一个新的发布版本，保留完整历史，不移动发布指针伪造历史。
- **统一策略门禁**：提交、预览、发布和正式运行共用策略引擎，可限制模型、工具、MCP、Agent 步数、流程节点、HTTP 主机，并可要求指定评测集达到最低通过率。
- **追加审计**：登录、成员、策略、版本、运行和迁移事件写入 SQLite 审计表，敏感字段在入库前脱敏。

角色权限：

| 角色 | 读取/正式运行 | 编辑/版本预览/评测 | 审批/发布/回滚 | 成员/审计 | Workspace/策略 |
|---|---:|---:|---:|---:|---:|
| viewer | 是 | 否 | 否 | 否 | 否 |
| editor | 是 | 是 | 否 | 否 | 否 |
| admin | 是 | 是 | 是 | 是 | 否 |
| owner | 是 | 是 | 是 | 是 | 是 |

### 混合存储

默认数据根目录是 `data/`，可用 `--data-dir` 指向独立实例目录：

```text
data/
├── governance.sqlite             # 用户、成员、会话、策略、版本、发布指针、审计
└── workspaces/<workspace-id>/    # Flow/Agent JSON、知识库、技能、媒体、运行记录等
```

首次初始化时，旧 `data/` 资产会**复制**到 `workspaces/default/`，原文件不会移动或删除；
已存在的 Agent/Flow 会注册为已发布 `v1`。迁移有幂等标记，重复启动不会重复建版本。

备份必须同时包含 `governance.sqlite` 和整个 `workspaces/` 目录。建议停服务后复制；如需在线备份，
先用 SQLite backup API 取得一致的数据库快照，再复制 Workspace 文件。该模式不提供跨节点一致性、
外部身份源、KMS 或数据库级行租户隔离，因此定位是可信内网中的小团队单实例，不应直接作为公网企业控制面。

### 认证 API 示例

```bash
# 首次初始化；已初始化实例改用 /api/auth/login
curl -c /tmp/flow-studio.cookies -X POST http://127.0.0.1:8788/api/setup \
  -H 'Content-Type: application/json' \
  -d '{"username":"owner","display_name":"Owner","password":"change-me-123"}'

# 读取当前身份并从响应取得 csrf_token；之后请求同时带 Cookie、CSRF 和 Workspace
curl -b /tmp/flow-studio.cookies http://127.0.0.1:8788/api/me \
  -H 'X-Workspace-ID: default'
```

下文所有 API 示例均省略这三个认证参数；实际调用需要复用登录 Cookie，写请求添加
`X-CSRF-Token`，并用 `X-Workspace-ID` 指定当前 Workspace。

## 节点类型

| 类型 | 说明 |
|---|---|
| 开始 / 结束 | 声明流程输入变量；end 的回复模板 = 流程返回文本 |
| **我的 Agent** | 把话术交给本机 AgentRoam（:3000 `/api/agent/run`）推理——带它配置的模型、工具、技能与记忆，`done.finalText` 作为节点输出；可复用 sessionId；失败可降级 |
| 大模型 | OpenAI 兼容 API（`config.llm`）；未启用/失败时降级跳过不阻断 |
| Agent | 引用已注册 agent 的一个能力，如 `job_agent.match_today` / `daily` / `analyze` |
| 意图 | 识别话术属于哪个意图（LLM 优先、关键词降级）：配置意图清单（名称/描述/示例话术），出边按意图名选分支，else 兜底；输出 `{intent, confidence, matched, text}` |
| 条件 | 出边上写分支表达式（`match.passed_count > 0`），`else` 兜底 |
| 模板 | `{{节点id.字段}}` `{{input.x}}` `{{vars.today}}` 渲染文本 |
| HTTP | 发一次请求，输出 `{status, json}` |

### 视频制作节点（AI 短片流水线）

**剧情 → 分镜 → 角色设定图（可选）→ 逐镜关键帧 → 图生视频 → 配音 → 字幕成片**，
比例（16:9 / 9:16 / 1:1 / 4:3）在分镜层一次设定、级联下游，合成时校验一致：

| 类型 | 说明 |
|---|---|
| 分镜 🎬 | 剧情提示词 → 分镜表（LLM 拆镜；无 LLM 按句切分降级），输出 `{shots[], count, aspect_ratio}` |
| 角色 👤 | 按方位清单（正面/左侧/右侧/背面…）各生成一张角色设定图入库 |
| 关键帧 🖼 | 逐镜头生成首帧图（文生图），角色描述注入提示词保证一致性 |
| 镜头视频 🎥 | 逐关键帧图生视频（通用「提交+轮询」两段式，兼容 OpenAI 风格中转） |
| 配音 🔊 | 调用 team-agent `voice-service` 的 `/v1/tts`，按分镜 `narration` 逐镜生成 WAV；默认 Qwen3-TTS 0.6B 的 `Serena`、语速 `1.0` |
| 成片 🎞 | 逐镜对齐视频和配音，视频不足时冻结尾帧、音频不足时补静音；生成并烧录 SRT，输出带旁白 MP4、连续 WAV 与独立 SRT |
| 合成 🧩 | ffmpeg concat 按序合成长片（统一 H.264/yuv420p），校验比例一致 |
| 素材 📦 | 引用素材库中的素材（图片/视频/音频），素材库面板可上传/预览/删除 |

顶栏 **「模型」** 弹窗配置分镜 LLM（默认继承 `config.llm`）、文生图、图生视频和文本转语音。
语音默认连接 `http://127.0.0.1:17863` 的 team-agent `voice-service`；本机回环地址可不填
token，远程地址必须配置 Bearer token。图片或视频未配置时仍会生成占位素材（占位 PNG
纯 Python 生成、占位视频 ffmpeg 生成、无 ffmpeg 退 GIF）；TTS 未启用、不可达或返回无效
WAV 时，配音节点明确失败，不会把静音视频标记为完成。成片合成依赖 ffmpeg 与 libass
字幕滤镜（`brew install ffmpeg`）。烧录中文字幕会按平台选择可覆盖中文的字体：macOS
使用 Heiti SC，Linux 优先 Noto Sans CJK SC，Windows 优先 Microsoft YaHei；字体缺失时
成片节点会明确失败并给出安装提示，避免静默产出方框字幕。

内置流程 **「AI 短片 · 分镜流水线」**（video-demo）：无 LLM、无模型 key 即可端到端
跑通全链路（占位图 + 占位片段 + 真实 ffmpeg 合成 8 秒成片），配好模型后同一流程直接产真片。
生成产物（关键帧/片段/成片）在节点输出里直接内嵌预览，素材库面板统一管理。

内置流程 **「项目介绍 · 配音成片」**（project-intro-video）接受 `project_name` 和
`project_brief`，生成 8 镜、16:9、目标 72 秒（控制在 60–90 秒）的中文项目介绍。
默认使用 Serena 逐镜配音，最后同时保存 MP4、WAV 和 SRT；项目没有配置 LLM 或视觉模型时
仍可用确定性分镜和占位画面验证整条配音成片链路。

画布为无限画布：视口裁剪（放大后只渲染可见节点）、连线增量更新、rAF 合帧、
右下角小地图导航、＋/− 缩放按钮。

主要内置流程包括：**应聘 Agent · 模拟数据日检**（job-hunt-demo）、**应聘 Agent · 每日流程**（job-hunt-daily）、**应聘助手 · 意图分流**（job-intent-demo）、**我的 Agent · 推理测试**（agent-brain-test，验证 AgentRoam 桥）、**AI 短片 · 分镜流水线**（video-demo）和 **项目介绍 · 配音成片**（project-intro-video）。流程库按 id 增量补种，升级不覆盖你的修改。

「我的 Agent」节点在 `config/config.yaml` 的 `agent_bridge` 段配置（默认 `http://127.0.0.1:3000`，无需 key——推理由你自己的 agent 完成）。

## 智能体平台（资源库）

概念模型（对齐 Dify）：**Agent 是中心资产**——知识库 / 记忆 / 技能 / 工具 / MCP
都是配对给智能体的能力；**画布（流程）是智能体的一种编排方式**。顶栏「资源库」：
**智能体 / 知识库 / 技能 / MCP / 记忆 / 评测** 六个标签页管理全部资产。

### 智能体（可创建的 Agent 资产）

创建/编辑智能体按分区配置（Dify 式）：

- **身份**：名称 / 描述 / System 提示词；
- **编排方式**：`对话式`（ReAct 工具循环，模型自主检索与调工具）或
  `流程编排`（绑定一条画布流程作为执行策略——invoke 按流程逐步执行，
  流程里可再用「智能体」节点组装多智能体，嵌套最深 3 层防打穿）；
- **能力配对**：知识库（自动 RAG + 检索片段数）/ 技能 / 工具 / MCP；
- **记忆与执行**：长期记忆开关、工具循环最大步数。

第三方智能体使用 Customer Agent 公共 Agent Loop。默认只把 System 提示词作为
`instructions` 发送；勾选「将名称和描述加入 CA System 提示词」后，Flow Studio
会在请求中同时加入名称与描述。该选项不会在 CA 创建或绑定固定智能体，
`orchestration.agent_id` 仍可保持为空。

画布 **✨ 智能体** 节点选择一个智能体作为步骤调用（自带其全部能力），输出
`{text, steps, tool_calls, session_id}`，steps 保留 RAG 检索与每次工具调用轨迹。
「▶ 调试运行」可直接对话测试。内置示例 **知识助手**（kb-assistant）。
LLM 未启用时按节点「失败时中断流程」开关降级跳过或中断。

- RAG：绑定了知识库的智能体，**每次对话自动检索 top-k 片段注入 system**（不依赖模型主动调工具），步骤里记 `[rag] kb_rag` 一条。
- 记忆：开启后按 `agent:<id>` / `session:<id>` 作用域读写，会话末尾自动存最近一轮对话。

### 知识库 / 记忆 / 技能 / MCP / 工具

| 能力 | 说明 |
|---|---|
| 知识库 📚 | 文档入库自动分块，SQLite FTS5 中文检索（CJK bigram 索引 + bm25）。画布 **知识库节点**（`{text, chunks[]}`）与智能体 RAG 共用；面板支持粘贴文本 / 上传 .md .txt 等 / 检索测试 |
| 记忆 💾 | 作用域键值存储（`session:<id>` / `global`）。画布 **记忆节点** 支持 get/set/search/list/delete；面板可浏览/写入/删除 |
| 技能 🛠 | SKILL.md 风格指令包（frontmatter name/description + Markdown 正文），`data/skills/`。画布 **技能节点**：prompt 留空输出指令原文，填了则用技能指令做 system 调 LLM |
| MCP 🔌 | stdio 客户端（纯 Python，JSON-RPC 换行帧）。面板配置服务器（command/args/env，如 `npx -y @modelcontextprotocol/server-filesystem /dir`），可列出工具测试连通；画布 **MCP 节点** 直接调用 |
| 工具 🧰 | 内置工具注册表（`http_request` / `now` / `calc` / `kb_search` / `memory_save` / `memory_load` / `memory_search` / `load_skill` / `list_skills`），画布 **工具节点** 调用，也供智能体 function-calling 使用 |

## 评测中心

「资源库 → 评测」：**标准问题集 × 多目标执行 → 自动校验 + 对比分析**，定位不同
agent 在哪类问题上出问题。

- **评测集**：标准问题 + 期望（`contains` / `not_contains` / `regex`，可选 LLM 评判标准）。内置「基础能力冒烟」（算术 / 改写 / 指令遵循）。
- **目标**：`platform`（平台智能体，有完整执行步骤轨迹）、`cli`（任意命令行 agent——内置 Codex `codex exec` 与 Claude `claude -p` 预设，`{prompt}` 占位）、`http`（POST `{message}` 的 chat 端点）。
- **运行**：逐用例 × 逐目标执行，记录答案、**执行步骤**、逐条校验明细、耗时、错误。
- **对比**：两次运行逐用例 side-by-side，结论不一致的用例高亮 `⚡ diff`，一眼看出哪个目标在哪道题上挂了、答案差在哪。

```bash
curl -X POST http://127.0.0.1:8788/api/evals/suites/smoke/run -H 'Content-Type: application/json' \
  -d '{"targets":[{"type":"platform","id":"kb-assistant"},{"type":"cli","key":"codex","name":"Codex","command":"codex","args":["exec","--skip-git-repo-check","{prompt}"]}]}'
curl "http://127.0.0.1:8788/api/evals/compare?run_a=<id1>&run_b=<id2>"
```

其余平台 API：智能体（`/api/ai-agents` CRUD + `/invoke`）、知识库（`/api/kb`、文档、`/search`）、
记忆（`/api/memory`）、技能（`/api/skills`）、MCP（`/api/mcp`、`/tools`、`/call`）、
工具清单（`/api/tools`）、评测（`/api/evals/suites|runs|compare`）。

## 可观测性（Langfuse）

`config.yaml` 打开开关后，所有执行数据自动上报 [Langfuse](https://langfuse.com)（零重依赖，
直连 ingestion API，后台线程批量发送，**上报失败绝不影响流程执行**）：

```yaml
observability:
  langfuse:
    enabled: true
    host: https://cloud.langfuse.com        # 或自建实例
    public_key: pk-lf-...
    secret_key: sk-lf-...
```

（也可用环境变量 `LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`。）

上报内容与映射：

| 平台事件 | Langfuse 对象 | 说明 |
|---|---|---|
| 流程 run | trace + 每节点 span | 输入/输出、状态、耗时；失败节点 `level=ERROR`，降级节点 `WARNING` |
| 智能体执行 | trace | RAG 检索与工具调用为 span，**LLM 调用为 generation（含 prompt/响应与 token 用量）** |
| 评测结果 | 每用例×目标一个 trace + `eval_pass` score | 在 Langfuse 看板直接统计通过率、按目标/用例对比 |

## 「我的 agent」接入（意图触发）

外部 agent（如 AgentRoam 桌面端）两种接法：

```bash
# 1. 对话式：自然语言进来，自动路由 + 执行 + 回复
curl -X POST http://127.0.0.1:8788/api/agent/chat \
  -H 'Content-Type: application/json' -d '{"message": "抓一下招聘网站"}'

# 2. 工具式：把每个流程注册成 OpenAI function-calling 工具
curl http://127.0.0.1:8788/api/agent/tools
curl -X POST http://127.0.0.1:8788/api/agent/tools/job-hunt-daily \
  -H 'Content-Type: application/json' -d '{"arguments": {"skip_scrape": true}}'
```

其余 API：流程 CRUD（`/api/flows`）、运行（`/api/flows/{id}/run`）、
运行历史（`/api/runs`）、节点类型（`/api/node-types`）、agent 能力目录（`/api/agents`）、
素材库（`/api/assets` + 上传/删除、`/media/files/{name}` 静态媒体）、
视频模型配置（`/api/video/models`）。

## 新增 agent 能力

在 `adapters.py` 里仿照 `register_job_agent` 写一个注册函数：动作入参/出参均为
JSON 兼容 dict（建议带 `text` 字段作为可读摘要，模板节点直接引用），声明参数类型
（引擎会按声明纠正模板渲染产生的字符串布尔/数字），然后在 `server.Studio.__init__` 注册。

## 设计文档

- `docs/superpowers/specs/2026-09-11-flow-studio-design.md`（编排画布第一版）
- `docs/superpowers/specs/2026-09-11-flow-video-pipeline-design.md`（视频制作画布）
- `docs/superpowers/specs/2026-09-20-flow-studio-governance-design.md`（小团队治理与混合存储）
