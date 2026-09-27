# Flow Studio 与 Customer Agent 动态多 Agent 编排设计

## 1. 目标

Flow Studio 同时支持两种多 Agent 工作方式，二者共享 Agent 资产、运行记录和可观测界面，但编排权不同：

1. **手工 Workflow 编排**：用户在 Flow 画布中显式配置多个 Agent 节点、连接关系、条件和执行顺序。图是运行前确定的权威定义。
2. **CA 动态编排**：用户在 Flow 中只配置一个 Customer Agent（CA）人物。运行时由该 CA 总控根据任务和中间结果自主派生一级临时子 Agent、分派任务、等待结果、调整团队并汇总交付。Flow 不参与拆解和调度，只实时展示 CA 的运行图。

目标不是用新的 Flow workforce 调度器代替 CA，而是把 CA 已有的子 Agent 能力升级为受协议约束、可持久化、可实时投影的动态编排能力。

## 2. 当前基础与缺口

### 2.1 已有能力

- Flow 的 `ai_agent` 节点可以选择已配置的外部 CA Agent，并传递模型、Skills、Tools、MCP、记忆与工具策略。
- CA 已有 `dispatch_agent`、`wait_agent`、独立子 Session、父子 Session 关系和 `agent_dispatch / agent_progress / agent_done` 领域事件。
- CA 已限制子 Agent 不再获得派生权限，天然符合“仅主 Agent 派生一级子 Agent”的安全边界。
- Flow 已有运行事件、节点状态、历史 Run 与数字人舞台，可承载运行时投影和回放。

### 2.2 缺口

- 当前 `dispatch_agent` 只能按名称调用已持久化的 Agent，不能由总控临时定义新角色。
- Flow HTTP v1 只输出回答、工具和 Run 终态，过滤了子 Agent 事件。
- Flow 的 CA Provider 没有动态团队事件模型，画布和数字人舞台无法增加运行时节点。
- 现有 workforce 在 Flow 侧预先编制固定岗位，编排权属于 Flow，不符合本方案。

## 3. 非目标

- 不改变手工 Workflow 多 Agent 的节点、连线和执行语义。
- 不让 Flow 根据任务替 CA 选择岗位或创建子 Agent。
- 不把临时子 Agent 写入 CA 的长期 Agent 资产列表。
- 不允许子 Agent 递归派生下一层 Agent。
- 不支持跨机器分布式团队调度。
- 不把运行时临时节点写回或污染 Flow 草稿/发布版本。

## 4. 双模式模型

### 4.1 手工 Workflow 模式

手工模式保持现状：

- Flow 图中有多个显式 Agent 节点。
- 每个节点可分别配置本地 Agent、CA Agent 或绑定流程。
- Flow 引擎依据静态边、条件和节点输入输出执行。
- 运行画布展示静态图上的状态变化。

### 4.2 CA 动态编排模式

CA 动态模式仍使用一个普通 `ai_agent` 节点。该 Agent 的 `orchestration.mode` 保持 `external_agent`，新增可选配置：

```json
{
  "orchestration": {
    "mode": "external_agent",
    "provider": "customer-agent",
    "dynamic_team": {
      "enabled": true,
      "max_workers": 6,
      "max_parallel": 3,
      "worker_timeout_seconds": 900
    }
  }
}
```

`dynamic_team` 是外部 CA Agent 的运行能力，不增加第四种 Agent 编排方式，避免破坏现有 `react / external_agent / flow` 三态契约。

Flow 配置页在 CA 能力目录声明支持动态编排时显示开关和上限。未开启时，该 Agent 与今天完全一致。

## 5. 所有权边界

### 5.1 Customer Agent 负责

- 判断是否需要拆分任务以及需要几个角色。
- 生成每个临时角色的名称、职责、指令和子任务。
- 决定并行、等待、追加派生、失败重试或提前停止。
- 汇总所有子 Agent 结果并输出最终回答。
- 持久化父子 Session、子 Agent 事件和终态。
- 执行成员数、并行数、能力、超时与递归深度限制。

### 5.2 Flow Studio 负责

- 配置 CA 总控 Agent 和动态团队上限。
- 把配置作为受信任运行约束传给 CA。
- 订阅 CA 标准事件，不推断或替代 CA 的编排决策。
- 将运行时子 Agent 投影到 Flow 画布和数字人舞台。
- 持久化足以重建动态运行图的标准事件和最终快照。
- 在实时与历史 Run 中统一展示状态、关系、进度和失败。

