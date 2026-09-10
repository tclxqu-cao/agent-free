# Job Agent · 智能应聘 Agent

单机单用户的求职监控与分析工具：自动复用招聘网站登录态，每天按你的规则抓取岗位，逐日快照做趋势分析，并结合你的个人经历给出**匹配岗位、学习清单、进阶差距、未来趋势**的每日日报。

架构：**确定性 workflow 主干 + 可选 LLM 分析节点**（不依赖 LangGraph/Agent Loop——每日批处理要的是可靠可重跑；LLM 只用于语义点评/学习建议/趋势判断，可随时关闭降级为规则引擎）。设计与决策记录见 `docs/superpowers/specs/2026-09-10-job-agent-design.md`。

## 功能

- **多站点**：Boss直聘 / 智联招聘 / 猎聘 / 前程无忧 / 拉勾，登录态持久化（storage_state），失效自动检测并提示
- **岗位字段**：岗位职责、年限要求、地址、学历、薪资（归一化为月薪 K）、特殊要求、紧急性、浏览量、招呼量、公司规模、公司距家距离（高德/OSM/内置区表三级 geocode + 缓存）；站点不提供的字段存 NULL
- **每日抓取**：关键词 × 站点全量抓取，SQLite 逐日快照（jobs / job_daily / keywords_daily）
- **趋势分析**：岗位数量变化（活跃/新增/下架、关键词结果总数）、要求变化（薪资均值、学历/年限分布周对比、技能词频周环比）、单岗位热度（浏览/招呼增量）
- **匹配引擎**：规则硬过滤（薪资/城市/学历/排除词/最远距离/年限）+ 0-100 评分（薪资30/距离20/技能重合30/经验贴合20）
- **顾问**：LLM（OpenAI 兼容 API，DeepSeek/GLM 等）输出逐岗位点评、学习清单、进阶建议、趋势判断；无 key 时规则引擎兜底
- **日报**：`output/<日期>/report.md`，含上述全部内容

## 快速开始

```bash
uv sync                          # 安装依赖
uv run playwright install chromium
uv run job-agent init            # 生成 config/config.yaml 与 config/profile.yaml

# 编辑两个配置文件：
#   config/config.yaml  家的坐标(取一次)、搜索关键词、过滤规则、启用站点、LLM
#   config/profile.yaml 你的技能/经历/期望/目标岗位（供匹配与建议用）

uv run job-agent login --site boss       # 逐站点扫码登录一次（有头浏览器）
uv run job-agent daily                   # 全流程：抓取→分析→建议→日报
open output/$(date +%F)/report.md
```

先体验可跑模拟数据（无需任何凭据）：`uv run job-agent demo`，生成 10 天确定性模拟数据并出一份完整日报。

## CLI 一览

| 命令 | 说明 |
|---|---|
| `init` | 生成配置文件 |
| `login --site boss` | 有头浏览器手动登录并保存登录态（boss/zhilian/liepin/job51/lagou） |
| `login-status` | 检查各站点登录态是否有效 |
| `scrape [--site x]` | 只抓取入库，不出报告 |
| `analyze [--days 30]` | 终端输出趋势分析 |
| `report [--date]` | 用库中数据重新生成指定日期报告 |
| `daily [--skip-scrape]` | 每日全流程 |
| `watch` | 常驻定时（`config.yaml: schedule.at`）执行 daily |
| `demo [--days 10]` | 模拟数据端到端演示 |

`--config-dir` 放在子命令前后均可。

## 每日定时

方式一：`uv run job-agent watch` 常驻。

方式二（推荐，机器重启也不丢）：launchd 或 crontab——

```bash
# crontab：每天 08:30
30 8 * * * cd /path/to/agent-free && /Users/caoqu/.local/bin/uv run job-agent daily >> logs/cron.log 2>&1
```

## 匹配规则（config.yaml → rules）

```yaml
rules:
  salary_min: 25            # 薪资上限低于它 → 淘汰
  experience_max: 10        # 年限下限超过它 → 淘汰
  education_allow: [不限, 大专, 本科]
  cities: [苏州]
  exclude_keywords: [外包, 驻场, 培训]
  max_distance_km: 50       # 距离未知时不淘汰
  expected_salary: 30       # 打分基准
```

自由文本"提示词"需求：把方向写进 `search.keywords`（结构化召回）+ `profile.expectations.directions`（LLM 语义过滤与点评）。LLM 关闭时按规则引擎执行。

## LLM 配置（可选）

任何 OpenAI 兼容服务均可：

```yaml
llm:
  enabled: true
  base_url: https://api.deepseek.com/v1
  api_key: sk-xxx
  model: deepseek-chat
  timeout: 90
```

失败/超时自动降级规则引擎，日报尾部标注建议来源。

## 字段可得性

各站点展示不同：浏览量/招呼量仅部分站点提供；职责/要求需进详情页（`search.fetch_detail: true`）。缺失字段在报告中显示 "—"。距离按"精确地址 > 区中心 > 市中心"精度递归兜底，`raw` 中记录 `distance_basis`。

## 风控与维护说明（重要）

- 招聘网站反爬较强且 DOM 经常改版。适配器把**选择器集中在各类顶部的 `SEL/CARD` 常量**里，改版时只需更新对应站点的几个选择器；抓取失败会自动把页面 HTML dump 到 `data/dumps/` 便于排查。
- 内置对策：真实 UA、每日一次低频、关键词数量可控、动作间 2-6s 随机延时、单站点失败不影响其余。若触发验证码，请在 `login` 的有头浏览器里手动通过；个人求职自用频率下通常足够稳定。
- 请遵守各网站用户协议，仅用于本人求职场景。

## 测试

```bash
uv run pytest tests/ -v      # 78 个用例：解析/匹配/趋势/存储/适配器解析/降级/端到端
uv run job-agent demo        # 端到端演示
```

## 目录

```
src/job_agent/
  models.py 配置加载与 Job 模型     db.py SQLite 存储与逐日快照
  normalize.py 字段归一化           distance.py 距离与 geocode 链
  browser.py Playwright/登录态      sites/ 站点适配器（base+5站点）+ scrape_all
  match.py 规则匹配                 analysis.py 趋势分析
  advisor.py LLM顾问+规则降级       report.py 日报  scheduler.py 调度
  demo.py 模拟数据                  cli.py 命令行
data/     运行数据（db、auth 登录态、dumps）—— gitignore
output/   每日日报 —— gitignore
```

## 后续扩展点（预留）

- 自动投递/打招呼：`sites/base.py` 增加站点 `apply()` 接口位即可（需自行评估风控与误投风险）
- 企业微信/邮件日报推送：在 `scheduler.run_daily` 末尾接入 webhook
- Web 界面：SQLite 单文件，直接套一层 FastAPI + 图表即可
