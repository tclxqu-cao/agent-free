# Flow Studio 视频制作画布（无限画布 + 视频流水线节点）设计文档

日期：2026-09-11
状态：已确认（用户授权自主决策，"后续都按最佳方案推进，不用同意"）
前置：`2026-09-11-flow-studio-design.md`（编排画布第一版：节点画布 / 引擎 / 意图 / 内置应聘流程）

## 1. 目标与范围

在 flow_studio 上扩展「AI 视频制作」能力：

- **视频流水线节点**：分镜（storyboard）、角色设定图（character，多方位）、关键帧（keyframe）、镜头视频（shot_video，图生视频）、合成（merge_video，多段合一条）、素材引用（asset）；
- **模型配置**：画布内可视化配置三类模型——文案/分镜 LLM（可继承 config.llm）、文生图、图生视频（OpenAI 兼容 + 通用视频任务两段式：提交 + 轮询）；
- **素材管理**：统一素材库（角色图 / 关键帧 / 视频片段 / 成片 / 上传文件），SQLite 元数据 + 文件落盘，画布素材面板上传/预览/删除；
- **比例贯穿**：aspect_ratio（16:9 / 9:16 / 1:1）从分镜 → 关键帧 → 镜头视频 → 合成全程携带，merge 校验一致；
- **无限画布与性能**：视口裁剪（视口外节点不进 DOM）、连线增量更新（拖动只重画相连边）、rAF 合帧、SVG overflow:visible 真·无边界、小地图导航；
- **可降级**：无任何模型 key 也能端到端跑通——LLM 失败降级规则切分镜，图片/视频生成降级为占位素材（纯 Python PNG；ffmpeg 生成 MP4 片段），合成依赖 ffmpeg（本机已确认 8.0 可用）。

不做（第一版）：视频转场特效、配音/字幕烧录、参考图 API 适配（角色一致性第一版靠角色描述注入 prompt；预置 `image.ref_endpoint` 配置钩子）、多用户鉴权。

## 2. 流水线（业界通用范式，调研即梦/LTX Studio/Runway/ComfyUI 后确认）

```
剧情提示词 ──► 分镜表（shot list JSON）──► 角色设定图（character sheet，多方位）
                    │                              │
                    ▼                              ▼
              关键帧图（每镜首帧，文生图）◄── 角色描述注入保证一致性
                    │
                    ▼
              镜头视频片段（图生视频，含运镜）──► ffmpeg concat 合成长片（成片入库）
```

分镜表 JSON 字段（`storyboard.shots[]`）：`index`（镜头号）、`title`、`duration`（秒）、
`shot_size`（景别：远景/全景/中景/近景/特写）、`camera`（运镜：推/拉/摇/移/固定…）、
`desc`（画面描述）、`image_prompt`（首帧绘图提示词）、`narration`（旁白/台词）。

## 3. 模块划分

| 模块 | 职责 |
|---|---|
| `video.py` | `VideoModels`（模型配置，`data/video_models.json` 读写）；`generate_image` / `generate_video`（OpenAI 兼容适配 + 失败降级占位）；`placeholder_png`（纯 Python PNG，零依赖）；`concat_videos`（ffmpeg concat，重编码统一参数）；`probe_duration` |
| `assets.py` | `AssetStore`：`data/media/assets.sqlite`（assets 表）+ `data/media/files/`；add/list/get/delete；`/media/files/*` 静态服务 |
| `video_nodes.py` | 6 个视频节点的执行逻辑（引擎 handler 委托）；批处理语义：keyframe / shot_video 节点内部循环处理上游 list，不引入引擎级循环节点 |
| `graph.py` | NODE_TYPES 新增 6 类 + 表单元数据（含 select widget、方位列表编辑器） |
| `engine.py` | FlowRunner 增加可选 `media` / `vmodels` 上下文；新类型 handler 委托 video_nodes |
| `server.py` | 素材 CRUD / 上传（multipart）/ 静态媒体 / 模型配置 GET/PUT |
| `web/` | 节点面板分组（基础 / 视频制作）；模型设置弹窗；素材库面板；节点输出内嵌图片/视频预览；无限画布性能优化 + 小地图 |
| `builtin_flows.py` | 内置 `video-demo` 电影短片流水线（全降级可跑通） |

## 4. 节点规格

