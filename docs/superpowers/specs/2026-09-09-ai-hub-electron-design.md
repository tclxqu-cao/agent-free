# AI Hub（Electron 多 AI 站点聚合桌面端）设计文档

日期：2026-09-09
状态：已确认

## 1. 目标与范围

做一个 Electron 桌面应用，把多个网页版 AI 聚合到一个窗口：

- **侧边栏**列出 AI 站点，点击切换（单屏模式）；
- **对比模式**下选 2-4 个站点并排分屏，分隔条可拖动；
- **同步发送**：底部输入条输入一次，自动填入并发送到当前分屏的多个 AI；
- **站点可配置**：内置 DeepSeek / Gemini / ChatGPT / Grok 预设，支持自定义添加任意网站（名称 + 网址 + 可选图标）；
- **登录态持久化**：每个站点独立会话分区，重启后保持登录；
- 外围能力：系统托盘常驻、全局快捷键唤起、深色模式。

明确不做（第一版）：对话内容导出、多窗口拆分、账号管理、自动更新、Windows/Linux 打包验证（配置预留）。

## 2. 总体架构

核心原则：**渲染进程只画外壳，永不直接操作网页内容**。嵌入页面是主进程创建的 `WebContentsView`；渲染进程只负责计算"每个格子应摆在哪"，通过 IPC 通知主进程。

```
┌─ 主进程 src/main
│   ├─ WindowManager      主窗口 BrowserWindow（hiddenInset 标题栏）
│   ├─ ViewPool           WebContentsView 池：每站点一个，惰性创建，隐藏不销毁
│   ├─ LayoutBridge       IPC: views:set-bounds → 逐视图 setBounds
│   ├─ BroadcastService   同步发送编排：适配器优先，剪贴板粘贴回退
│   ├─ ConfigStore        userData/config.json 原子读写
│   ├─ SessionManager     每站点 persist:<id> 分区 + UA 清洗
│   ├─ TrayService        托盘菜单（显示窗口 / 站点切换 / 退出）
│   ├─ ShortcutsService   全局快捷键注册与冲突处理
│   └─ ThemeService       nativeTheme.themeSource
├─ 预加载 src/preload      contextBridge 暴露白名单 API
└─ 渲染进程 src/renderer（Vue 3 + TypeScript + Pinia）
    ├─ Sidebar            站点列表、添加/编辑站点、单屏/对比模式切换
    ├─ SplitLayout        分屏区：占位 div + 可拖分隔条 + ResizeObserver
    └─ BroadcastBar       底部同步输入条（目标选择、发送状态角标）
```

脚手架：`electron-vite`。技术栈：Electron + Vue 3 + TypeScript + Pinia；打包用 electron-builder。

### 嵌入技术选型（已确认）

采用 **`WebContentsView` + 主进程视图管理**（Electron 官方当前推荐方式），而非已不推荐的 `<webview>` 标签或多窗口并排。理由：稳定性与性能最好；分屏即"设置矩形 bounds"；支持任意数量视图。代价是需要一层布局同步机制（见 §4）。

## 3. 数据模型与存储

```ts
interface SiteConfig {
  id: string        // 预设如 'deepseek'；自定义自动生成 nanoid
  name: string      // 显示名
  url: string       // 打开的网址
  icon?: string     // 内置图标 key（预设站点图标为打包在应用内的 SVG 资源）或 dataURL；缺省用首字母头像
  adapter?: string  // 适配器 id；预设自带，自定义默认 'generic'
}

interface AppSettings {
  theme: 'system' | 'light' | 'dark'   // 默认 'system'
  globalShortcut: string               // 默认 'CmdOrCtrl+Shift+A'
  closeToTray: boolean                 // 默认 true
}

interface AppConfig {
  sites: SiteConfig[]
  settings: AppSettings
}
```

- 预设站点：deepseek（`https://chat.deepseek.com/`）、gemini（`https://gemini.google.com/app`）、chatgpt（`https://chatgpt.com/`）、grok（`https://grok.com/`）。预设是纯数据，扩展只需加条目。
- 存储路径：`app.getPath('userData')/config.json`，原子写（临时文件 + rename）。
- 文件损坏：备份为 `config.json.bak` 后重置默认并提示。

## 4. 视图管理与分屏布局

- **生命周期**：首次打开站点时创建其 WebContentsView；切换/退出对比模式只隐藏不销毁（保留页面状态与登录）；侧边栏提供"关闭页面"主动销毁视图。
- **会话隔离**：每站点 `session.fromPartition('persist:<id>')`；UA 去除 `Electron/...` 特征串，避免站点拦截。
- **布局模式**：
  - 单屏：1 个站点占满内容区；
  - 对比：2-4 个站点等宽并排，分隔条可拖动调整相邻列比例（比例存内存即可，第一版不持久化）。
