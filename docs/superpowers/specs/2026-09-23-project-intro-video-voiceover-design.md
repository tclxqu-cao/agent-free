# Flow Studio 项目介绍视频与 team-agent 配音设计

日期：2026-09-23
状态：已确认
前置：`2026-09-11-flow-video-pipeline-design.md`

## 1. 目标

在现有 Flow Studio 视频流水线上补齐配音、字幕和最终音视频合成能力，并新增一条可直接产出当前项目介绍视频的内置流程。默认成片规格为 16:9、60–90 秒、中文旁白，复用本机 team-agent `voice-service` 的 Qwen3-TTS 模型和 Serena 音色。

Flow Studio 不复制 TTS 模型、Python 运行时或 team-agent 的进程管理。它只通过 team-agent 已公开的 HTTP 协议生成 WAV，并把音频及成片保存到当前 Workspace 的素材库。

## 2. 已确认边界

- 默认 TTS 地址：`http://127.0.0.1:17863`。
- 默认模型由 team-agent 管理：`mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-6bit`。
- 默认音色 `Serena`，语速 `1.0`，单次文本遵守服务端 600 字符限制。
- 本机回环地址允许无 token；非回环地址必须配置 Bearer token。
- TTS 不可用或响应不是有效 WAV 时，配音节点失败，不把无声视频标记为完整成片。
- 图片和视频模型仍保留现有占位降级，因而在没有图片/视频 key 时也能验证配音、字幕和 ffmpeg 合成链路。
- 不新增第三方 Python 依赖；HTTP 使用现有 `httpx`，媒体处理使用现有 ffmpeg/ffprobe。
- 保留 `video-demo` 与 `merge_video` 的现有接口和结果。
- 不读取任意本机仓库文件。内置项目介绍流程接收可编辑的项目名称和公开简介，默认内容介绍当前 Flow Studio 项目。

## 3. 流程和数据流

```text
项目名称 + 公开简介
        |
        v
storyboard（6–8 镜，输出 narration）
        |--------------------------|
        v                          v
keyframe -> shot_video         voiceover
        |                          |
        | clips[]                  | tracks[] + subtitles[]
        |--------------------------|
                    v
              video_compose
                    |
                    v
        MP4 + WAV 旁白 + SRT 字幕
```

内置 `project-intro-video` 使用 8 个镜头和 16:9 比例。分镜 LLM 接到明确约束：旁白总时长目标 60–90 秒，每镜必须有适合中文口播的 `narration`。无 LLM 时，规则分镜把输入句子同时用作画面描述和旁白，不产生空配音。

## 4. 模块和接口

### 4.1 `speech.py`

新增独立的 team-agent voice-service HTTP 适配层：

```python
def synthesize_wav(config: dict, text: str, *, voice: str = "", speed: float = 1.0,
                   session_id: str = "", generation: int = 0) -> bytes

def wav_duration(data: bytes) -> float
```

`synthesize_wav` 调用 `POST {base_url}/v1/tts`：

```json
{
  "sessionId": "flow-<run_id>-<shot_index>",
  "generation": 0,
  "text": "旁白",
  "voice": "Serena",
  "speed": 1.0
}
```

远程地址发送 `Authorization: Bearer <token>`。响应必须是 RIFF/WAVE，最大 64 MiB。错误消息保留 HTTP 状态和服务端公开错误码，但不包含 token。

### 4.2 模型配置

`VideoModels.DEFAULT_MODELS` 新增 `speech`：

```json
{
  "enabled": true,
  "base_url": "http://127.0.0.1:17863",
  "token": "",
  "voice": "Serena",
  "speed": 1.0,
  "timeout": 120
}
```

继续通过 Workspace 的 `video_models.json`、`GET/PUT /api/video/models` 和页面“模型”弹窗管理。API 返回 token 的现有行为暂不扩展；运行日志和异常脱敏必须覆盖 token/Authorization。

### 4.3 `voiceover` 节点

参数：

- `shots_source`：默认 `{{storyboard.shots}}`。
- `voice`：留空时使用 speech 配置默认音色。
- `speed`：默认 `1.0`，范围 `0.5–2.0`。