| 节点 | 关键参数 | 输出 |
|---|---|---|
| `storyboard` 分镜 🎬 | `story`（剧情，模板）、`shot_count`（默认 4）、`aspect_ratio`（默认 16:9）、`style`（画面风格） | `{shots[], count, aspect_ratio, via: llm/fallback, text}` |
| `character` 角色 👤 | `name`、`description`、`views[]`（默认 正面/左侧/右侧/背面）、`style`、`aspect_ratio` | `{views:[{view, asset_id, path}], count}` |
| `keyframe` 关键帧 🖼 | `shots_source`（模板，默认 `{{storyboard.shots}}`）、`characters_source`（角色描述注入，可选）、`style` | `{frames:[{index, asset_id, path}], count}` |
| `shot_video` 镜头视频 🎥 | `frames_source`（默认 `{{keyframe.frames}}`）、`shots_source`（取运镜/描述拼 motion prompt）、`duration`（每镜秒，默认 3）、`aspect_ratio` | `{clips:[{index, asset_id, path, duration}], total_duration, mode}` |
| `merge_video` 合成 🧩 | `clips_source`（默认 `{{shot_video.clips}}`）、`title`、`enforce_ratio`（默认 true） | `{asset_id, path, duration, count, aspect_ratio}` |
| `asset` 素材 📦 | `asset_id`（素材面板选择） | `{asset_id, kind, path, name, meta}` |

生成语义（所有生成类节点一致）：
1. 模型已配置且调用成功 → 真实素材入库，`mode: "model"`；
2. 模型未配置 / 调用失败 → **占位素材**入库（PNG 占位图；ffmpeg testsrc 占位视频，无 ffmpeg 时该节点按 `optional` 降级 skipped），`mode: "placeholder"`，流水线不断；
3. 素材 meta 记录 `{aspect_ratio, duration, prompt, mode}`，merge 据此校验比例。

视频模型 API 约定（`data/video_models.json` 可配 path，兼容硅基流动/云雾等 OpenAI 风格中转）：
提交 `POST {base_url}{submit_path}`（默认 `/videos/generations`）→ 响应取 `id|task_id`；
轮询 `GET {base_url}{poll_path}`（默认 `/videos/{id}`）→ `status` 完成（succeeded/success/completed）后取 `video_url|output.url|url|data[0].url` 下载。

## 5. 无限画布性能（前端）

- **视口裁剪**：渲染时只把视口（±300px margin）内的节点写入 DOM，其余标记 `hidden`，平移/缩放时按需显隐——百级节点下 DOM 常量；
- **连线增量更新**：每条边持有持久 SVG `<g>`，`updateEdgesFor(nodeId)` 只重算相连边；新增/删除边做 diff 而非 innerHTML 全量重建；
- **rAF 合帧**：pointermove 的画布重绘统一 `requestAnimationFrame` 节流；
- **真·无边界**：SVG `overflow: visible`，去掉固定 4000×3000 尺寸；缩放范围放宽 0.2–2.5；
- **小地图**：右下角 canvas 概览（节点矩形 + 视口框），点击/拖动跳转视角；
- **fitView 用数据计算**（不再读 DOM offsetHeight），拖动时节点宽高走缓存。

## 6. API 增量

| 方法/路径 | 说明 |
|---|---|
| `GET /api/assets?kind=&flow_id=` | 素材列表 |
| `POST /api/assets/upload` | multipart 上传（file/name/kind/flow_id） |
| `DELETE /api/assets/{id}` | 删除（含文件） |
| `GET /media/files/{name}` | 静态媒体（basename 白名单防穿越） |
| `GET /api/video/models` / `PUT` | 模型配置读写（`data/video_models.json`） |

## 7. 测试

- 单元：占位 PNG（magic/尺寸）、分镜降级切分 / LLM mock、批处理循环数量、比例校验、模型配置读写；
- 集成：AssetStore CRUD + 上传 + 路径穿越防护；concat 真实 ffmpeg 产 MP4（无 ffmpeg skip）；
- **端到端**：内置 video-demo 全流程（无 LLM 无 key 全占位降级）跑通，断言成片文件存在、时长≈各镜之和、素材计数正确；FastAPI TestClient 覆盖全部新 API；
- **GUI e2e**：浏览器黑盒验证——节点面板分组、添加/连线视频节点、运行 video-demo、素材面板预览、模型设置保存、300 节点压测下平移/缩放/框选流畅（测 FPS 与交互延迟）。
