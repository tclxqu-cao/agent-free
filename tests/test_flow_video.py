"""Flow Studio 视频流水线测试：占位生成 / 分镜 / 角色 / 关键帧 / 镜头视频 / 合成 / 素材库 / 模型配置。

约定：机器有 ffmpeg → 占位视频与合成走真实 MP4；无 ffmpeg → GIF 兜底 + 合成降级
（相关断言用 has_ffmpeg 分支或 skip）。整机冒烟在 macOS + ffmpeg 8 完成。
"""

import json
import struct
import zlib

import pytest

from flow_studio.assets import AssetStore
from flow_studio.engine import FlowRunner
from flow_studio.graph import graph_from_dict
from flow_studio.video import (DEFAULT_MODELS, ASPECTS, VideoModels, concat_videos,
                               gen_image, gen_video, has_ffmpeg,
                               placeholder_png_bytes, placeholder_video_bytes, probe_duration)
from flow_studio.video_nodes import _fallback_shots, _resolve_list

pytest.importorskip("fastapi", reason="服务测试需要 fastapi（uv sync --extra studio）")


# ---------------------------------------------------------------- 占位素材
def test_placeholder_png_shape_and_magic():
    data = placeholder_png_bytes("S01", "9:16", "seed-x")
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", data[16:24])
    assert (w, h) == ASPECTS["9:16"]
    # IDAT 可解压（数据完整性）
    ihdr_len = struct.unpack(">I", data[8:12])[0]
    idat = data[8 + ihdr_len + 12:]
    idat_len = struct.unpack(">I", idat[:4])[0]
    raw = zlib.decompress(idat[8:8 + idat_len])
    expect = h * (1 + w * 3)
    assert len(raw) == expect


def test_placeholder_video_mp4_with_ffprobe(tmp_path):
    if not has_ffmpeg():
        pytest.skip("需要 ffmpeg")
    data, ext = placeholder_video_bytes("S02", "16:9", 2.0, "seed-y")
    assert ext == "mp4"
    p = tmp_path / "clip.mp4"
    p.write_bytes(data)
    assert probe_duration(p) == pytest.approx(2.0, abs=0.5)


def test_gen_video_hf_gradio_failure_falls_back_to_placeholder(monkeypatch):
    """hf_gradio provider：Space 调用失败时回退占位，流水线不断。"""
    import flow_studio.video as vmod

    def boom(*a, **kw):
        raise RuntimeError("space unreachable")

    monkeypatch.setattr(vmod, "_gen_video_hf_space", boom)
    cfg = {"enabled": True, "provider": "hf_gradio",
           "hf_space": "x/y", "hf_api": "/generate_video", "timeout": 60}
    res = gen_video(cfg, "motion", b"\x89PNG fake", 2.0, "16:9", label="S01", seed="s")
    assert res["mode"] == "placeholder" and "失败" in res["error"]


def test_gen_video_hf_gradio_requires_first_frame():
    cfg = {"enabled": True, "provider": "hf_gradio", "hf_space": "x/y"}
    res = gen_video(cfg, "motion", None, 2.0, "16:9", label="S01", seed="s")
    assert res["mode"] == "placeholder" and "首帧" in res["error"]


def test_video_models_video_defaults_have_provider(tmp_path):
    from flow_studio.video import DEFAULT_MODELS as DM

    vm = VideoModels(tmp_path / "v.json")
    cfg = vm.load()["video"]
    assert cfg["provider"] == "http" and cfg["hf_space"]


def test_placeholder_video_gif_fallback_without_ffmpeg(tmp_path, monkeypatch):
    import flow_studio.video as vmod

    monkeypatch.setattr(vmod, "has_ffmpeg", lambda: False)
    data, ext = vmod.placeholder_video_bytes("S03", "1:1", 1.0, "seed-z")
    assert ext == "gif" and data[:6] == b"GIF89a"
    p = tmp_path / "clip.gif"
    p.write_bytes(data)
    if has_ffmpeg():  # ffmpeg 在时顺手验证 GIF 可被解码
        import subprocess

        r = subprocess.run(
            ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
             "-show_entries", "stream=nb_read_frames", "-of", "json", str(p)],
            capture_output=True, text=True)
        frames = json.loads(r.stdout)["streams"][0]["nb_read_frames"]
        assert int(frames) >= 12  # 1 秒 × 12fps


def test_gen_image_degrades_to_placeholder_without_config():
    res = gen_image({}, "a cat", "16:9", label="K-正面", seed="s")
    assert res["mode"] == "placeholder" and res["ext"] == "png"
    assert res["data"][:8] == b"\x89PNG\r\n\x1a\n"
    assert "未配置" in res["error"]


