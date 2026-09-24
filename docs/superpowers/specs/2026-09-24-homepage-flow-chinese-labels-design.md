# 个人主页流程中文命名设计

## 目标

将个人主页当前生效的 `homepage-main` 流程中所有面向用户的名称统一为中文，包括流程名称、26 个节点显示名称，以及流程引用的两个智能体显示名称。内部契约保持不变，避免影响路由、Skill 调用、会话续接和已发布主页行为。

## 范围

- 流程名称：`Portfolio · Main Skill Flow` 改为 `个人主页 · 主流程`。
- 智能体名称：
  - `Homepage Chat Agent` 改为 `个人主页聊天智能体`。
  - `Portfolio Content Agent` 改为 `个人主页内容智能体`。
- `homepage-main` 的 26 个节点 `label` 全部改为中文。
- 品牌名和正式项目名（例如 `AgentRoam`、`Agent Swarms`、`Flow Studio`）保留官方写法，节点中的功能词使用中文。

不修改节点 `id`、智能体 `id`、Skill ID、Slash 命令、意图分支名、边、节点参数、坐标和执行逻辑。

## 节点名称

| 节点 ID | 中文显示名称 |
| --- | --- |
| `start` | 主页访客输入 |
| `command` | 精确命令路由 |
| `slash_route` | 未知命令检测 |
| `slash_guard` | 是否为 Slash 命令 |
| `clarify` | 未知命令引导 |
| `intent` | 自然语言意图识别 |
| `help` | 帮助内容 |
| `whoami` | 个人介绍 |
| `timeline` | 经历时间线 |
| `contact` | 联系方式 |
| `works` | 项目作品 |
| `job_snapshot` | 读取岗位快照 |
| `jobs` | 格式化岗位结果 |
| `project_agentroam` | 项目 · AgentRoam |
| `project_knowledge_base` | 项目 · 知识库 |
| `project_smart_refund` | 项目 · 智能赔付 |
| `project_agent_swarms` | 项目 · Agent Swarms |
| `project_quality_platform` | 项目 · 质量平台 |
| `project_customer_service` | 项目 · 客服系统 |
| `project_flow_studio` | 项目 · Flow Studio |
| `project_meitu_web` | 项目 · 美图工坊 |
| `project_vibe_works` | 项目 · Vibe 作品馆 |
| `project_kid_earth` | 项目 · 小小地球侦探队 |
| `chat` | 主页只读聊天 |
| `format_chat` | 格式化聊天结果 |
| `end` | 主页展示结果 |

## 实现边界

同时更新三层数据，避免重启或重新初始化后回退：

1. 内置默认定义，作为新数据目录和缺失资产补种的来源。
2. `data/workspaces/default` 中当前物化的流程与智能体资产。
3. 治理数据库中的新版本及发布指针，使个人主页读取的发布快照立即生效。

治理写入沿用 Flow Studio 现有版本流程，不直接覆盖历史版本；历史版本继续保留原名称，便于审计和回滚。

## 验证

- 对比变更前后图结构：节点 ID、类型、参数、位置和边完全一致，只有流程名及节点 `label` 改变。
- 验证两个智能体除 `name` 外的模型、能力集、记忆和连接配置完全一致。
- 执行相关单元测试和 `git diff --check`。
- 通过运行中 `:8788` API确认发布版名称，再用浏览器打开 Flow Studio，检查流程选择器、画布 26 个节点及智能体列表均显示中文。
- 从个人主页执行一条普通聊天和一条 Slash 命令，确认路由及输出不受命名修改影响。
