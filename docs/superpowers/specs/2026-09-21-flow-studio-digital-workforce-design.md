# Flow Studio 数字员工编制与协作设计

## 1. 目标

在 Flow Studio 现有「智能体资产 + 编排画布 + 小团队治理」之上增加**组织层**：

给一个复杂任务（例：开发一个产品），系统自动完成岗位编制（产品经理、技术架构、开发、测试、运维、销售），
生成一支可运行的数字员工团队；员工之间通过**持久化消息总线**收发任务消息、通过**共享看板**领取与推进任务卡，
由后台驱动器逐轮推进直到交付；Web UI 直观看到组织图上的消息流动、看板卡片迁移和每个人的收件箱与产出。

设计约束：

- 保持 Python 3.11+ / FastAPI / SQLite / 零构建 vanilla-JS 形态，不引入外部消息中间件、队列或前端框架。
- **无 LLM key 时全链路可跑通**（岗位降级为规则产出，看板与总线仍是真实持久化与推送），与仓库既有降级风格一致。
- 复用而非平行建设：员工绑定现有 `ai_agents` 资产并走 `AgentRuntime.run`；治理沿用 `agent` 资源类型、`Principal` 能力、策略与审计。

## 2. 范围

### 2.1 本期包含

- 六个内置岗位模板 + 岗位可扩展；模板落为现有智能体资产，受版本/审批/发布约束。
- 任务 → 自动编制：LLM 依据任务裁剪岗位与职责，无 LLM 时按关键词规则模板降级，始终给出可解释的编制理由。
- 共享看板：`backlog / doing / review / done / blocked` 五列，卡片含归属岗位、依赖、产出摘要与迁移历史。
- 持久化消息总线：单播、群发、任务分派、评审、交付、人工干预六类消息；线程化（`parent_id`）、未读计数、已读回执。
- 后台协作驱动器：按轮次推进，员工读收件箱与自己的卡 → 调 `AgentRuntime`（装配协作工具）→ 写卡片产出并给下游发消息；支持启动、暂停、恢复、单轮、停止与进程重启后恢复。
- 人类以「老板」身份加入：随时向任意员工或全员发消息、改卡片、下达追加指令，下一轮员工必须响应。
- SSE 实时推送 + 组织图/看板/消息流三视图联动，桌面与移动视口可用。
- 治理：策略新增 `workforce` 段（轮次、每轮消息、员工数、允许岗位、是否允许自动建智能体）与审计事件。

### 2.2 本期不包含

- 跨机分布式执行、多实例并发同一团队（仅单实例内多线程）、外部 IM/邮件网关。
- 员工自主修改自身 System 提示词并自我发布（产出仍需人审批发布，见 §8）。
- 真实外网部署、CI/CD 或真实销售动作：运维与销售岗位产出的是方案与文案，不执行外部变更。

## 3. 概念模型

| 概念 | 说明 | 落点 |
|---|---|---|
| 岗位 Role | 职责、交付物、上下游协作习惯、可用协作工具 | `workforce.ROLE_TEMPLATES`，落为 `ai_agents` 资产 |
| 团队 Team | 一次任务的临时编制：任务、成员、状态、轮次、产物 | `workforce.sqlite` |
| 员工 Employee | 岗位 + 绑定的智能体 + 运行状态（idle/working/blocked/done） | 同上 |
| 看板 Board | 共享任务卡与迁移历史，团队唯一事实来源 | `board.sqlite` |
| 总线 Bus | 员工/人之间的持久化消息与线程 | `bus.sqlite` |
| 驱动 Crew | 轮次调度器：取料 → 执行 → 通信 → 收敛判定 | `crew.py` 后台线程 |

看板与总线职责边界：**看板是状态（谁在做什么、做到哪），总线是过程（说了什么）**。
卡片迁移必须伴随一条总线消息，反向亦然，两者互相校验（`consistency_check`）。

## 4. 存储

在 Workspace 目录 `data/workspaces/<id>/workforce/` 下三个 SQLite（WAL、foreign_keys、显式事务，沿用 `RunStore` 写法）：

```text
workforce/
├── workforce.sqlite   # teams, employees, rounds
├── board.sqlite       # cards, card_moves
└── bus.sqlite         # messages, reads
```

关键字段：

- `teams(id, workspace_id, task, status, plan_json, round_no, max_rounds, created_by, created_at, started_at, finished_at, error)`
  - `status ∈ draft / planning / running / paused / waiting_human / done / failed / stopped`
- `employees(id, team_id, role, agent_id, name, status, last_active_at, produced_json)`
- `cards(id, team_id, title, detail, role, status, priority, depends_on_json, output_json, created_by, updated_at)`
- `card_moves(id, team_id, card_id, from_status, to_status, actor, reason, created_at)`
- `messages(id, team_id, from_actor, to_actor, kind, subject, body_json, parent_id, round_no, created_at, ack_at)`
  - `to_actor = "*"` 表示全员广播；`kind ∈ assign / request / response / review / deliver / announce / human`
