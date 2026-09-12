# Flow Studio（Agent 编排画布）设计文档

日期：2026-09-11
状态：已确认（用户授权自主决策，"后续都按最佳方案推进，不用同意"）

## 1. 目标与范围

在 agent-free 仓库**单独新增一个模块** `src/flow_studio/`，提供可视化 Agent 编排画布：

- **Workflow 编排 + Agent 编排**：拖拽节点、连线成图，保存为可执行流程；
- **节点类型**：开始、结束、**大模型（LLM）节点**、**Agent 节点**（引用已注册 agent 的某个能力，如 job_agent 的抓取/匹配/分析）、**条件节点**（连线上写分支表达式）、**意图节点**（识别话术属于哪个意图，出边按意图名选分支、else 兜底；LLM 优先、关键词降级）、模板节点、HTTP 节点；
- **意图触发**：提供"我的 agent"入口 `POST /api/agent/chat`——任意外部 agent（如 AgentRoam 桌面端）把用户自然语言发过来，模块用 LLM（无 LLM 时关键词降级）路由到最匹配的流程并执行，返回最终回复文本；另提供 `/api/agent/tools` 输出 OpenAI function-calling 风格的工具清单，方便外部 agent 把每个流程注册成工具；
- **把已有应聘 agent 配置出来**：预置两个内置流程（模拟数据日检 / 每日真实流程），节点编排串联 `job_agent` 的 seed → 匹配 → 条件分支 → LLM 点评 → 汇总，画布打开即可运行验证。

明确不做（第一版）：并行分支调度、子流程嵌套、循环节点、多用户/鉴权、节点级断点重跑。

## 2. 技术选型

沿用项目原则（确定性主干、少依赖、可降级、SQLite 免部署）：

- 执行引擎 / 图模型 / 意图路由：**纯 Python + PyYAML + httpx，零新依赖**；
- 画布 Web UI：**零构建单页应用**（原生 JS + SVG 连线），不依赖 CDN、离线可用；
- 服务层：FastAPI + Uvicorn 放在可选依赖组 `[studio]`（`uv sync --extra studio`），不装也不影响 job_agent 本体；
- LLM：复用 OpenAI 兼容 API 配置（config.llm），未启用或调用失败时：LLM 节点降级为 skipped（流程继续），意图路由降级为关键词匹配；
- 表达式：条件分支用**受限 AST 求值器**（只允许比较/布尔/算术/路径取值，禁止调用与属性逃逸），模板用 `{{ 路径 }}` 占位符。

## 3. 总体架构

```
外部 agent（AgentRoam 等）──POST /api/agent/chat──┐
                                                  ▼
浏览器画布 ◄── FastAPI(server.py) ── intent.py 意图路由（LLM/关键词）
   │                 │                             │
   │  拖拽编排/保存/运行   │  store.py：flows/*.json + runs.sqlite
   ▼                 ▼                             ▼
graph.py 图模型 ──► engine.py FlowRunner ──► registry.py AgentRegistry
                    │  条件分支/模板渲染/防环        │
                    ▼                             ▼
              template.py 安全求值            adapters.py：job_agent 能力注册
```

模块划分（`src/flow_studio/`）：

| 模块 | 职责 |
|---|---|
| `graph.py` | `FlowGraph/Node` dataclass、节点类型元数据（画布表单据此渲染）、图校验 |
| `template.py` | `render()` 模板渲染 + `eval_expr()` 安全条件表达式 |
| `engine.py` | `FlowRunner`：从 start 拓扑执行，条件选边，产出 `RunResult`（逐节点状态） |
| `registry.py` | `AgentRegistry`：`register(agent, action, fn, params, description)` + 能力目录 |
| `adapters.py` | job_agent 能力适配：demo_seed / match_today / daily / scrape / analyze / report / login_status |
| `llm.py` | OpenAI 兼容 chat 调用（与 job_agent.advisor.llm_chat 同款，保持模块独立） |
| `intent.py` | `IntentRouter`：LLM 优先选流程 + 抽参数，失败降级关键词打分 |
| `store.py` | `FlowStore`（`data/flows/*.json`）+ `RunStore`（SQLite runs 表） |
| `builtin_flows.py` | 预置"应聘 Agent"两个流程，服务启动且库为空时种子写入 |
| `server.py` | FastAPI 应用工厂 `create_app()` + `flow-studio` 启动入口 |
| `web/` | 画布单页（index.html / app.js / style.css） |

## 4. 图模型与执行语义

流程 JSON（`data/flows/<id>.json`）：

```json
{
  "version": 1, "id": "job-hunt-demo", "name": "应聘 Agent · 模拟数据日检",
  "description": "...", "triggers": ["看看今天的岗位", "跑一次应聘demo"],
  "nodes": [{"id": "match", "type": "agent", "label": "匹配今日岗位",
             "params": {"agent": "job_agent", "action": "match_today", "args": {}}}],
  "edges": [{"from": "branch", "to": "advisor", "branch": "match.passed_count > 0"},
            {"from": "branch", "to": "none",    "branch": "else"}]
}
```

