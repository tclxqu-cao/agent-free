"""视频流水线节点执行逻辑：分镜 / 角色设定 / 关键帧 / 镜头视频 / 合成 / 素材引用。

由 engine.FlowRunner 委托调用（_run_video_node）。生成类节点"永不中断"：
模型未配置或调用失败 → 占位素材（mode=placeholder），流水线继续；
结构性错误（上游为空、ffmpeg 缺失且 required）→ 抛错或 SkipNode 降级。
"""

from __future__ import annotations

import json
import re

from .assets import AssetStore
from .template import render, resolve
from .video import VideoModels, _norm_aspect, concat_videos, gen_image, gen_video

SHOT_SIZES = ["远景", "全景", "中景", "近景", "特写"]
CAMERAS = ["固定镜头", "缓慢推近", "横向移动", "摇镜", "跟随"]


class SkipNode(Exception):
    """视频节点降级跳过（engine 转 _SkipNode，流程继续）。"""


# ---------------------------------------------------------------- 分镜
def run_storyboard(node, ns, media, vmodels, llm_cfg) -> dict:
    story = render(node.params.get("story") or "", ns).strip()
    if not story:
        raise ValueError("剧情提示词为空（story）")
    count = max(1, min(int(node.params.get("shot_count") or 4), 20))
    ratio = _norm_aspect(node.params.get("aspect_ratio"))
    style = render(str(node.params.get("style") or ""), ns).strip()

    shots = None
    via = "fallback"
    llm_effective = (vmodels.effective_llm(llm_cfg) if vmodels else {}) or llm_cfg or {}
    if llm_effective.get("enabled") and llm_effective.get("api_key"):
        from .llm import llm_json

        system = ("你是资深影视分镜师。把剧情拆成分镜表，只输出 JSON 数组，"
                  "每个元素字段：index(从1整数)、title、duration(秒数)、"
                  "shot_size(景别：远景/全景/中景/近景/特写)、camera(运镜)、"
                  "desc(画面描述)、image_prompt(首帧绘图提示词,英文更佳)、narration(旁白台词)。")
        user = (f"剧情：{story}\n风格：{style or '电影感'}\n"
                f"拆成 {count} 个镜头，比例 {ratio}。只输出 JSON 数组。")
        parsed = llm_json(llm_effective, system, user)
        if isinstance(parsed, list) and parsed:
            shots = [_norm_shot(s, i, style, ratio) for i, s in enumerate(parsed[:count])]
            via = "llm"
    if shots is None:
        shots = _fallback_shots(story, count, style, ratio)
    return {"shots": shots, "count": len(shots), "aspect_ratio": ratio, "via": via,
            "text": f"分镜 {len(shots)} 个（{ratio}，{via}）："
                    + "；".join(f"{s['index']}.{s['title']}" for s in shots)}


def _norm_shot(raw, i: int, style: str, ratio: str) -> dict:
    if isinstance(raw, str):
        raw = {"desc": raw}
    shot = raw if isinstance(raw, dict) else {}
    desc = str(shot.get("desc") or shot.get("description") or "").strip()
    img = str(shot.get("image_prompt") or desc).strip()
    if style:
        img = f"{img}, {style}" if img else style
    return {
        "index": int(shot.get("index") or i + 1),
        "title": str(shot.get("title") or f"镜头{i + 1}")[:40],
        "duration": max(1.0, min(_as_float(shot.get("duration"), 3.0), 30.0)),
        "shot_size": str(shot.get("shot_size") or SHOT_SIZES[i % len(SHOT_SIZES)]),
        "camera": str(shot.get("camera") or CAMERAS[i % len(CAMERAS)]),
        "desc": desc,
        "image_prompt": img,
        "narration": str(shot.get("narration") or ""),
        "aspect_ratio": ratio,
    }