- `reads(message_id, actor, read_at)`

读扩展：`GET /api/workforce/teams/{id}/snapshot` 一次性返回团队、员工、看板、最近消息与统计，供首屏；增量走 SSE。

## 5. 自动编制

`workforce.compose(task, llm_cfg, policy) -> plan`：

1. LLM 可用时：给岗位清单与任务，要求输出 `{roster:[{role, reason, focus}], goal, milestones:[{title, role, depends_on}]}`。
2. 无 LLM 或解析失败：规则降级——默认六岗全编，按任务关键词裁剪（含「推广/客户/售价」留销售，含「上线/部署/监控」留运维，反之剔除），里程碑按固定价值链 产品 → 架构 → 开发 → 测试 → 运维 → 销售 生成，`via="rules"`。
3. 校验：岗位必须在 `policy.workforce.allowed_roles`；里程碑数 ≤ `max_milestones`；引用岗位必须存在；不满足直接返回 `{detail, code}` 而不是静默改写。
4. 计划落库为 `draft` 团队，人可改（增删员工、改卡标题）后再 `start`。
5. 建团队时按 `plan.roster` 自动创建对应 `ai_agents` 资产（`save_draft` v1，状态 `pending`），受 §8 策略约束；`auto_create_agents=false` 时要求显式绑定已发布智能体。

## 6. 协作驱动

`CrewDriver` 每个团队一个工作线程（`threading.Thread`，daemon），`_guards` 保证同团队单实例：

每轮 `round(n)`：

1. 取本轮输入：员工收件箱未读 + 人新增的 `human` 消息 + 该员工 `doing`/`blocked` 卡 + 上游已 `done` 卡产出。
2. 无料可做的员工本轮跳过并记 `rounds` 明细（不空转 LLM）。
3. 逐员工调用 `AgentRuntime.run(agent, brief, session_id=f"team:{team}:{employee}")`；
   `brief` 内含任务目标、看板摘要、自己待办、未读消息原文与**必须产出的结构化协议**。
4. 员工通过协作工具改变世界（工具内部校验并写审计，越权或非法迁移直接返回错误给模型自纠）：
   `board_list`、`board_add`、`board_move`、`board_comment`、`bus_read`、`bus_post`、`bus_ack`。
5. 一轮结束做收敛判定：全部卡 `done` → 交付汇总并 `done`；连续 `stall_rounds` 轮无状态变化 → `waiting_human` 并广播求助；达 `max_rounds` → `paused` 并说明原因。
6. 每轮写 `rounds` 记录（参与员工、消息数、卡片迁移、耗时、错误），SSE 播报 `round_done`。

控制语义：`pause` 在轮边界生效；`stop` 立即终止轮次并保留全部历史；`resume` 从库中状态继续；
`step` 只跑一轮（调试与演示用）；所有控制动作写审计。

降级：无 LLM 时 `crew._fallback_turn(employee, context)` 按岗位产出规则化交付物（PRD 要点 / 架构分层 / 实现清单 / 测试用例 / 发布与回滚方案 / 卖点话术），
仍真实调用 `board_move` 与 `bus_post`，链路、消息、看板与 SSE 完全一致，便于无 key 环境验收。

## 7. API

沿用认证三件套（Cookie、`X-CSRF-Token`、`X-Workspace-ID`）与 `{detail, code, request_id}` 错误体。

| 方法 路径 | 能力 | 说明 |
|---|---|---|
| `GET /api/workforce/roles` | resource.read | 内置岗位清单与职责 |
| `GET/POST /api/workforce/teams` | resource.read / resource.write | 团队清单；POST 用 `{task}` 自动编制并返回计划 |
| `GET /api/workforce/teams/{id}/snapshot` | resource.read | 团队 + 员工 + 看板 + 消息 + 统计 |
| `PATCH /api/workforce/teams/{id}` | resource.write | 改任务描述/员工/卡片（start 前） |
| `POST /api/workforce/teams/{id}/start` | runtime.execute | 启动驱动，可选 `max_rounds` |
| `POST /api/workforce/teams/{id}/step` | runtime.preview | 只推进一轮 |
| `POST /api/workforce/teams/{id}/pause` \| `/resume` \| `/stop` | runtime.execute | 轮边界控制 |
| `POST /api/workforce/teams/{id}/message` | runtime.execute | 人以老板身份发消息（`to` 为员工 id 或 `*`） |
| `POST /api/workforce/teams/{id}/cards` \| `PUT /cards/{cid}` | resource.write | 人工加卡/改卡/迁移 |
| `GET /api/workforce/teams/{id}/inbox/{employee}` | resource.read | 该员工消息线程 |
| `GET /api/workforce/events` | resource.read | SSE：`message`、`card`、`employee`、`round`、`team` 事件增量 |

SSE 端点仅允许同源、`Last-Event-ID` 可选重放最近 500 条；连接以 Workspace 订阅，事件带 `workspace_id` 过滤。

## 8. 治理接入