## 6. CA 动态派生

### 6.1 新工具

CA 新增 `spawn_agent`，与既有“调用预存 Agent”的 `dispatch_agent` 并存：

```json
{
  "name": "接口设计师",
  "role": "负责接口和数据契约",
  "task": "基于需求给出 API、字段和错误码",
  "instructions": "先核对上游约束，再输出可测试契约"
}
```

模型只能定义角色语义和任务，不能自行扩大权限。临时子 Agent 的运行配置由服务端生成：

- 模型默认继承总控运行所选 Profile。
- Skills、Tools、MCP 和工具策略只能取 Flow 配置允许的 worker 能力上限与总控能力的交集。
- `spawn_agent`、`dispatch_agent` 和 `wait_agent` 不进入子 Agent 工具集。
- 子 Agent 使用独立 Session，记录 `parentSessionId`、角色、任务和本次 Flow Run 标识。

### 6.2 生命周期

1. 总控调用 `spawn_agent`，CA 校验团队上限并创建子 Session。
2. 子 Agent 后台执行，总控可继续派生其他成员。
3. 子 Agent 的进度与终态以事件写回父 Session；完成后向总控注入可消费的 mailbox 通知。
4. 总控用 `wait_agent` 等待指定成员，或在收到通知后继续规划。
5. 总控可以基于中间结果继续派生新成员，但总成员数和并发数始终受服务端限制。
6. 父 Run 取消时立即取消全部仍在运行的子 Agent。
7. 父 Run 进入成功终态前必须没有仍在运行的子 Agent；否则运行时先等待到统一期限，超时则把未完成成员标记为失败并交给总控收口。

临时 Agent 不成为长期资产，但子 Session 和事件保留，可审计、定位和回放。

## 7. Flow HTTP 协议

### 7.1 能力发现

`GET /api/flow/v1/catalog` 的 `features` 增加：

```json
{
  "dynamicAgentOrchestration": true,
  "maxSpawnDepth": 1
}
```

Flow 只有在该能力为真时允许保存 `dynamic_team.enabled=true`。

### 7.2 运行请求

`POST /api/flow/v1/runs` 增加可选字段：

```json
{
  "orchestration": {
    "mode": "dynamic_team",
    "maxWorkers": 6,
    "maxParallel": 3,
    "workerTimeoutSeconds": 900
  }
}
```

该字段由 Flow 的已发布 Agent 配置生成，浏览器运行输入不能覆盖。未提供时维持现有单 Agent 行为。

### 7.3 SSE 事件

Flow v1 以向后兼容方式增加以下事件：

| 事件 | 关键字段 | 含义 |
|---|---|---|
| `agent.spawned` | `agentId, sessionId, parentSessionId, name, role, task` | 临时成员已创建 |
| `agent.started` | `agentId, sessionId` | 成员开始执行 |
| `agent.progress` | `agentId, text, phase, toolName?` | 思考摘要、回答片段或工具进度 |
| `agent.completed` | `agentId, summary, durationMs` | 成员成功完成 |
| `agent.failed` | `agentId, code, message` | 成员执行失败 |

事件 ID 沿用父 Session 的单调递增游标。断线重连通过 `Last-Event-ID` 重放，不依赖 Flow 端猜测缺失状态。

原 `assistant.delta / tool.started / tool.completed / run.completed / run.failed` 保持不变。

## 8. Flow 运行时投影

### 8.1 画布

- 保存和发布的 Flow 仍只有一个 CA 总控节点。
- Run 开始后，首个 `agent.spawned` 事件在总控旁创建“运行时节点”，使用虚线轮廓和“临时”标识。
- 总控到子 Agent 的边表示派生/分派关系；本期没有子 Agent 之间的直接边，因为协作由总控中介。
- 节点状态为 `queued / running / completed / failed / cancelled`。
- 节点详情显示角色、任务、最新进度、工具、耗时、子 Session ID 和最终摘要。
- 运行结束后节点保留在该 Run 的监控视图中，但不会写回 Flow 编辑图。

### 8.2 数字人舞台

数字人舞台复用同一运行时投影：

- 总控人物固定为中心/主管位置。
- 子 Agent 出生时新增人物工位，派发任务时播放从总控到成员的消息动画。
- 进度和终态来自同一标准事件，不另建轮询或第二套状态机。
- 历史 Run 根据持久化事件重放成员出现、执行和完成过程。

### 8.3 双模式识别