def _as_float(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _fallback_shots(story: str, count: int, style: str, ratio: str) -> list[dict]:
    """无 LLM 降级：按句切分 → 均匀分成 count 组，套用固定景别/运镜循环。"""
    sents = [s.strip() for s in re.split(r"[。！？!?\n；;]+", story) if s.strip()]
    if not sents:
        sents = [story.strip()]
    groups: list[str] = [""] * count
    for i, s in enumerate(sents):
        gi = i * count // max(1, len(sents))
        groups[gi] = f"{groups[gi]}，{s}" if groups[gi] else s
    for i, g in enumerate(groups):  # 空组从相邻借内容
        if not g:
            groups[i] = sents[i % len(sents)]
    return [_norm_shot({"desc": g, "title": f"镜头{i + 1}",
                        "duration": 3.0,
                        "shot_size": SHOT_SIZES[i % len(SHOT_SIZES)],
                        "camera": CAMERAS[i % len(CAMERAS)]},
                       i, style, ratio) for i, g in enumerate(groups)]


# ---------------------------------------------------------------- 角色设定图
def run_character(node, ns, media: AssetStore, vmodels, llm_cfg) -> dict:
    name = render(str(node.params.get("name") or ""), ns).strip() or "主角"
    desc = render(str(node.params.get("description") or ""), ns).strip()
    if not desc:
        raise ValueError(f"角色「{name}」缺少外貌描述（description）")
    views = [v for v in _str_list(node.params.get("views"), ["正面", "左侧", "右侧", "背面"]) if v]
    ratio = _norm_aspect(node.params.get("aspect_ratio"))
    style = render(str(node.params.get("style") or ""), ns).strip()
    flow_id = str((ns.get("vars") or {}).get("flow_id") or "")

    out_views = []
    modes = []
    for view in views:
        prompt = (f"character design sheet, {name}, {view} view, full body, "
                  f"{desc}, {style}, consistent character design, plain background")
        res = gen_image((vmodels.load().get("image") if vmodels else {}) or {},
                        prompt, ratio, label=f"K-{view}", seed=f"{name}-{view}")
        modes.append(res["mode"])
        asset = media.add_bytes(
            res["data"], res["ext"], kind="image", name=f"{name}-{view}",
            flow_id=flow_id,
            meta={"role": name, "view": view, "aspect_ratio": ratio,
                  "mode": res["mode"], "prompt": prompt,
                  "error": res.get("error")})
        out_views.append({"view": view, "asset_id": asset["id"],
                          "path": asset["path"], "url": asset["url"]})
    mode = "model" if "model" in modes else "placeholder"
    return {"views": out_views, "count": len(out_views), "mode": mode,
            "name": name, "description": desc,
            "text": f"角色「{name}」设定图 {len(out_views)} 张（{ '、'.join(views) }，{mode}）"}


# ---------------------------------------------------------------- 关键帧
def run_keyframe(node, ns, media: AssetStore, vmodels, llm_cfg) -> dict:
    source = node.params.get("shots_source") or "{{storyboard.shots}}"
    shots = _resolve_list(source, ns)
    if not shots:
        raise ValueError("关键帧节点上游镜头为空（shots_source），请连线分镜节点")
    chars = node.params.get("characters_source")
    char_desc = render(str(chars or ""), ns).strip()
    style = render(str(node.params.get("style") or ""), ns).strip()
    # 比例级联：节点显式设置 > 分镜层（项目级）> 默认 16:9
    ratio = _norm_aspect(node.params.get("aspect_ratio") or
                         (shots[0].get("aspect_ratio") if isinstance(shots[0], dict) else None))
    flow_id = str((ns.get("vars") or {}).get("flow_id") or "")

    frames = []
    modes = []
    for i, shot in enumerate(shots):
        s = shot if isinstance(shot, dict) else {"desc": str(shot)}
        prompt = str(s.get("image_prompt") or s.get("desc") or "")
        prompt = "，".join(p for p in (prompt, style, char_desc) if p)
        res = gen_image((vmodels.load().get("image") if vmodels else {}) or {},
                        prompt or f"cinematic still, scene {i + 1}", ratio,
                        label=f"S{i + 1:02d}", seed=f"frame-{i}-{prompt[:24]}")
        modes.append(res["mode"])
        asset = media.add_bytes(
            res["data"], res["ext"], kind="image",
            name=f"关键帧-{s.get('title') or i + 1}", flow_id=flow_id,
            meta={"shot_index": int(_as_float(s.get("index"), i + 1)),
                  "aspect_ratio": ratio, "mode": res["mode"],
                  "prompt": prompt, "error": res.get("error")})
        frames.append({"index": int(_as_float(s.get("index"), i + 1)),
                       "asset_id": asset["id"], "path": asset["path"],
                       "url": asset["url"], "title": str(s.get("title") or ""),
                       "aspect_ratio": ratio})
    mode = "model" if "model" in modes else "placeholder"
    return {"frames": frames, "count": len(frames), "mode": mode,
            "text": f"关键帧 {len(frames)} 张（{ratio}，{mode}）"}


# ---------------------------------------------------------------- 镜头视频
def run_shot_video(node, ns, media: AssetStore, vmodels, llm_cfg) -> dict:
    frames = _resolve_list(node.params.get("frames_source")
                           or "{{keyframe.frames}}", ns)
    if not frames:
        raise ValueError("镜头视频节点上游关键帧为空（frames_source），请连线关键帧节点")
    shots = _resolve_list(node.params.get("shots_source")
                          or "{{storyboard.shots}}", ns) or []
    duration = max(1.0, min(_as_float(node.params.get("duration"), 3.0), 30.0))
    # 比例级联：节点显式设置 > 分镜层 > 关键帧携带 > 默认 16:9
    shots_ratio = shots[0].get("aspect_ratio") if shots and isinstance(shots[0], dict) else None
    frames_ratio = frames[0].get("aspect_ratio") if isinstance(frames[0], dict) else None
    ratio = _norm_aspect(node.params.get("aspect_ratio") or shots_ratio or frames_ratio)
    flow_id = str((ns.get("vars") or {}).get("flow_id") or "")
    vcfg = (vmodels.load().get("video") if vmodels else {}) or {}

    clips = []
    modes = []
    for i, frame in enumerate(frames):
        shot = shots[i] if i < len(shots) and isinstance(shots[i], dict) else {}
        motion = "，".join(p for p in (
            str(shot.get("desc") or ""), f"运镜：{shot.get('camera') or '固定镜头'}",
            f"时长约{duration}秒", "画面连贯流畅，电影感") if p)
        first_frame = None
        aid = frame.get("asset_id") if isinstance(frame, dict) else None
        if aid:
            fp = media.file_path(str(aid))
            first_frame = fp.read_bytes() if fp else None
        res = gen_video(vcfg, motion, first_frame, duration, ratio,
                        label=f"S{i + 1:02d}", seed=f"clip-{i}")
        modes.append(res["mode"])
        real_duration = duration
        if res["ext"] == "mp4":
            from .video import probe_duration

            tmp = media.files / f"_probe_{i}.mp4"
            tmp.write_bytes(res["data"])
            real_duration = probe_duration(tmp) or duration
            tmp.unlink(missing_ok=True)
        elif res["ext"] == "gif":
            real_duration = duration
        asset = media.add_bytes(
            res["data"], res["ext"], kind="video",
            name=f"镜头{i + 1:02d}", flow_id=flow_id,
            meta={"shot_index": i + 1, "aspect_ratio": res.get("aspect_ratio") or ratio,
                  "duration": round(real_duration, 2), "mode": res["mode"],
                  "prompt": motion, "error": res.get("error")})
        clips.append({"index": i + 1, "asset_id": asset["id"], "path": asset["path"],
                      "url": asset["url"], "duration": round(real_duration, 2),
                      "aspect_ratio": res.get("aspect_ratio") or ratio})
    mode = "model" if "model" in modes else "placeholder"
    total = round(sum(c["duration"] for c in clips), 2)
    return {"clips": clips, "count": len(clips), "total_duration": total,
            "mode": mode, "text": f"镜头视频 {len(clips)} 段，合计 {total} 秒（{mode}）"}


# ---------------------------------------------------------------- 合成
def run_merge_video(node, ns, media: AssetStore, vmodels, llm_cfg) -> dict:
    clips = _resolve_list(node.params.get("clips_source")
                          or "{{shot_video.clips}}", ns)
    if not clips:
        raise ValueError("合成节点上游片段为空（clips_source），请连线镜头视频节点")
    paths = []
    for c in clips:
        if isinstance(c, dict) and c.get("asset_id"):
            fp = media.file_path(str(c["asset_id"]))
        elif isinstance(c, dict) and c.get("path"):
            fp = media.file_path(str(c["path"]))
        else:
            fp = None
        if fp is None:
            raise ValueError(f"片段文件缺失：{c}")
        paths.append({"path": str(fp), "aspect_ratio": c.get("aspect_ratio"),
                      "duration": c.get("duration")})
    title = render(str(node.params.get("title") or ""), ns).strip() or "成片"
    enforce = node.params.get("enforce_ratio", True)
    out_tmp = media.files / f"_merge_tmp_{(ns.get('vars') or {}).get('run_id', 'x')}.mp4"
    try:
        merged = concat_videos(paths, out_tmp, enforce_ratio=bool(enforce))
    except RuntimeError as e:
        if node.params.get("optional"):
            raise SkipNode(str(e)) from e
        raise
    flow_id = str((ns.get("vars") or {}).get("flow_id") or "")
    data = out_tmp.read_bytes()
    out_tmp.unlink(missing_ok=True)
    asset = media.add_bytes(data, "mp4", kind="video", name=title, flow_id=flow_id,
                            meta={"aspect_ratio": merged["aspect_ratio"],
                                  "duration": merged["duration"],
                                  "clip_count": merged["count"], "mode": "merge"})
    return {"asset_id": asset["id"], "path": asset["path"], "url": asset["url"],
            "duration": merged["duration"], "count": merged["count"],
            "aspect_ratio": merged["aspect_ratio"],
            "text": f"成片「{title}」：{merged['count']} 段合成，"
                    f"{merged['duration']} 秒，{merged['aspect_ratio']}"
                    f"（预览/下载：{asset['url']}）"}


# ---------------------------------------------------------------- 素材引用
def run_asset(node, ns, media: AssetStore, vmodels, llm_cfg) -> dict:
    aid = render(str(node.params.get("asset_id") or ""), ns).strip()
    if not aid:
        raise ValueError("素材节点未选择素材（asset_id）")
    rec = media.get(aid)
    if rec is None:
        raise ValueError(f"素材不存在：{aid}")
    return {"asset_id": rec["id"], "kind": rec["kind"], "path": rec["path"],
            "url": rec["url"], "name": rec["name"], "meta": rec["meta"],
            "text": f"素材 {rec['name']}（{rec['kind']}，{rec['path']}）"}


# ---------------------------------------------------------------- 工具
def _str_list(v, default: list[str]) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [s.strip() for s in re.split(r"[,，\n]+", v) if s.strip()]
    return list(default)


def _resolve_list(source, ns) -> list:
    """模板解析为 list：占位符直取原始值；JSON 数组文本反序列化；逐行文本切分。"""
    if isinstance(source, list):
        return source
    if not isinstance(source, str) or not source.strip():
        return []
    val = resolve(source, ns)
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        text = val.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                return parsed if isinstance(parsed, list) else []
            except ValueError:
                return []
        return [s.strip() for s in text.splitlines() if s.strip()] if text else []
    return []


def execute_video_node(node, ns, media, vmodels, llm_cfg) -> dict:
    handler = {
        "storyboard": run_storyboard, "character": run_character,
        "keyframe": run_keyframe, "shot_video": run_shot_video,
        "merge_video": run_merge_video, "asset": run_asset,
    }.get(node.type)
    if handler is None:
        raise ValueError(f"未知视频节点类型：{node.type}")
    return handler(node, ns, media, vmodels, llm_cfg)