- 员工绑定的智能体沿用 `agent` 资源类型：自动创建 → `draft/pending`，正式运行需 `release.publish`；
  `require_approval=false` 的 Workspace 允许驱动器直接读取未发布草稿并在 UI 标注「未发布（策略豁免）」。
- 策略 `workforce` 段（`PolicyEngine.normalize` 向后兼容补默认）：
  `enabled`(true)、`max_teams`(5)、`max_members`(8)、`max_rounds`(12)、`max_messages_per_round`(40)、
  `max_cards`(30)、`allowed_roles`(空=全部)、`auto_create_agents`(true)、`allow_human_messages`(true)。
- 门禁阶段 `workforce_start`：校验团队规模、岗位白名单、员工绑定智能体的模型是否在 `allowed_models`。
  违反返回 403 `policy_code`，审计 `workforce.denied`。
- 审计事件：`workforce.team.create/compose/start/step/pause/resume/stop`、`workforce.card.move`、
  `workforce.message.post`、`workforce.round.done`、`workforce.deliver.summary`。`details` 经 `sanitize_details`。
- 跨 Workspace 隔离：`team_id` 只能在其所属 Workspace 解析；驱动器线程持有 `workspace_id` 并在每次工具调用时校验。

## 9. Web UI

顶栏新增第三个视图 **「数字员工」**（`nav-workforce`），与「智能体」「工作流画布」并列：

- **左栏**：任务输入框（多行）+「自动编制」+ 团队列表（状态徽标、轮次、消息数）。
- **中区上：组织图**。员工为节点（岗位图标/色），人类老板节点固定；员工间边按实际消息数加权，
  有新消息时沿边流动一个粒子并高亮，节点显示当前状态（空闲/执行中/受阻/完成）与未读数。
- **中区下：共享看板**。五列卡片，卡片显示岗位色条、依赖、状态、产出摘要；迁移历史悬停可见；支持人工拖拽（写 `human` 消息）。
- **右栏：实时消息流**。按时间倒序的线程化消息，`kind` 图标与配色区分，点击定位组织图与卡片；底部输入框向选中员工或全员发消息。
- **员工详情抽屉**：职责、绑定的智能体链接、本轮提示词、steps 轨迹、产出、收件箱。
- 控制条：启动 / 单轮 / 暂停 / 恢复 / 停止 + 轮次进度 + `waiting_human` 醒目提示。
- SSE 驱动增量渲染，断线自动重连并回退为快照刷新；页面切换或标签页隐藏时暂停动画（`prefers-reduced-motion` 降级为无粒子）。
- 权限：viewer 隐藏全部控制与发消息按钮；editor 可建团队与发消息，`release.publish` 类按钮仍按能力隐藏；服务端独立校验。

## 10. 一致性与失败处理

- 卡片迁移与消息写入在同一 SQLite 事务外按「先消息后卡片」顺序补偿：卡片事务失败则回滚消息写入标记（`messages.ack_at` 不变并在本轮错误中记录）。
- 员工执行异常：不终止团队，记 `employee.status=blocked`、错误写入本轮并向老板发 `request` 消息求助。
- 进程重启：`running` 团队在启动时降级为 `paused` 且 `plan_json.restart=true`，UI 显示「可恢复」，不自动重放（避免无人监督的循环）。
- 驱动线程禁止直接读客户端传入路径；所有存储来自 `WorkspaceRuntime`。
- 单实例同团队仅一个驱动线程（`_guards` + `team.status` 双检），并发 `start` 返回 409 当前状态。

## 11. 测试与验收

单元：

- 编制：LLM 桩输出解析、规则降级岗位裁剪、非法岗位拒绝、里程碑依赖闭包。
- 看板：合法/非法状态迁移、依赖阻塞、迁移历史、并发迁移。
- 总线：线程树、未读计数、`*` 广播、`Last-Event-ID` 重放窗口、Workspace 隔离。
- 驱动：单轮推进收敛、停滞 → `waiting_human`、`max_rounds` 截断、pause 轮边界生效、异常 → blocked + 求助、无 LLM 降级链路。
- 策略：成员/轮次/卡片/岗位白名单越界拒绝与审计码。

API 集成（httpx TestClient，含认证与 CSRF）：

- 建团队 → 计划可读 → 改卡 → start → 轮询 snapshot → 消息含 产品→架构→开发→测试→运维→销售 至少一条链路 → 全部卡 done。
- 四角色权限矩阵（viewer 不可建/不可控，editor 可建可发消息不可发布）。
- SSE：`/api/workforce/events` 在 start 后收到 `message` 与 `card` 事件序列且顺序与库一致。
- 重启恢复：新建实例进程内 stop→start 服务后团队为 `paused` 且可 `resume`。

浏览器验收（Playwright，桌面 + 移动视口）：输入真实任务 → 自动编制出 6 个岗位 → 启动 → 观察组织图消息流动、看板卡片迁移、消息流增长 → 人工插话 → 员工下一轮响应 → 停止；截图存 `gui-test-screenshots/`。