- 节点类型：`start`（声明输入变量与默认值）、`end`（`output` 模板 = 流程回复）、`llm`（`system`/`prompt`，输出 `{text}`；无 LLM → skipped 不阻断）、`agent`（`agent`+`action`+`args` 模板渲染后调用注册表）、`intent`（`source` 话术模板 + `intents` 清单 [{name, description, samples}]，输出 `{intent, confidence, matched, text, via}`，出边 branch = 意图名或 else，保存时校验 branch 必须在意图清单内）、`template`（多行模板 → `{text}`）、`http`（method/url/headers/body → `{status,json}`）、`condition`（无参数，分支写在出边 `branch`，`else` 兜底）；
- 命名空间：模板/表达式里 `input` = 流程输入，节点 id = 该节点输出 dict，`vars.today` / `vars.run_id` 内置；点号路径逐级取值；
- 执行：从 start 开始按就绪顺序串行执行；condition 节点按出边顺序取第一条为真的表达式（否则走 `else`）；OR-join（任一活跃入边到达即触发，节点至多执行 2 次、总步数上限 1000，防环）；
- 失败语义：普通节点异常 → 流程 failed（RunResult 带节点错误）；`llm` 节点可降级；`agent`/`http` 节点 `params.optional=true` 时异常降级为 skipped。

## 5. 服务 API

| 方法/路径 | 说明 |
|---|---|
| `GET /api/node-types` | 节点类型元数据（画布动态表单） |
| `GET /api/agents` | 已注册 agent 能力目录（画布 agent 节点下拉） |
| `GET/POST /api/flows`、`GET/PUT/DELETE /api/flows/{id}` | 流程 CRUD |
| `POST /api/flows/{id}/run` | 带输入运行，返回 RunResult（画布逐节点高亮） |
| `POST /api/agent/chat` | `{message}` → 意图路由 → 执行 → `{matched, flow_id, reply, run}` |
| `GET /api/agent/tools` | OpenAI function-calling 工具清单（每流程一个工具） |
| `GET /api/runs?flow_id=&limit=`、`GET /api/runs/{id}` | 运行历史 |

## 6. 画布（web/）

单页编辑器：左侧节点面板点击添加；画布网格上拖拽移动节点、从输出端口拖到输入端口连线（SVG 贝塞尔）；条件/意图边显示分支标签，双击可改（意图节点从意图清单下拉选）；右侧属性面板按节点类型动态渲染参数表单（agent 节点二级下拉选 agent/action，args 用 JSON 文本域；意图节点用结构化意图清单编辑器：名称/描述/示例话术，可增删）；顶部保存/运行（输入弹窗）/意图测试框；运行后节点描边高亮（运行中蓝/成功绿/失败红/降级灰），点击节点查看输入输出 JSON。

## 7. 内置"应聘 Agent"流程

1. **job-hunt-demo（模拟数据日检，无凭据无 LLM 可跑通）**：start → agent(seed_demo_data, 10 天) → agent(match_today) → condition：`match.passed_count > 0` → llm（职业顾问点评 Top5）→ template 汇总日报 → end；`else` → template"今日暂无匹配岗位" → end；
2. **job-hunt-daily（每日真实流程）**：start → agent(daily 全流程：抓取→匹配→建议→报告) → template 摘要（岗位数/匹配数/日报路径/抓取异常）→ end；
3. **job-intent-demo（意图分流演示）**：start(message) → intent 节点（find_jobs / trend_analysis 两个意图）→ 三条支路：find_jobs → seed+match+岗位回复；trend_analysis → seed+analyze+趋势回复；else → 兜底闲聊回复。

内置流程用 `FlowStore.seed_missing` 按 id 增量补种（只加缺的、绝不覆盖用户已有流程），升级新增内置流程后重启即得。

## 8. 测试

pytest：模板渲染与表达式安全（禁 import/call/逃逸）、图校验、引擎（顺序/条件两分支/LLM 降级/agent 注册/防环/失败传播）、意图降级路由与 LLM 路由（mock）、FlowStore/RunStore、内置应聘流程用 demo 数据端到端跑通（断言 end 输出含岗位数）、FastAPI TestClient 全 API + chat 触发内置流程。studio 未安装时服务相关测试 skip。

## 9. 假设记录（自主决策）

1. "写到 agent free 那个文件夹" = `/Users/caoqu/agent-free`（其会话在做本项目，job_agent 在此）；
2. "我的 agent" = 外部主 agent（如 AgentRoam），以 HTTP API 形式接入（chat + tools），本模块不改 customer-agent 代码；
3. 画布为单用户本地工具，服务默认 `127.0.0.1:8788`，无鉴权；
4. fastapi/uvicorn 为可选依赖组，`flow-studio` 入口缺依赖时提示 `uv sync --extra studio`。