- **布局同步协议**：渲染进程为每个格子渲染空 div，`ResizeObserver` + rAF 节流，计算各格子在窗口视口内的矩形，IPC 推送 `views:set-bounds { panes: [{ siteId, x, y, width, height }] }`；主进程应用到对应视图。因为矩形按"剩余空间"计算，视图天然不会遮挡标题栏与底部输入条。
- **BroadcastBar**：吸底悬浮，可一键收起（收起后布局重算，网页占满）。
- **z 序**：主进程维护 addChildView 顺序；视图之间无重叠，无需额外层级管理。

## 5. 同步发送（核心机制）

流程：BroadcastBar 输入 → 选择目标（默认当前分屏全部站点）→ BroadcastService 逐站点执行：

1. **站点适配器**（`webContents.executeJavaScript` 注入）：
   - 定位输入框：站点特定 selector；generic 适配器取可见的最大 `textarea` / `[contenteditable="true"]`；
   - 写入文本：React/Vue 受控组件用 native value setter + 派发 `input` 事件；contenteditable 用 `document.execCommand('insertText')`；
   - 触发发送：站点特定发送按钮 selector，或向输入框派发 Enter keydown/keyup。
2. **回退：剪贴板粘贴模拟**：适配器任一步失败即走此路径——保存剪贴板 → 写入待发文本 → `webContents.focus()` + `webContents.paste()` → 模拟 Enter → 恢复剪贴板。该路径不依赖站点 DOM，保证可用性底线。
3. **结果反馈**：每站点返回 `{ siteId, ok, reason? }`，对应格子显示发送状态角标。

内置适配器模块：`adapters/deepseek.ts`、`adapters/chatgpt.ts`、`adapters/gemini.ts`、`adapters/grok.ts`、`adapters/generic.ts`。适配器以纯函数形式编写并编译为字符串注入，便于站点改版时单独维护。

已知风险（接受并在手测中验证）：站点 DOM 改版导致对应适配器失效（回退路径兜底）；Gemini 需谷歌登录且风控较严；自动化输入可能触发个别站点风控。

## 6. 托盘 / 快捷键 / 深色模式

- **托盘**：`closeToTray` 开启时（默认开），关闭窗口 = 隐藏到托盘并首次提示；托盘菜单：显示主窗口、各站点快速切换、退出（退出时真正退出应用）。
- **全局快捷键**：`globalShortcut` 注册唤起/隐藏切换键，设置页可修改或清除；注册冲突时提示但不阻塞启动。
- **深色模式**：`nativeTheme.themeSource = settings.theme`，嵌入页面通过 `prefers-color-scheme` 自动适配；外壳 UI 用 CSS 变量 + `.dark` class 切换。

## 7. IPC 面（preload 白名单）

| 通道 | 方向 | 用途 |
|---|---|---|
| `config:get` / `config:set` | R→M | 读写配置（站点 + 设置） |
| `views:open` / `views:close` / `views:hide` | R→M | 打开/销毁/隐藏站点视图 |
| `views:set-bounds` | R→M | 推送分屏布局矩形 |
| `views:navigate` / `views:reload` | R→M | 站点内导航 / 刷新 |
| `views:events` | M→R | did-fail-load / 加载完成 / 页面标题变化等回推 |
| `broadcast:send` | R→M | 同步发送，回推逐站点结果 |
| `tray:*` / `shortcuts:set` / `theme:set` | R→M | 外围设置 |
| `app:show` / `app:hide` | M→R | 托盘/快捷键触发窗口行为 |

## 8. 错误处理

- 站点加载失败（断网等）：格子覆盖错误层 + 重试按钮（渲染进程渲染，因其掌握格子矩形）；
- 渲染进程崩溃：主进程捕获并重新加载该视图，格子内提示；
- 配置损坏：备份后重置默认并提示；
- 发送失败：逐站点显示原因，不影响其他站点；
- 快捷键注册失败：提示冲突，应用正常运行。

## 9. 测试与验收

- 单元测试（vitest）：ConfigStore 读写/损坏恢复、布局矩形计算纯函数、适配器脚本字符串生成。
- 手动验收清单：
  1. 四个预设站点可登录，重启后登录态保持；
  2. 单屏切换、对比模式 2-4 站点、分隔条拖动；
  3. 同步发送到多站点成功；适配器故意失效时回退粘贴路径可用；
  4. 托盘关闭/唤起、全局快捷键、深浅色切换（外壳 + 嵌入页面）；
  5. 自定义站点添加/编辑/删除、图标缺省首字母头像。
- 打包：electron-builder → macOS arm64 dmg；Windows 目标配置预留不验证。
