# Job Agent（智能应聘 Agent）设计文档

日期：2026-09-10
状态：已确认（用户授权自主决策，无需逐步确认）

## 1. 目标与范围

做一个单机单用户的智能应聘 CLI Agent：

- **多站点自动登录**：支持 Boss直聘、智联招聘、猎聘、前程无忧(51job)、拉勾；首次手动扫码登录，登录态持久化，之后自动复用；
- **按规则/提示词抓取岗位**：配置关键词与过滤规则，抓取岗位并抽取字段：岗位职责、年限要求、地址、学历、薪资、特殊要求、紧急性、浏览量、招呼量、公司规模、公司距家距离；
- **每日抓取**：定时全量抓取，逐日快照存库；
- **变化分析**：岗位统计数量变化、岗位整体要求（薪资/学历/年限/技能词频）变化、单岗位热度变化（浏览量/招呼量增量）；
- **结合个人经历的顾问**：匹配岗位推荐、技能缺口学习清单、"更上一层"所需能力、未来岗位趋势展望。

明确不做（第一版）：自动投递/自动打招呼（风控与误投风险高，预留接口位）、GUI、多用户、移动端。

## 2. 技术选型决策

用户授权在 workflow / agent loop / LangGraph 中任选。**选择：确定性 workflow 主干 + LLM 分析节点，不引入 LangGraph**。

理由：
- 每日抓取是定时批处理，要求可重跑、可断点、行为可预测，agent loop 的自由决策带来不确定性且无必要；
- LLM 只在三个分析节点出现（语义匹配、学习建议、趋势展望），通过 OpenAI 兼容 API（httpx 直连）调用，可配置可关闭；
- 无 LLM key 时全链路降级为规则版仍可用；
- 少依赖（playwright / pyyaml / httpx / pytest），SQLite 免部署。

技术栈：Python 3.13 + uv，Playwright(sync API, Chromium)，SQLite(stdlib)，PyYAML，httpx，pytest。

## 3. 总体架构

```
config/config.yaml   config/profile.yaml
        │
        ▼
┌─────────────────────── daily workflow ────────────────────────┐
│ 1. scrape   各站点 adapter 抓取搜索结果+详情（登录态复用）        │
│ 2. normalize 薪资/年限/学历/距离 归一化                          │
│ 3. store     SQLite 逐日快照（jobs / job_daily / keywords_daily）│
│ 4. match     规则硬过滤+软评分（可选 LLM 语义二次过滤）           │
│ 5. analyze   数量趋势 / 要求变化 / 热度变化                      │
│ 6. advise    LLM：匹配度、学习清单、进阶差距、趋势（无LLM则规则版）│
│ 7. report    output/YYYY-MM-DD/report.md                        │
└────────────────────────────────────────────────────────────────┘
```

模块划分（`src/job_agent/`）：

| 模块 | 职责 |
|---|---|
| `models.py` | `Job` dataclass 与字段定义 |
| `db.py` | SQLite 建表、upsert、逐日快照、趋势查询 |
| `normalize.py` | 薪资("25-40K·16薪")、年限("3-5年")、学历、技能词、紧急性解析 |
| `distance.py` | home 坐标 + 高德(可选)/Nominatim/区级中心表 兜底 + haversine，geocode 缓存 |
| `browser.py` | Playwright 封装：storage_state 持久化、登录检测、随机延时、HTML dump |
| `sites/base.py` | `BaseSite` 抽象：login_url / search / parse / logged_in |
| `sites/{boss,zhilian,liepin,job51,lagou}.py` | 各站点 adapter（选择器集中在类顶部便于修正） |
| `match.py` | 规则匹配：硬过滤 + 0-100 评分 |
| `analysis.py` | 数量/薪资/学历/年限/技能词频趋势，新增/消失岗位，热度增量 |
| `advisor.py` | LLM 顾问 + 无 LLM 规则降级 |
| `report.py` | Markdown 日报生成 |
| `scheduler.py` | `watch` 每日定时循环 |
| `demo.py` | 生成模拟数据，无凭据端到端验证 |
| `cli.py` | login / login-status / scrape / analyze / report / daily / watch / demo |

## 4. 数据设计

SQLite（`data/job_agent.db`）：

- `jobs`：job_id(site+url 哈希)、site、title、company、company_size、industry、salary_min/max(K)、salary_text、city、district、address、experience_min/max(年)、education、responsibilities(JSON)、requirements_extra(JSON)、skills(JSON)、urgency、url、first_seen、last_seen、extra(JSON: views/greets/hr 等)；
- `job_daily`：(date, job_id, salary_min, salary_max, views, greets, distance_km, status[active/missing]) —— 逐日快照，支撑变化分析与消失检测；
- `keywords_daily`：(date, keyword, site, total_count) —— 搜索结果总数趋势；
- `runs`：每次抓取的站点/关键词/数量/耗时/错误；
- `geocode_cache`：地址 → (lat, lng)。

字段可得性说明：各站点展示字段不同（如浏览量/招呼量仅部分站点提供），缺失一律存 NULL，报告标注来源站点。

## 5. 关键机制

- **登录**：`job-agent login --site boss` 打开有头浏览器，用户手动扫码登录一次，storage_state 存 `data/auth/<site>.json`；抓取时检测登录态失效则中止该站点并在报告中提示重新登录。风控对策：每日一次低频、关键词数量有限、随机 2-6s 延时、真实 UA、失败时 dump HTML 供调试；README 说明选择器可能随站点改版需微调。
- **距离**：config 配 home 坐标（取一次）；地址 geocode 优先高德 key（可选），否则 Nominatim；均失败用内置区级中心表模糊匹配；结果缓存；仍失败 distance=NULL。
- **紧急性**：规则信号（"急聘"/"招满即止"/发布 3 天内+热度）判 high/medium/low。
- **匹配**：config.rules 硬过滤（薪资下限、城市、学历、排除词"外包/驻场"等、最远距离）+ 软评分（薪资 30 / 距离 20 / 技能重合 30 / 经验吻合 20）；prompt 自由文本在 LLM 开启时做语义二次过滤与排序。
- **调度**：`daily` 单次全流程；`watch --at 08:30` 内置循环；README 提供 launchd/crontab 示例。

## 6. 错误处理

单站点失败不影响其他站点（run 日志 + 报告"抓取异常"节）；LLM 超时/失败自动降级规则版；解析失败字段存 NULL 不中断；DB 写入事务化。

## 7. 测试

pytest：normalize（薪资/年限/学历/技能/紧急性样例）、match（过滤与评分）、analysis（用 fixture 数据算趋势/增量/消失检测）、db（upsert/快照幂等）、sites（HTML fixture 片段解析）、advisor（无 LLM 降级路径）。`job-agent demo` 生成多日模拟数据走通 scrape→report 全链路。

## 8. 假设记录（自主决策）

1. 用户已授权技术选型与实现细节自主决策（"按最佳方案实现，不用同意"）；
2. "智能应聘"范围按需求条目界定为抓取+分析+推荐，不含自动投递；
3. 本机无招聘网站凭据，真实抓取需用户首次 `login`，站点改版可能需微调 adapter 选择器（已内置 dump/调试手段）；demo 模式保证无凭据可验证全链路；
4. LLM 走 OpenAI 兼容 API（base_url/model/key 可配，兼容 DeepSeek/GLM 等），默认关闭。