- 手工 Workflow：拓扑来源为发布 Flow 的静态图，运行事件只更新既有节点。
- CA 动态编排：拓扑来源为 CA 运行事件，运行时扩展总控节点。
- 一个手工 Workflow 可以包含多个 CA 节点，其中任意节点都可单独开启动态团队；运行时子图归属各自父节点，ID 以 Flow node run 和 CA session 双重隔离。

## 9. 持久化与回放

Flow RunStore 为每个 CA 节点保存规范化动态事件，并在节点终态输出中保存 `dynamicTeamSnapshot`：

```json
{
  "supervisorSessionId": "...",
  "agents": [
    {
      "agentId": "...",
      "sessionId": "...",
      "name": "接口设计师",
      "role": "负责接口和数据契约",
      "task": "...",
      "status": "completed",
      "summary": "..."
    }
  ]
}
```

实时画布优先消费事件；页面刷新或历史回放先读快照，再从最后事件游标补增量。快照是恢复加速，不替代原始事件证据。

## 10. 权限与治理

- 动态编排必须由 Flow 中已发布的 CA Agent 配置显式开启。
- `maxWorkers`、`maxParallel` 与超时在 Flow 和 CA 两端校验，CA 为最终权威。
- 临时子 Agent 的能力不得超过总控运行能力和 worker allowlist 的交集。
- 工具执行继续经过 CA 的工具策略和审批边界。
- 角色名、任务和进度按不可信模型输出处理：限制长度、结构化校验、UI 转义。
- 审计记录至少包含父 Run、父 Session、子 Session、角色、派生时间、终态和取消来源。

## 11. 失败语义

- CA 不支持动态编排：Flow 配置页禁用开关；旧配置运行时返回明确的 `DYNAMIC_ORCHESTRATION_UNSUPPORTED`，不静默退化成单 Agent。
- 超过成员或并行上限：`spawn_agent` 返回结构化工具错误，总控可以调整计划。
- 单个子 Agent 失败：不直接终止父 Run；失败事件进入总控上下文，由总控决定补派、重试或带风险汇总。
- SSE 断线：Flow 使用游标重连；若重放窗口失效，则用节点 Run 输出快照恢复并继续订阅。
- 父 Run 取消：CA 取消全部子 Agent，Flow 将未终态节点标为 `cancelled`。
- CA 服务中断：Flow 节点失败并保留已经收到的团队状态，不能把部分团队展示成成功。

## 12. 测试与验收

### 12.1 Customer Agent

- `spawn_agent` 创建临时定义和独立子 Session，不写长期 Agent Store。
- 能力继承只收窄不扩大；子 Agent 无派生工具。
- 成员数、并行数、超时与父取消生效。
- 多个子 Agent 并行运行，事件 ID 单调且可按游标重放。
- 子 Agent 成功、失败、追加派生和父 Run 收敛行为正确。
- Flow v1 对新事件做规范映射，旧事件契约不回归。

### 12.2 Flow Studio

- CA 配置页仅在 catalog 支持时显示并保存动态团队配置。
- Provider 发送受控 `orchestration` 字段并解析全部新事件。
- 手工 Workflow 多 Agent 的现有测试保持不变。
- 动态节点只存在于 Run 投影，不进入 Flow 草稿或发布快照。
- 页面刷新、SSE 重连和历史 Run 都能恢复相同团队图。
- 同一 Flow 中多个动态 CA 节点的子图互不串线。

### 12.3 真实验收

1. 在 Flow 中配置一个启用动态团队的 CA Agent。
2. 只向该节点提交一个复杂任务，不预建产品、开发或测试 Agent。
3. CA 自主派生至少两个不同角色的临时子 Agent，并根据中间结果追加或调整任务。
4. Flow 画布实时出现临时节点、父子连线、状态与进度。
5. 数字人舞台显示相同成员和消息动画。
6. 父 CA 汇总子 Agent 结果后成功结束，历史 Run 可完整回放。
7. 证明长期 Agent 列表没有新增临时资产，子 Session 与审计记录仍可查询。

## 13. 实施边界

实现需要同时修改两个仓库：

- `/Users/caoqu/team-agent/customer-agent`：临时子 Agent 运行时、工具、事件、Flow HTTP v1 协议和测试。
- `/Users/caoqu/agent-free`：CA 配置、Provider 事件解析、RunStore 投影、Flow 画布、数字人舞台和测试。

Customer Agent 当前工作区存在其它未提交修改。实施必须只触碰本功能相关文件，并与现有修改逐文件合并，不能覆盖或回退用户工作。
