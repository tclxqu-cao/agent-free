# Flow Studio 小团队治理平台设计

## 1. 目标

在现有 Flow Studio 上增加可用于小团队单实例部署的治理闭环：本地账号登录、Workspace 多租户、`owner/admin/editor/viewer` RBAC、Agent 与 Flow 的版本审批发布、不可变历史与回滚、统一策略门禁，以及不可变审计日志。

设计必须保持现有 Python 3.11+、FastAPI、SQLite 和 JSON 文件资产形态，不引入外部身份服务或数据库。现有单用户资产在首次初始化时无损进入 `default` Workspace，并登记为已发布的 v1。

## 2. 范围

### 2.1 本期包含

- 首次启动 owner 初始化、用户名密码登录、服务端 Session Cookie、退出登录和会话过期。
- Workspace 创建、切换和成员管理；所有资产、运行记录、评测和配置按 Workspace 隔离。
- 角色权限：
  - `owner`：Workspace、成员、角色、策略、资产、审批、发布、回滚和审计全部权限。
  - `admin`：管理普通成员和资产，审批、发布、回滚；不能转移 owner 或删除 Workspace。
  - `editor`：创建与编辑草稿、预览运行、执行评测和提交审批。
  - `viewer`：查看与运行已发布版本、查看运行记录。
- Agent 与 Flow 的不可变版本、审批、发布、拒绝、回滚和删除版本。
- 统一策略门禁覆盖提交、发布、预览和正式运行。
- 审计覆盖身份、成员、版本、策略和运行操作。
- Web UI 提供初始化、登录、Workspace 切换、治理中心、审批、版本、策略、成员和审计视图。

### 2.2 本期不包含

- OIDC、LDAP、企业 SSO、SCIM、外部数据库和分布式 Session。
- 跨实例高可用、组织计费、配额结算和跨 Workspace 资产共享。
- 知识库、技能、MCP、记忆和评测集的版本审批；这些资源仍受租户隔离、RBAC、策略和审计约束。

## 3. 总体架构

采用混合存储：

- `data/governance.sqlite`：用户、Workspace、成员、Session、策略、Agent/Flow 版本、发布指针和审计。
- `data/workspaces/<workspace-id>/`：该 Workspace 的已发布 Agent/Flow 物化文件，以及知识库、技能、MCP、记忆、评测、媒体和运行记录。
- `GovernanceStore` 负责事务和身份治理；`PolicyEngine` 负责纯函数式策略判断；`WorkspaceRuntime` 负责组装一个 Workspace 的现有存储与执行引擎。
- `Studio` 缓存 `WorkspaceRuntime`，但每次请求仍从已认证 Principal 解析 Workspace，不能从客户端传入的目录或资源路径构造存储。

现有顶层数据仅在迁移目标为空且迁移标记不存在时复制到 `data/workspaces/default/`。复制成功、版本登记成功后写迁移标记；旧目录保留，不做破坏性移动。

## 4. 身份认证与会话

首次访问未初始化实例时，Web UI 只允许访问静态资源、健康检查、初始化状态和 owner 初始化 API。初始化提交用户名、显示名和密码，事务内创建 owner、`default` Workspace、成员关系和初始策略。

密码使用标准库 `hashlib.scrypt`，每个密码使用独立随机 salt；数据库只保存算法、参数、salt 和派生结果。密码至少 10 个字符。登录成功后生成 256-bit 随机 token，数据库保存 token 的 SHA-256 摘要，浏览器只持有原 token。

Session Cookie：

- 名称 `flow_studio_session`。
- `HttpOnly`、`SameSite=Strict`、`Path=/`。
- 默认 12 小时绝对过期；退出时服务端删除 Session 并清 Cookie。
- HTTPS 请求设置 `Secure`；回环 HTTP 开发环境不设置 `Secure`。
- 每个 Session 携带独立 CSRF token。所有有副作用的 `/api/*` 请求必须发送 `X-CSRF-Token`，登录与初始化除外。

连续登录失败按用户名与来源地址计数：5 分钟内达到 5 次后锁定 10 分钟。成功登录清理失败计数。失败原因对外统一为“用户名或密码错误”，详细原因只进审计。

## 5. Workspace 与 RBAC

已登录请求使用 `X-Workspace-ID` 选择 Workspace。服务端验证用户成员关系后生成：

```text
Principal(user_id, username, workspace_id, role, session_id, request_id)
```

未指定时使用用户可访问的第一个 Workspace，优先 owner 关系。UI 保存上次选择，但服务端不信任该值。

