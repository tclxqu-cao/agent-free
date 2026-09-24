# 个人主页中文命名与作品媒体实施计划

> 设计依据：`docs/superpowers/specs/2026-09-24-homepage-flow-chinese-labels-design.md`

## 目标

在不改变流程内部 ID、路由、参数和连线的前提下，将 `homepage-main` 及其两个智能体的显示名称改为中文；同时让个人主页执行 `/works` 时展示现有项目图片和视频，而不依赖模型临时生成静态资源地址。

## 任务 1：锁定中文显示名契约

**文件**

- 修改：`src/flow_studio/homepage_flows.py`
- 修改：`src/flow_studio/default_agents.json`
- 修改：`tests/test_homepage_flow.py`
- 修改：`tests/test_flow_builtin.py`

**步骤**

1. 为流程名、26 个节点标签和两个智能体名称补充精确断言。
2. 按设计规格更新内置定义，只修改面向用户的 `name`/`label`。
3. 运行聚焦测试，确认节点 ID、类型、参数、位置、边和智能体能力配置未变化。

## 任务 2：由 `portfolio-works` Skill 返回媒体区块

**文件**

- 修改：`/Users/caoqu/team-agent/customer-agent/.agent/skills/portfolio-works/SKILL.md`
- 修改：`/Users/caoqu/team-agent/customer-agent/packages/server/lib/portfolio-content-agent.test.ts`
- 验证：`src/flow_studio/homepage_artifacts.py`
- 验证：`/Users/caoqu/.zcode/workspace/default/portfolio/portfolio-core.js`
- 验证：`/Users/caoqu/.zcode/workspace/default/portfolio/main.js`

**步骤**

1. 明确 `/works` 制品从 Customer Agent Skill 返回、校验、缓存、导出到浏览器渲染的完整路径。
2. `portfolio-works` Skill 返回可点击的项目作品墙；每个项目卡片通过受限 `data-command` 发出 `/project PROJECT_ID`。
3. 对应 `portfolio-project-*` Skill 返回项目图片、视频或流程图，媒体数据不进入 Flow 和前端代码。
4. Flow Studio 只按统一制品协议校验、缓存和透传，不按 Skill ID 注入媒体。
5. 主页通用 HTML 清洗器保留受控样式类和项目命令按钮，通用媒体渲染器展示详情 Skill 的 `image`/`video` blocks。
6. 补充 Skill、清洗和交互契约测试，覆盖作品墙结构、命令白名单及详情媒体路径。

## 任务 3：更新当前物化数据并发布治理版本

**文件/数据**

- 修改：`data/workspaces/default/flows/homepage-main.json`
- 修改：`data/workspaces/default/ai_agents/homepage-chat-agent.json`
- 修改：`data/workspaces/default/ai_agents/portfolio-content-agent.json`
- 通过治理 API 新建并发布 `homepage-main` 版本

**步骤**

1. 从更新后的内置定义生成当前物化流程，确保只有名称和标签变化。
2. 更新两个物化智能体的 `name`，保留模型、技能、工具、MCP、记忆和连接配置。
3. 通过现有治理 API 创建、提交、审批并发布新版本，不覆盖历史版本。
4. 用管理 API 核对已发布快照与物化数据一致。

## 任务 4：刷新制品并验证运行实例

**命令与验证**

1. 运行：`.venv/bin/pytest tests/test_homepage_flow.py tests/test_flow_builtin.py`。
2. 运行相关前端制品测试；若修改静态前端，再执行对应 Node 测试。
3. 执行：`git diff --check`。
4. 通过 `:8788` 实际接口刷新 `/works`，确认缓存制品同时含项目表格、图片和视频区块。
5. 重新导出 `content-snapshot.js`，确保刷新页面后的静态回退内容一致。
6. 使用浏览器验收 `:8788` 的中文流程/节点/智能体，以及 `:8801` 的 `/works` 图片和视频实际加载与播放控件。
7. 验证普通聊天、`/works` 和一个项目 Slash 命令仍走原有分支。

## 风险控制

- 保留当前工作区已有未提交修改，不格式化或重写无关文件。
- 不改变内部 ID 和协议字段。
- 媒体只引用个人主页公开 `assets/` 路径，不暴露本机绝对路径。
- 当前服务存在活动执行时，避免中断；只有代码不能热加载且确需验证时才重启现有服务。