def test_gen_video_degrades_without_config():
    res = gen_video({}, "motion", None, 2.0, "9:16", label="S01", seed="s")
    assert res["mode"] == "placeholder"
    assert res["aspect_ratio"] == "9:16"
    assert res["data"][:4]  # 有内容


def test_gen_image_model_error_falls_back(monkeypatch):
    import httpx

    def boom(*a, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", boom)
    cfg = {"enabled": True, "base_url": "http://127.0.0.1:9", "api_key": "k", "model": "m"}
    res = gen_image(cfg, "p", "16:9", label="K", seed="s")
    assert res["mode"] == "placeholder" and "失败" in res["error"]


# ---------------------------------------------------------------- 模型配置
def test_video_models_roundtrip_and_llm_inheritance(tmp_path):
    vm = VideoModels(tmp_path / "video_models.json")
    cfg = vm.load()
    assert set(cfg) == set(DEFAULT_MODELS)
    for key, defaults in DEFAULT_MODELS.items():
        assert defaults.keys() <= cfg[key].keys()
    saved = vm.save({"image": {"enabled": True, "base_url": "http://x", "api_key": "k",
                               "model": "img-1"}})
    assert saved["image"]["enabled"] is True
    assert saved["image"]["model"] == "img-1"
    assert saved["video"]["submit_path"] == DEFAULT_MODELS["video"]["submit_path"]  # 其余保留默认
    assert json.loads((tmp_path / "video_models.json").read_text())["image"]["model"] == "img-1"

    # use_config_llm：继承 config.llm；独立配置优先
    vm2 = VideoModels(tmp_path / "v2.json")
    assert vm2.effective_llm({"enabled": True, "api_key": "ck"})["api_key"] == "ck"
    vm2.save({"llm": {"use_config_llm": False, "enabled": True, "api_key": "own"}})
    assert vm2.effective_llm({"enabled": True, "api_key": "ck"})["api_key"] == "own"


# ---------------------------------------------------------------- 素材库
def test_asset_store_crud_and_traversal_guard(tmp_path):
    store = AssetStore(tmp_path / "media")
    rec = store.add_bytes(b"\x89PNG\r\n\x1a\nfake", "png", kind="image",
                          name="测试图", flow_id="f1", meta={"ratio": "16:9"})
    assert rec["url"] == f"/media/files/{rec['path']}"
    assert (tmp_path / "media" / "files" / rec["path"]).exists()
    assert store.get(rec["id"])["meta"]["ratio"] == "16:9"
    assert len(store.list(kind="image")) == 1
    assert store.list(kind="video") == []

    # 路径穿越防护
    assert store.file_path("../../etc/passwd") is None
    assert store.file_path("a/b") is None
    assert store.file_path(rec["id"]) is not None

    assert store.delete(rec["id"]) is True
    assert store.get(rec["id"]) is None
    assert not (tmp_path / "media" / "files" / rec["path"]).exists()
    assert store.delete(rec["id"]) is False


# ---------------------------------------------------------------- 分镜节点
def _runner(tmp_path):
    media = AssetStore(tmp_path / "media")
    vm = VideoModels(tmp_path / "video_models.json")
    return FlowRunner(media=media, vmodels=vm), media, vm


def _video_flow(nodes, edges):
    return graph_from_dict({"id": "v", "name": "视频测试", "nodes": nodes, "edges": edges})


def _n(id, type, **params):
    return {"id": id, "type": type, "params": params}


def test_storyboard_fallback_split(tmp_path):
    runner, _, _ = _runner(tmp_path)
    g = _video_flow([
        _n("start", "start", inputs=[{"key": "story", "default":
            "第一句剧情。第二句剧情！第三句剧情？第四句剧情\n第五句剧情"}]),
        _n("sb", "storyboard", story="{{input.story}}", shot_count=3,
           aspect_ratio="9:16", style="水墨风"),
        _n("end", "end", output="{{sb.count}} 镜 {{sb.aspect_ratio}} {{sb.via}}"),
    ], [{"from": "start", "to": "sb"}, {"from": "sb", "to": "end"}])
    run = runner.run(g, {})
    assert run.status == "success"
    assert run.output == "3 镜 9:16 fallback"
    shots = run.node_run("sb").output["shots"]
    assert [s["index"] for s in shots] == [1, 2, 3]
    assert all(s["aspect_ratio"] == "9:16" for s in shots)
    assert any("水墨风" in s["image_prompt"] for s in shots)
    assert all(s["desc"] for s in shots)


def test_storyboard_llm_mock(monkeypatch, tmp_path):
    import flow_studio.llm as llm_mod

    fake = [{"index": i + 1, "title": f"镜{i + 1}", "duration": 2,
             "shot_size": "特写", "camera": "推", "desc": f"画面{i}",
             "image_prompt": f"scene {i}", "narration": ""} for i in range(4)]
    monkeypatch.setattr(llm_mod, "llm_json", lambda cfg, s, u: fake)
    monkeypatch.setattr(llm_mod, "llm_chat", lambda *a, **kw: "[]")
    runner, _, vm = _runner(tmp_path)
    vm.save({"llm": {"enabled": True, "api_key": "k", "base_url": "http://x"}})
    g = _video_flow([
        _n("start", "start"),
        _n("sb", "storyboard", story="一个故事。两个故事。", shot_count=4,
           aspect_ratio="16:9", style=""),
        _n("end", "end", output="{{sb.via}}:{{sb.count}}"),
    ], [{"from": "start", "to": "sb"}, {"from": "sb", "to": "end"}])
    run = runner.run(g, {})
    assert run.status == "success"
    assert run.output == "llm:4"
    assert run.node_run("sb").output["shots"][0]["duration"] == 2.0


def test_fallback_shots_short_story():
    shots = _fallback_shots("只有一句", 3, "风格", "16:9")
    assert len(shots) == 3 and all(s["desc"] for s in shots)


def test_storyboard_empty_story_fails(tmp_path):
    runner, _, _ = _runner(tmp_path)
    g = _video_flow([_n("start", "start"),
                     _n("sb", "storyboard", story="", shot_count=2),
                     _n("end", "end")],
                    [{"from": "start", "to": "sb"}, {"from": "sb", "to": "end"}])
    run = runner.run(g, {})
    assert run.status == "failed" and "剧情提示词为空" in run.error


# ---------------------------------------------------------------- 角色 / 关键帧 / 镜头视频 / 合成
def test_character_generates_views_into_library(tmp_path):
    runner, media, _ = _runner(tmp_path)
    g = _video_flow([
        _n("start", "start"),
        _n("ch", "character", name="少年", description="蓝衣短发", views=["正面", "背面"],
           aspect_ratio="1:1", style="像素风"),
        _n("end", "end", output="{{ch.count}}:{{ch.mode}}:{{ch.name}}"),
    ], [{"from": "start", "to": "ch"}, {"from": "ch", "to": "end"}])
    run = runner.run(g, {})
    assert run.status == "success" and run.output == "2:placeholder:少年"
    views = run.node_run("ch").output["views"]
    assert [v["view"] for v in views] == ["正面", "背面"]
    for v in views:
        rec = media.get(v["asset_id"])
        assert rec["kind"] == "image" and rec["meta"]["view"] in ("正面", "背面")
        assert (media.files / rec["path"]).exists()


def _pipeline_graph(duration=1, enforce=True, optional=False):
    return _video_flow([
        _n("start", "start", inputs=[{"key": "story",
                                      "default": "日出。赶路。遇雨。宿营。"}]),
        _n("sb", "storyboard", story="{{input.story}}", shot_count=3,
           aspect_ratio="16:9", style="电影感"),
        _n("ch", "character", name="旅人", description="斗笠行者", views=["正面"],
           aspect_ratio="1:1", style=""),
        _n("kf", "keyframe", shots_source="{{sb.shots}}",
           characters_source="{{ch.description}}"),
        _n("sv", "shot_video", frames_source="{{kf.frames}}",
           shots_source="{{sb.shots}}", duration=duration, optional=optional),
        _n("mg", "merge_video", clips_source="{{sv.clips}}", title="成片",
           enforce_ratio=enforce, optional=optional),
        _n("end", "end", output="{{mg.count}}段{{mg.duration}}s {{mg.aspect_ratio}}"),
    ], [
        {"from": "start", "to": "sb"}, {"from": "sb", "to": "ch"},
        {"from": "ch", "to": "kf"}, {"from": "kf", "to": "sv"},
        {"from": "sv", "to": "mg"}, {"from": "mg", "to": "end"},
    ])


@pytest.mark.skipif(not has_ffmpeg(), reason="需要 ffmpeg")
def test_full_pipeline_placeholder_e2e(tmp_path):
    runner, media, _ = _runner(tmp_path)
    g = _pipeline_graph(duration=1)
    run = runner.run(g, {})
    assert run.status == "success", run.error
    sb, kf, sv, mg = (run.node_run(x).output for x in ("sb", "kf", "sv", "mg"))
    assert sb["count"] == 3 and sb["aspect_ratio"] == "16:9"
    assert kf["count"] == 3
    assert sv["count"] == 3 and sv["mode"] == "placeholder"
    # 比例级联：分镜 16:9 → 关键帧/镜头视频未显式设置时继承
    assert {c["aspect_ratio"] for c in sv["clips"]} == {"16:9"}
    assert mg["count"] == 3 and mg["aspect_ratio"] == "16:9"
    assert mg["duration"] == pytest.approx(sum(c["duration"] for c in sv["clips"]), abs=0.8)
    merged = media.get(mg["asset_id"])
    assert merged["kind"] == "video" and merged["meta"]["clip_count"] == 3
    assert (media.files / merged["path"]).stat().st_size > 1000
    # 素材库：3 关键帧 + 3 片段 + 1 成片 + 1 角色图
    kinds = [a["kind"] for a in media.list()]
    assert kinds.count("image") == 4 and kinds.count("video") == 4
    assert run.output.startswith("3段")


def test_keyframe_ratio_inherits_storyboard(tmp_path):
    runner, _, _ = _runner(tmp_path)
    g = _video_flow([
        _n("start", "start"),
        _n("sb", "storyboard", story="一。二。三。", shot_count=2, aspect_ratio="9:16"),
        _n("kf", "keyframe", shots_source="{{sb.shots}}"),
        _n("end", "end", output="{{kf.frames.0.aspect_ratio}}"),
    ], [{"from": "start", "to": "sb"}, {"from": "sb", "to": "kf"}, {"from": "kf", "to": "end"}])
    run = runner.run(g, {})
    assert run.status == "success"
    assert run.node_run("kf").output["frames"][0]["aspect_ratio"] == "9:16"


def test_merge_ratio_mismatch(tmp_path):
    if not has_ffmpeg():
        pytest.skip("需要 ffmpeg")
    runner, media, _ = _runner(tmp_path)
    a = media.add_bytes(placeholder_video_bytes("A", "16:9", 1.0, "a")[0], "mp4",
                        kind="video", meta={"aspect_ratio": "16:9"})
    b = media.add_bytes(placeholder_video_bytes("B", "9:16", 1.0, "b")[0], "mp4",
                        kind="video", meta={"aspect_ratio": "9:16"})
    from flow_studio.video import concat_videos

    clips = [
        {"path": str(media.files / a["path"]), "aspect_ratio": "16:9"},
        {"path": str(media.files / b["path"]), "aspect_ratio": "9:16"},
    ]
    with pytest.raises(ValueError, match="比例不一致"):
        concat_videos(clips, tmp_path / "out.mp4", enforce_ratio=True)
    merged = concat_videos(clips, tmp_path / "out2.mp4", enforce_ratio=False)
    assert merged["count"] == 2 and merged["duration"] > 0


def test_merge_without_ffmpeg_skips_when_optional(tmp_path, monkeypatch):
    """无 ffmpeg：镜头视频走 GIF 占位，合成 optional=true 时降级跳过、流程继续。"""
    import flow_studio.video as vmod

    monkeypatch.setattr(vmod, "has_ffmpeg", lambda: False)
    runner, media, _ = _runner(tmp_path)
    g = _video_flow([
        _n("start", "start", inputs=[{"key": "story", "default": "一。二。三。"}]),
        _n("sb", "storyboard", story="{{input.story}}", shot_count=2, aspect_ratio="16:9"),
        _n("kf", "keyframe", shots_source="{{sb.shots}}"),
        _n("sv", "shot_video", frames_source="{{kf.frames}}", duration=1),
        _n("mg", "merge_video", clips_source="{{sv.clips}}", optional=True),
        _n("t", "template", template="降级继续"),
        _n("end", "end", output="{{t.text}}"),
    ], [{"from": "start", "to": "sb"}, {"from": "sb", "to": "kf"},
        {"from": "kf", "to": "sv"}, {"from": "sv", "to": "mg"},
        {"from": "mg", "to": "t"}, {"from": "t", "to": "end"}])
    run = runner.run(g, {})
    assert run.status == "success"
    assert run.output == "降级继续"
    sv = run.node_run("sv").output
    assert sv["count"] == 2 and sv["mode"] == "placeholder"
    assert {c["path"] for c in sv["clips"]}  # GIF 片段已入库
    assert run.node_run("mg").status == "skipped"


def test_resolve_list_shapes(tmp_path):
    from flow_studio.template import build_namespace

    ns = build_namespace({}, {}, {"sb": {"shots": [{"index": 1}]},
                                  "j": {"arr": [1, 2]}})
    assert _resolve_list("{{sb.shots}}", ns) == [{"index": 1}]
    assert _resolve_list('[{"index": 2}]', ns) == [{"index": 2}]
    assert _resolve_list("一行\n二行", ns) == ["一行", "二行"]
    assert _resolve_list("", ns) == []
    assert _resolve_list("{{nope.x}}", ns) == []


def test_asset_node_reference(tmp_path):
    runner, media, _ = _runner(tmp_path)
    rec = media.add_bytes(b"gif89axxxx", "gif", kind="image", name="已上传")
    g = _video_flow([
        _n("start", "start"),
        _n("a", "asset", asset_id=rec["id"]),
        _n("end", "end", output="{{a.kind}}:{{a.name}}"),
    ], [{"from": "start", "to": "a"}, {"from": "a", "to": "end"}])
    run = runner.run(g, {})
    assert run.output == "image:已上传"
    g_missing = _video_flow([
        _n("start", "start"),
        _n("a", "asset", asset_id="deadbeef"),
        _n("end", "end"),
    ], [{"from": "start", "to": "a"}, {"from": "a", "to": "end"}])
    assert runner.run(g_missing, {}).status == "failed"


def test_video_node_without_media_store_fails(tmp_path):
    runner = FlowRunner()
    g = _video_flow([_n("start", "start"), _n("sb", "storyboard", story="x"),
                     _n("end", "end")],
                    [{"from": "start", "to": "sb"}, {"from": "sb", "to": "end"}])
    run = runner.run(g, {})
    assert run.status == "failed" and "素材库未初始化" in run.error


# ---------------------------------------------------------------- 服务 API
@pytest.fixture
def client(config_dir, tmp_path):
    from fastapi.testclient import TestClient

    from flow_studio.server import create_app

    return TestClient(create_app(config_dir, tmp_path / "data"))


def test_api_video_models(client):
    cfg = client.get("/api/video/models").json()
    assert "llm" in cfg and "image" in cfg and "video" in cfg
    r = client.put("/api/video/models", json={
        "image": {"enabled": True, "base_url": "http://img", "api_key": "k",
                  "model": "m1"}})
    assert r.status_code == 200 and r.json()["image"]["model"] == "m1"
    assert client.get("/api/video/models").json()["image"]["enabled"] is True


def test_api_assets_upload_list_delete(client):
    png = placeholder_png_bytes("S01", "1:1", "up")
    r = client.post("/api/assets/upload?name=up.png&kind=image&ext=png", content=png)
    assert r.status_code == 200
    rec = r.json()
    assert rec["kind"] == "image" and rec["bytes"] == len(png)

    listing = client.get("/api/assets?kind=image").json()
    assert any(a["id"] == rec["id"] for a in listing)

    served = client.get(rec["url"])
    assert served.status_code == 200 and served.content == png
    assert client.get("/media/files/..%2F..%2Fconfig.yaml").status_code in (404, 400)
    assert client.get("/media/files/nope.png").status_code == 404

    assert client.delete(f"/api/assets/{rec['id']}").status_code == 200
    assert client.get(rec["url"]).status_code == 404
    assert client.delete(f"/api/assets/{rec['id']}").status_code == 404


def test_api_node_types_include_video(client):
    types = client.get("/api/node-types").json()
    assert {"storyboard", "character", "keyframe", "shot_video",
            "merge_video", "asset"} <= set(types)
    assert types["storyboard"]["form"][0]["key"] == "story"


def test_api_run_video_demo_e2e(client, tmp_path):
    """端到端：内置 video-demo 通过 HTTP 运行，全降级产成片。"""
    flows = {f["id"] for f in client.get("/api/flows").json()}
    assert "video-demo" in flows
    if not has_ffmpeg():
        pytest.skip("合成需要 ffmpeg（占位降级链路已由单测覆盖）")
    r = client.post("/api/flows/video-demo/run", json={"inputs": {}})
    assert r.status_code == 200
    run = r.json()
    assert run["status"] == "success", run.get("error")
    by_node = {n["node_id"]: n for n in run["node_runs"]}
    assert by_node["storyboard"]["output"]["count"] == 4
    assert by_node["character"]["output"]["count"] == 3
    assert by_node["merge"]["output"]["count"] == 4
    url = by_node["merge"]["output"]["url"]
    got = client.get(url)
    assert got.status_code == 200 and len(got.content) > 1000
    assert "🎬" in run["output"] and url in run["output"]