权限使用能力名称而非散落的角色字符串：

| Capability | owner | admin | editor | viewer |
|---|---:|---:|---:|---:|
| `resource.read` | yes | yes | yes | yes |
| `runtime.execute` | yes | yes | yes | yes |
| `resource.write` | yes | yes | yes | no |
| `runtime.preview` | yes | yes | yes | no |
| `eval.execute` | yes | yes | yes | no |
| `release.approve` | yes | yes | no | no |
| `release.publish` | yes | yes | no | no |
| `release.rollback` | yes | yes | no | no |
| `member.manage` | yes | yes | no | no |
| `policy.manage` | yes | no | no | no |
| `workspace.manage` | yes | no | no | no |
| `audit.read` | yes | yes | no | no |

Admin 只能创建或调整 `editor/viewer`；owner 可授予 `admin`。owner 不能删除自己的最后一个 owner 关系。

## 6. Workspace 运行时与数据隔离

`WorkspaceRuntime` 在 `data/workspaces/<id>/` 下构造现有 `FlowStore`、`RunStore`、`AgentStore`、`KBStore`、`MemoryStore`、`SkillStore`、`MCPManager`、`EvalStore`、媒体和视频配置。所有现有业务 API 从 `request.state.workspace_runtime` 读取服务，不再使用全局默认存储。

Job Agent 注册表和 LLM/AgentRoam 配置仍是实例级配置；资产、记忆、知识和运行数据是 Workspace 级。跨 Workspace ID 即使同名也互不可见，Agent 引用 Flow、知识库、技能和 MCP 时只在当前 Workspace 解析。

## 7. Agent/Flow 版本模型

`resource_versions` 保存不可变快照：

```text
workspace_id, resource_type, resource_id, version_no,
status, action, snapshot_json, content_hash,
created_by, created_at, submitted_by, submitted_at,
approved_by, approved_at, rejected_by, rejected_at,
published_by, published_at, reason, rollback_of
```

- `resource_type` 仅允许 `agent`、`flow`。
- `action` 为 `upsert` 或 `delete`。
- 状态机为 `draft -> pending -> approved|rejected -> published`。
- 保存每次生成新的不可变 `draft` 版本；相同内容哈希重复保存返回现有最新草稿，不制造重复版本。
- 提交后快照不可修改；继续编辑会从该版本派生新草稿。
- 提交人不能批准自己的版本。若 Workspace 只有一个具有审批能力的成员，owner 可自批，审计标记 `self_approved=true`。
- 发布在一个 SQLite 事务中验证审批与策略、更新 `resource_releases` 指针、记录审计；事务成功后原子写临时文件并替换物化 JSON。若物化失败，补偿事务恢复旧发布指针并记录失败审计。
- 删除也是 `action=delete` 的版本；发布删除版本后移除物化文件，但历史仍可回滚。
- 回滚选择历史已发布版本，将其快照复制为新的版本并直接发布；新版本记录 `rollback_of` 和原因，旧历史不改变。

读取规则：viewer 和正式运行只读取发布指针；editor/admin/owner 的编辑视图优先显示自己或当前 Workspace 最新未终结版本，并明确显示状态。预览 API 必须显式携带 `version_no`。

## 8. 统一策略门禁

`PolicyEngine.evaluate(stage, resource_type, snapshot, context)` 返回：

```json
{"decision":"allow|warn|deny","violations":[{"code":"...","message":"...","path":"..."}]}
```

策略字段：

- `allowed_models`：空数组表示不限制。
- `denied_tools`、`denied_mcp_servers`。
- `allowed_flow_node_types`：空数组表示使用系统全部已注册节点。
- `max_agent_steps`：默认 12，硬范围 1–30。
- `allowed_http_hosts`：空数组表示不限制；非空时 Flow HTTP 节点只能访问清单主机。
- `require_approval`：默认 true。
- `required_eval_suite_id`：默认空；非空时发布前要求该评测集存在合格结果。
- `min_eval_pass_rate`：默认 1.0，范围 0–1。

门禁阶段：

- `submit`：检查 Agent 工具/MCP/步数与 Flow 节点/HTTP 主机；拒绝跨 Workspace 引用。
- `publish`：重新执行静态策略，验证审批状态和评测阈值。
- `preview`：执行静态策略；允许未发布版本，但不绕过工具、MCP、节点和网络限制。
- `run`：所有 Flow run、Agent invoke、chat 和工具式入口只解析已发布版本并再次应用当前策略。