节点按镜头顺序读取 `narration`。每个非空旁白生成一条 WAV 素材；旁白为空视为结构错误。输出：

```json
{
  "tracks": [{
    "index": 1,
    "asset_id": "...",
    "path": "...wav",
    "url": "/media/files/...wav",
    "duration": 7.4,
    "text": "旁白"
  }],
  "subtitles": [{"index": 1, "text": "旁白", "duration": 7.4}],
  "count": 8,
  "total_duration": 68.2,
  "voice": "Serena",
  "speed": 1.0,
  "mode": "team-agent"
}
```

每次请求产生节点日志，不记录全文，仅记录镜头编号、字符数、音色和耗时。

### 4.4 `video_compose` 节点

参数：

- `clips_source`：默认 `{{shot_video.clips}}`。
- `voiceovers_source`：默认 `{{voiceover.tracks}}`。
- `subtitles_source`：默认 `{{voiceover.subtitles}}`。
- `title`：输出名称。
- `burn_subtitles`：默认 `true`。

镜头、配音和字幕必须数量相等且 index 顺序一致。每镜目标时长取 `max(镜头时长, 配音时长 + 0.35 秒)`：视频不足时冻结末帧，音频不足时补静音。随后按顺序拼接已对齐镜头，合成连续旁白音轨，生成 SRT；默认使用 ffmpeg/libass 烧录中文字幕，并保留独立 WAV 与 SRT 素材。

输出包含 `asset_id/path/url/duration/count/aspect_ratio`，以及 `audio_asset_id/audio_url`、`subtitle_asset_id/subtitle_url`、`voice`。如果 ffmpeg、视频、音频或字幕结构不满足要求，节点失败并保留完整异常堆栈。

## 5. 画布与内置流程

节点面板“视频制作”新增：

- `配音`：从分镜旁白生成逐镜 WAV。
- `成片`：将镜头、配音和字幕合成最终 MP4。

模型弹窗新增“文本转语音（team-agent voice-service）”，可配置启用状态、地址、token、音色、语速和超时。

新增内置流程 `project-intro-video`：

```text
start(project_name, project_brief)
 -> storyboard(8 镜, 16:9, 科技产品演示风)
 -> keyframe
 -> shot_video
 -> voiceover
 -> video_compose
 -> end
```

默认简介覆盖 Flow Studio 的可视化 Agent 编排、意图与条件路由、治理版本、实时节点日志、项目介绍视频能力，以及本机 team-agent TTS。该流程没有角色节点，因为产品介绍视频不需要人物一致性资产。

## 6. 安全和故障处理

- `base_url` 只允许 `http`/`https`，拒绝 URL 用户信息和 fragment。
- 非回环地址没有 token 时拒绝调用。
- token 仅进入 Authorization 请求头；运行节点输出、日志和素材 meta 不保存 token。
- 限制单段旁白 600 字、总镜头 20、单 WAV 64 MiB。
- HTTP 401/429/503、超时、无效 WAV、结构错位和 ffmpeg 错误均明确失败。
- 生成临时文件使用素材目录内的 run_id 前缀，并在成功或失败后清理。

## 7. 验收

- 单元测试：TTS 请求体、Bearer token、非回环鉴权、600 字限制、无效 WAV、WAV 时长。
- 节点测试：逐镜配音顺序、音频素材入库、空旁白失败、token 不进入运行结果。
- ffmpeg 测试：视频延长、静音补齐、SRT 时间轴、字幕烧录和最终 MP4 音轨。
- API 测试：模型配置包含 speech，节点类型包含 voiceover/video_compose，内置流程结构正确。
- 集成测试：对真实 `127.0.0.1:17863` 健康检查后生成一条短中文 WAV；测试失败只说明本机运行边界，不用 mock 冒充真实 TTS。
- 浏览器验收：桌面及 390×844 视口能配置 TTS、添加两类节点、运行项目介绍流程，并从运行输出预览音频和最终视频。

真实项目介绍成片的画面质量取决于当前文生图和图生视频配置；即使这些模型未配置，验收仍应产出带真实 Serena 配音和字幕的占位画面 MP4。
