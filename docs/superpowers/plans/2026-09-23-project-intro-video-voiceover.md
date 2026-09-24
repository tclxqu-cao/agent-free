# Project Intro Video Voiceover Implementation Plan

> **For the main agent:** Implement this plan directly in the current session. Do not dispatch implementation or code-review subagents. After all development tasks are complete, run the affected unit tests and fix any failures before reporting completion. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add reusable team-agent TTS, per-shot voiceover, subtitle generation, final audio/video composition, and a built-in 16:9 project introduction workflow to Flow Studio.

**Architecture:** A focused `speech.py` adapter owns the team-agent voice-service protocol and WAV validation. Existing video node dispatch gains `voiceover` and `video_compose`; the first creates Workspace audio assets and the second aligns clips, creates SRT, burns subtitles, muxes narration, and saves all final artifacts. `VideoModels` remains the Workspace-scoped configuration owner.

**Tech Stack:** Python 3.12, FastAPI, httpx, SQLite-backed AssetStore, ffmpeg/ffprobe, vanilla JavaScript, pytest.

## Global Constraints

- Default output is 16:9, 60–90 seconds, Chinese Serena narration.
- Reuse team-agent `voice-service`; do not copy its model or runtime into Flow Studio.
- Add no Python dependency; use existing httpx and ffmpeg.
- Preserve `video-demo`, `merge_video`, existing API defaults, and dirty-worktree changes.
- Fail explicitly when TTS is unavailable; image/video generation may retain placeholder degradation.
- Never put a TTS token into logs, node output, exceptions, or asset metadata.

---

### Task 1: team-agent Speech Adapter and Configuration

**Files:**
- Create: `src/flow_studio/speech.py`
- Modify: `src/flow_studio/video.py`
- Modify: `src/flow_studio/web/app.js`
- Unit tests: `tests/test_flow_video.py`

**Interfaces:**
- Consumes: team-agent `POST /v1/tts` and Workspace `VideoModels` JSON persistence.
- Produces: `synthesize_wav(config, text, *, voice, speed, session_id, generation) -> bytes` and `wav_duration(data) -> float`; `VideoModels.load()["speech"]`.

- [ ] **Step 1: Implement validated TTS HTTP calls**

```python
def synthesize_wav(config, text, *, voice="", speed=1.0, session_id="", generation=0):
    endpoint = validate_voice_url(config["base_url"])
    response = httpx.post(f"{endpoint}/v1/tts", headers=authorization(config), json={
        "sessionId": session_id,
        "generation": generation,
        "text": text,
        "voice": voice or config.get("voice") or "Serena",
        "speed": speed,
    }, timeout=float(config.get("timeout") or 120))
    response.raise_for_status()
    validate_wav(response.content)
    return response.content
```

- [ ] **Step 2: Add speech defaults and model-dialog fields**

Add `speech={enabled,base_url,token,voice,speed,timeout}` to `DEFAULT_MODELS`; render matching fields in `MODEL_FIELDS` and label the section “文本转语音（team-agent voice-service）”.

- [ ] **Step 3: Cover protocol and security behavior**

```python
def test_synthesize_wav_uses_team_agent_contract(monkeypatch):
    data = synthesize_wav(cfg, "你好", voice="Serena", speed=1.0,
                          session_id="flow-run-1", generation=0)
    assert captured["json"]["text"] == "你好"
    assert captured["headers"]["Authorization"] == "Bearer hidden"
    assert b"hidden" not in data
```

Test loopback without token, remote without token rejection, text/speed bounds, HTTP error sanitization, invalid RIFF and WAV duration.

### Task 2: Voiceover Node

**Files:**
- Modify: `src/flow_studio/graph.py`
- Modify: `src/flow_studio/engine.py`
- Modify: `src/flow_studio/video_nodes.py`
- Modify: `src/flow_studio/web/app.js`
- Unit tests: `tests/test_flow_video.py`

**Interfaces:**
- Consumes: `storyboard.shots[]`, `VideoModels.load()["speech"]`, `synthesize_wav`, `AssetStore.add_bytes`.
- Produces: `run_voiceover(...) -> {tracks,subtitles,count,total_duration,voice,speed,mode,text}`.

- [ ] **Step 1: Register the node type and executor dispatch**

```python
"voiceover": {
    "label": "配音", "icon": "🔊", "color": "#2563eb",
    "form": [
        {"key": "shots_source", "widget": "textarea", "default": "{{storyboard.shots}}"},
        {"key": "voice", "widget": "text", "default": ""},
        {"key": "speed", "widget": "number", "default": 1.0},
    ],
}
```

Add `voiceover` to engine video dispatch, `execute_video_node`, palette grouping, node summaries and help copy.

- [ ] **Step 2: Generate and persist one track per shot**

Resolve the source list, require non-empty `narration`, call `synthesize_wav` with `flow-<run_id>-<index>`, calculate duration, and save `kind="audio"`, `ext="wav"` with public metadata only.

- [ ] **Step 3: Test batch ordering and failure boundaries**

```python
def test_voiceover_synthesizes_every_shot_in_order(monkeypatch, tmp_path):
    run = runner.run(graph_with_voiceover, {})
    output = run.node_run("voice").output
    assert [track["index"] for track in output["tracks"]] == [1, 2]
    assert all(media.get(track["asset_id"])["kind"] == "audio"
               for track in output["tracks"])
```