策略更新后，旧批准不构成豁免。拒绝返回 HTTP 403 和稳定 `policy_code`；结构或状态冲突返回 409。

## 9. 审计

审计记录只追加，不提供更新或删除 API。事件至少包含：

```text
event_id, workspace_id, user_id, username, event_type,
resource_type, resource_id, version_no, request_id,
outcome, ip_address, details_json, created_at
```

覆盖初始化、登录、登录失败、退出、Session 失效、Workspace/成员/角色、策略、草稿、提交、批准、拒绝、发布、回滚、正式运行、预览和策略拒绝。`details_json` 必须经过敏感字段清理，不写密码、Session、CSRF、API Key、Cookie 或完整敏感提示词。

## 10. API

公共 API：

- `GET /api/setup/status`
- `POST /api/setup`
- `POST /api/auth/login`
- `POST /api/auth/logout`

身份与租户：

- `GET /api/me`
- `GET/POST /api/workspaces`
- `GET/POST /api/workspaces/{id}/members`
- `PUT/DELETE /api/workspaces/{id}/members/{user_id}`

治理：

- `GET/PUT /api/governance/policy`
- `GET /api/governance/audit`
- `GET /api/governance/approvals`
- `GET /api/governance/resources/{type}/{id}/versions`
- `POST /api/governance/resources/{type}/{id}/versions/{version}/submit`
- `POST /api/governance/resources/{type}/{id}/versions/{version}/approve`
- `POST /api/governance/resources/{type}/{id}/versions/{version}/reject`
- `POST /api/governance/resources/{type}/{id}/versions/{version}/publish`
- `POST /api/governance/resources/{type}/{id}/versions/{version}/rollback`
- `POST /api/governance/resources/{type}/{id}/versions/{version}/preview`

现有 Agent/Flow 保存和删除 API 改为创建草稿版本。现有正式运行 API 只执行当前发布版本。所有错误统一返回 `{detail, code, request_id}`。

## 11. Web UI

- 未初始化时显示 owner 初始化界面；未登录时显示登录界面，不渲染平台数据。
- 全局导航增加 Workspace 选择器、当前用户菜单和“治理”入口。
- Agent 卡片和 Flow 工具栏显示 `draft/pending/approved/published/rejected` 状态及版本号。
- 编辑保存后显示“提交审批”；admin/owner 在治理中心处理待审批版本并填写批准/拒绝原因。
- 治理中心用页签组织：概览、待审批、版本、成员、策略、审计。页面保持现有紧凑工作台风格，不增加营销式说明页。
- viewer 不渲染写入按钮；editor 不渲染审批/发布按钮；服务端仍独立校验所有权限。
- 401 回到登录界面；403 显示无权限；409 显示状态冲突并刷新资源状态；策略拒绝展示具体违规项。

## 12. 错误与一致性

- SQLite 使用 WAL、foreign keys 和显式事务；唯一约束防止重复用户名、成员和版本号。
- 文件物化使用同目录临时文件加 `os.replace`，避免半写文件。
- Session、CSRF、Workspace、RBAC、版本状态和策略依次校验；任何一步失败都不执行运行时或外部工具。
- 登录、审批、发布和回滚对并发请求保持幂等；已完成状态的重复请求返回当前结果，不重复写版本。
- 实例未初始化时，除初始化白名单外全部 API 返回 503 `setup_required`。

## 13. 测试与验收

单元测试覆盖密码哈希、Session/CSRF、登录锁定、RBAC 能力矩阵、Workspace 隔离、状态机、自批例外、内容去重、回滚 lineage、策略判断、敏感字段清理和迁移幂等。

API 集成测试覆盖：

- 首次初始化、登录/退出、401、CSRF 失败。
- 四角色对成员、策略、草稿、审批、发布、回滚、预览和正式运行的权限。
- 两个 Workspace 使用同名资源仍完全隔离。
- editor 提交、admin 批准发布、viewer 正式运行、admin 回滚的完整链路。
- 策略在 submit/publish/preview/run 四阶段拒绝违规资产。
- 旧资产迁移为 default Workspace v1 published 且重复启动不重复登记。

浏览器验收覆盖桌面和移动视口：首次初始化、登录、Workspace 切换、editor 提交、admin 审批发布、viewer 按钮隐藏、版本回滚、策略拒绝和审计查询。验证真实请求与页面状态，不以静态截图或 HTTP 200 替代交互验收。