Assert empty narration, disabled speech, unavailable service and invalid WAV fail without persisting credentials.

### Task 3: Final Video Composition

**Files:**
- Modify: `src/flow_studio/video.py`
- Modify: `src/flow_studio/video_nodes.py`
- Modify: `src/flow_studio/graph.py`
- Modify: `src/flow_studio/engine.py`
- Modify: `src/flow_studio/web/app.js`
- Unit tests: `tests/test_flow_video.py`

**Interfaces:**
- Consumes: ordered `shot_video.clips`, `voiceover.tracks`, `voiceover.subtitles`, and AssetStore files.
- Produces: `compose_narrated_video(clips, voiceovers, subtitles, out_path, srt_path, burn_subtitles=True) -> dict` and node output containing final video/audio/subtitle assets.

- [ ] **Step 1: Implement alignment, SRT and ffmpeg composition**

For each index, compute `target=max(video_duration,audio_duration+0.35)`, extend video with `tpad=stop_mode=clone`, pad audio with `apad`, trim both to target, render subtitles with libass when enabled, and concatenate normalized video/audio streams into H.264/AAC MP4. Generate SRT intervals from cumulative target durations.

- [ ] **Step 2: Register and implement `video_compose`**

```python
def run_video_compose(node, ns, media, vmodels, llm_cfg):
    clips = _resolve_list(node.params.get("clips_source"), ns)
    voices = _resolve_list(node.params.get("voiceovers_source"), ns)
    subtitles = _resolve_list(node.params.get("subtitles_source"), ns)
    validate_aligned_indexes(clips, voices, subtitles)
    result = compose_narrated_video(...)
    return persist_video_audio_and_srt(media, result, title, flow_id)
```

Add graph form fields, engine dispatch, palette metadata, node description and media previews.

- [ ] **Step 3: Test real ffmpeg artifacts**

Create two placeholder MP4 clips and two deterministic WAV fixtures, compose them, then assert final duration, audio stream presence via ffprobe, SRT ordering, video/audio/SRT assets, and failure for mismatched indexes. Skip only when ffmpeg is absent.

### Task 4: Project Introduction Workflow and Documentation

**Files:**
- Modify: `src/flow_studio/builtin_flows.py`
- Modify: `src/flow_studio/README.md`
- Modify: `src/flow_studio/web/index.html`
- Unit tests: `tests/test_flow_builtin.py`
- Integration tests: `tests/test_flow_video.py`

**Interfaces:**
- Consumes: registered `storyboard`, `keyframe`, `shot_video`, `voiceover`, `video_compose` nodes.
- Produces: built-in `project-intro-video` flow and user-visible documentation.

- [ ] **Step 1: Add the built-in 8-shot flow**

Define start inputs `project_name` and `project_brief`, defaulting to Flow Studio public product content. Connect storyboard → keyframe → shot_video → voiceover → video_compose → end, with 16:9 and technology-product visual style.

- [ ] **Step 2: Strengthen narration fallback and target duration instructions**

Set fallback narration to the shot description. Tell the storyboard LLM to produce 8 concise Chinese narration segments totaling 60–90 seconds and set durations consistent with narration.

- [ ] **Step 3: Update user-facing node and model documentation**

Document the two nodes, the team-agent endpoint/configuration, explicit TTS failure behavior, independent WAV/SRT outputs and the `project-intro-video` built-in flow. Bump frontend asset versions so the running static server reloads the controls.

- [ ] **Step 4: Add API and built-in graph assertions**

```python
def test_project_intro_flow_contains_voiceover_and_compose(client):
    flow = client.get("/api/flows/project-intro-video").json()
    assert [node["type"] for node in flow["nodes"]][-3:] == [
        "voiceover", "video_compose", "end"]
```

Assert node-type API includes both new nodes and model API includes speech defaults.

### Task 5: Real Local TTS and Browser Acceptance

**Files:**
- Modify only if an acceptance failure requires a source fix.

**Interfaces:**
- Consumes: live `http://127.0.0.1:17863`, an isolated Flow Studio data directory, and the finished frontend.
- Produces: one real Serena WAV and one narrated/subtitled project-introduction MP4 in isolated assets.

- [ ] **Step 1: Probe and synthesize real local speech**

Require `/health` to report `tts=true`, synthesize a short Chinese line, verify RIFF/WAVE and non-zero duration, and do not print audio bytes or tokens.

- [ ] **Step 2: Run the built-in flow against isolated data**

Use placeholder image/video generation plus live TTS, assert the run succeeds and the final MP4 contains video and AAC audio streams. Confirm WAV and SRT download routes return 200.

- [ ] **Step 3: Verify desktop and mobile UI with ego-browser**

Using one TaskSpace, verify speech settings render/save, both nodes appear in the video palette, running status/logs advance through voiceover and compose, and audio/video controls fit desktop and 390×844 viewports without horizontal page overflow.

## Final Unit Test Verification

- [ ] **Main agent: run affected unit tests after development is complete**

Run: `.venv/bin/python -m pytest tests/test_flow_video.py tests/test_flow_builtin.py tests/test_flow_engine.py tests/test_flow_server.py -q`

Expected: PASS

Then run: `.venv/bin/python -m pytest -q`

Expected: PASS

If a test fails, fix the implementation or test and rerun these commands until they pass. Report the commands and results in the final response.
