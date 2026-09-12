"""视频生成适配层：模型配置、OpenAI 兼容图片/视频生成、占位素材、ffmpeg 合成。

设计原则（与项目一致）：
- 模型未配置或调用失败一律降级为占位素材（占位 PNG 纯 Python 生成零依赖；
  占位视频优先 ffmpeg lavfi，无 ffmpeg 时退纯 Python GIF），保证流水线可跑通；
- 视频 API 走通用两段式（提交任务 + 轮询），提交/轮询路径可配置，
  兼容 OpenAI 风格中转（硅基流动 / 云雾等）；
- 合成长片用 ffmpeg concat demuxer，重编码统一参数（H.264 + yuv420p）。
"""

from __future__ import annotations

import base64
import colorsys
import json
import os
import re
import shutil
import struct
import subprocess
import time
import zlib
from pathlib import Path

import httpx

ASPECTS = {"16:9": (640, 360), "9:16": (360, 640), "1:1": (512, 512), "4:3": (640, 480)}
DEFAULT_ASPECT = "16:9"

DEFAULT_MODELS: dict = {
    "llm": {"use_config_llm": True, "base_url": "", "api_key": "", "model": "", "timeout": 90},
    "image": {"enabled": False, "base_url": "", "api_key": "", "model": "",
              "size_map": {"16:9": "1344x768", "9:16": "768x1344",
                           "1:1": "1024x1024", "4:3": "1024x768"},
              "timeout": 120},
    "video": {"enabled": False, "provider": "http", "base_url": "", "api_key": "", "model": "",
              "submit_path": "/videos/generations", "poll_path": "/videos/{task_id}",
              "status_field": "status", "url_fields": "video_url,output.url,url,data[0].url",
              "done_words": "succeeded,success,completed,complete,done,finished",
              "interval": 5, "timeout": 600, "extra": {},
              # provider = "hf_gradio"：调 Hugging Face Space（Gradio API，匿名免费，
              # 依赖经 uv --with gradio_client 运行时注入，核心零依赖不变）
              "hf_space": "Saravutw/WAN2.2_I2V_LIGHTNING_4-8step_custom",
              "hf_api": "/generate_video",
              "hf_token": "",
              "hf_params": {"steps": 4, "negative_prompt": "static, blurry, low quality, distorted",
                            "guidance_scale": 3.5, "guidance_scale_2": 3.5,
                            "quality": 1, "flow_shift": 3, "frame_multiplier": 16}},
}


class VideoModels:
    """三类模型配置（llm / image / video），落盘 data/video_models.json。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._cache: dict | None = None

    def load(self) -> dict:
        if self._cache is None:
            data: dict = {}
            if self.path.exists():
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001 损坏配置按空处理
                    data = {}
            merged = {}
            for key, defaults in DEFAULT_MODELS.items():
                cfg = dict(defaults)
                cfg.update(data.get(key) or {})
                merged[key] = cfg
            self._cache = merged
        return self._cache

    def save(self, data: dict) -> dict:
        merged = {}
        for key, defaults in DEFAULT_MODELS.items():
            cfg = dict(defaults)
            cfg.update(data.get(key) or {})
            merged[key] = cfg
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        self._cache = merged
        return merged

    def effective_llm(self, config_llm: dict | None = None) -> dict:
        """分镜/文案用 LLM：优先独立配置，use_config_llm 时继承 config.llm。"""
        llm = dict(self.load().get("llm") or {})
        if llm.get("use_config_llm") and config_llm:
            return {**llm, **{k: v for k, v in config_llm.items() if v}}
        return llm


def _norm_aspect(ratio: str | None) -> str:
    r = str(ratio or "").strip()
    return r if r in ASPECTS else DEFAULT_ASPECT


def _seed_hue(seed: str) -> int:
    return sum(seed.encode("utf-8")) % 360


def _hue_rgb(hue: int, sat: float = 0.55, val: float = 0.85) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb((hue % 360) / 360.0, sat, val)
    return int(r * 255), int(g * 255), int(b * 255)


# ---------------------------------------------------------------- 占位 PNG（零依赖）
_FONT: dict[str, list[str]] = {  # 5x7 像素点阵（仅占位标号需要的字符）
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    " ": ["00000", "00000", "00000", "00000", "00000", "00000", "00000"],
}


def _draw_label(px: bytearray, w: int, text: str, scale: int, rgb: tuple[int, int, int]) -> None:
    glyphs = [_FONT[c] for c in text.upper() if c in _FONT]
    if not glyphs:
        return
    gw = sum(len(g[0]) * scale + scale for g in glyphs)
    gh = 7 * scale
    x0 = max(8, (w - gw) // 2)
    y0 = 12
    for gi, g in enumerate(glyphs):
        gx = x0 + gi * (5 * scale + scale)
        for ry, row in enumerate(g):
            for rx, bit in enumerate(row):
                if bit != "1":
                    continue
                for dy in range(scale):
                    y = y0 + ry * scale + dy
                    if y >= len(px) // (w * 3):
                        continue
                    for dx in range(scale):
                        x = gx + rx * scale + dx
                        if x >= w:
                            continue
                        o = (y * w + x) * 3
                        px[o:o + 3] = bytes(rgb)


def placeholder_png_bytes(label: str, aspect_ratio: str | None = None,
                          seed: str = "") -> bytes:
    """生成占位 PNG：seed 决定色相的渐变 + 白色网格 + 标号点阵。零第三方依赖。"""
    w, h = ASPECTS[_norm_aspect(aspect_ratio)]
    hue = _seed_hue(seed or label)
    top = _hue_rgb(hue, 0.5, 0.92)
    bottom = _hue_rgb(hue + 40, 0.6, 0.45)
    px = bytearray(w * h * 3)
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(top[0] * (1 - t) + bottom[0] * t)
        g = int(top[1] * (1 - t) + bottom[1] * t)
        b = int(top[2] * (1 - t) + bottom[2] * t)
        row = bytes((r, g, b)) * w
        px[y * w * 3:(y + 1) * w * 3] = row
    # 网格线（半透明白 → 直接混色）
    grid = _hue_rgb(hue, 0.1, 1.0)
    for gy in range(0, h, 40):
        for x in range(w):
            o = (gy * w + x) * 3
            px[o:o + 3] = bytes(grid)
    for gx in range(0, w, 40):
        for y in range(h):
            o = (y * w + gx) * 3
            px[o:o + 3] = bytes(grid)
    _draw_label(px, w, label, 6, (255, 255, 255))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + px[y * w * 3:(y + 1) * w * 3] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


# ---------------------------------------------------------------- ffmpeg 工具
def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _run(cmd: list[str], timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def probe_duration(path: Path) -> float:
    """ffprobe 读时长；失败返回 0。"""
    ff = shutil.which("ffprobe")
    if not ff:
        return 0.0
    try:
        r = _run([ff, "-v", "error", "-show_entries", "format=duration",
                  "-of", "json", str(path)])
        return float(json.loads(r.stdout.decode() or "{}").get("format", {}).get("duration") or 0)
    except Exception:  # noqa: BLE001
        return 0.0


# ---------------------------------------------------------------- 占位视频
def placeholder_video_bytes(label: str, aspect_ratio: str | None = None,
                            duration: float = 3.0, seed: str = "") -> tuple[bytes, str]:
    """占位视频：(mp4, 'mp4') 优先 ffmpeg 动态渐变；无 ffmpeg 时纯 Python GIF。

    返回 (数据, 扩展名)。两者浏览器均可直接预览，ffmpeg 也都能读。
    """
    ratio = _norm_aspect(aspect_ratio)
    w, h = ASPECTS[ratio]
    duration = max(1.0, min(float(duration or 3.0), 30.0))
    if has_ffmpeg():
        import tempfile

        # -movflags +faststart 需要可 seek 输出，不能写 stdout 管道 → 落临时文件
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
            tmp_path = Path(tf.name)
        try:
            hue = _seed_hue(seed or label)
            c0 = "0x%02x%02x%02x" % _hue_rgb(hue, 0.55, 0.25)
            c1 = "0x%02x%02x%02x" % _hue_rgb(hue + 60, 0.6, 0.85)
            src = (f"gradients=s={w}x{h}:d={duration}:r=12:speed=0.35:nb_colors=2:"
                   f"c0={c0}:c1={c1}")
            out = _run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src,
                        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart", str(tmp_path)],
                       timeout=max(120, duration * 20))
            if out.returncode != 0:
                src = f"testsrc2=s={w}x{h}:d={duration}:r=12"
                out = _run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src,
                            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt",
                            "yuv420p", "-movflags", "+faststart", str(tmp_path)],
                           timeout=max(120, duration * 20))
            if out.returncode == 0 and tmp_path.exists() and tmp_path.stat().st_size:
                return tmp_path.read_bytes(), "mp4"
        finally:
            tmp_path.unlink(missing_ok=True)
    # GIF 兜底：未压缩式编码体积大 → 半尺寸保流畅
    return _gif_bytes(label, w // 2, h // 2, duration, seed), "gif"


_GLYPH = {"0": "01110 10001 10011 10101 11001 10001 01110",
          "1": "00100 01100 00100 00100 00100 00100 01110",
          "2": "01110 10001 00001 00010 00100 01000 11111",
          "3": "11110 00001 00001 01110 00001 00001 11110",
          "4": "00010 00110 01010 10010 11111 00010 00010",
          "5": "11111 10000 11110 00001 00001 10001 01110",
          "6": "00110 01000 10000 11110 10001 10001 01110",
          "7": "11111 00001 00010 00100 01000 01000 01000",
          "8": "01110 10001 10001 01110 10001 10001 01110",
          "9": "01110 10001 10001 01111 00001 00010 01100",
          "S": "01111 10000 10000 01110 00001 00001 11110",
          "H": "10001 10001 10001 11111 10001 10001 10001",
          "K": "10001 10010 10100 11000 10100 10010 10001",
          "-": "00000 00000 00000 11111 00000 00000 00000",
          " ": "00000 00000 00000 00000 00000 00000 00000"}


def _gif_bytes(label: str, w: int, h: int, duration: float, seed: str) -> bytes:
    """纯 Python GIF89a（无 ffmpeg 兜底）：256 色动渐变 + 标号点阵，12fps。"""
    frames = max(int(duration * 12), 2)
    hue = _seed_hue(seed or label)
    # 全局调色板：灰阶 256 级（帧内像素即索引）
    palette = bytes(i for i in range(256) for _ in range(3))
    glyphs = [[list(map(int, row)) for row in _GLYPH[c].split()]
              for c in label.upper() if c in _GLYPH]

    def frame_bytes(fi: int) -> bytes:
        phase = fi / frames
        buf = bytearray(w * h)
        for y in range(h):
            v = (y / h * 0.55 + phase * 0.3)
            base = int(v * 255)
            for x in range(w):
                buf[y * w + x] = min(255, max(0, base + (x * 60 // w) - 20))
        # 点阵标号居中
        gw = sum(6 * len(g[0]) for g in glyphs)
        x0 = max(4, (w - gw) // 2) if glyphs else 0
        for gi, g in enumerate(glyphs):
            for ry, row in enumerate(g):
                for rx, bit in enumerate(row):
                    if not bit:
                        continue
                    bx, by = x0 + gi * 6 * 5 + rx * 6, 10 + ry * 6
                    if by + 6 <= h:
                        for dy in range(6):
                            yy = by + dy
                            if yy < h:
                                for dx in range(6):
                                    xx = bx + dx
                                    if xx < w:
                                        buf[yy * w + xx] = 255
        return bytes(buf)

    def lzw(px: bytes) -> bytes:
        """未压缩式 LZW：ClearCode + 字面量，每 253 个像素重发 Clear 防表增长。"""
        bits_out = bytearray()
        acc = nbits = 0

        def put(code: int) -> None:
            nonlocal acc, nbits
            acc |= code << nbits
            nbits += 9
            while nbits >= 8:
                bits_out.append(acc & 0xFF)
                acc >>= 8
                nbits -= 8

        put(256)  # clear
        for i, p in enumerate(px):
            put(p)
            if (i + 1) % 253 == 0:
                put(256)
        put(257)  # EOI
        if nbits:
            bits_out.append(acc & 0xFF)
        return bytes(bits_out)

    out = bytearray(b"GIF89a")
    out += struct.pack("<HHBBB", w, h, 0xF7, 0, 0) + palette
    delay = int(100 / 12)
    for fi in range(frames):
        # GCE: 0x21 0xF9 0x04 <packed=0> <delay:2 LE> <transparent=0> <terminator>
        out += (b"\x21\xF9\x04\x00" + struct.pack("<HB", delay, 0) + b"\x00")
        out += b"\x2C" + struct.pack("<HHHHB", 0, 0, w, h, 0)
        data = lzw(frame_bytes(fi))
        out += b"\x08"
        for i in range(0, len(data), 255):
            sub = data[i:i + 255]
            out += bytes((len(sub),)) + sub
        out += b"\x00"
    out += b"\x3B"
    return bytes(out)


# ---------------------------------------------------------------- 模型生成（可降级）
class _GenResult(dict):
    @property
    def mode(self) -> str:
        return self.get("mode", "")


def _download(url: str, timeout: float) -> bytes:
    with httpx.get(url, timeout=timeout, follow_redirects=True) as r:
        r.raise_for_status()
        return r.content


def gen_image(vcfg: dict, prompt: str, aspect_ratio: str | None,
              label: str, seed: str = "") -> dict:
    """文生图：OpenAI 兼容 /images/generations。任何失败 → 占位 PNG。"""
    ratio = _norm_aspect(aspect_ratio)
    if vcfg.get("enabled") and vcfg.get("base_url") and vcfg.get("api_key"):
        size = (vcfg.get("size_map") or {}).get(ratio) or "1024x1024"
        try:
            # httpx 顶层 post 返回的 Response 不支持 with，直接接住
            r = httpx.post(
                f"{str(vcfg['base_url']).rstrip('/')}/images/generations",
                headers={"Authorization": f"Bearer {vcfg['api_key']}"},
                json={"model": vcfg.get("model") or "gpt-image-1",
                      "prompt": prompt, "size": size, "n": 1,
                      "response_format": "b64_json"},
                timeout=float(vcfg.get("timeout") or 120),
            )
            if r.status_code >= 400 and "response_format" in (r.text or ""):
                r = httpx.post(  # gpt-image-1 等不接受 response_format → 去掉重试
                    f"{str(vcfg['base_url']).rstrip('/')}/images/generations",
                    headers={"Authorization": f"Bearer {vcfg['api_key']}"},
                    json={"model": vcfg.get("model") or "gpt-image-1",
                          "prompt": prompt, "size": size, "n": 1},
                    timeout=float(vcfg.get("timeout") or 120))
            r.raise_for_status()
            item = (r.json().get("data") or [{}])[0]
            if item.get("b64_json"):
                return {"mode": "model", "data": base64.b64decode(item["b64_json"]),
                        "ext": "png", "aspect_ratio": ratio}
            if item.get("url"):
                return {"mode": "model", "data": _download(item["url"], 120),
                        "ext": "png", "aspect_ratio": ratio}
        except Exception as e:  # noqa: BLE001 → 占位
            return {"mode": "placeholder", "aspect_ratio": ratio,
                    "error": f"图片模型调用失败：{type(e).__name__}: {e}",
                    "data": placeholder_png_bytes(label, ratio, seed), "ext": "png"}
    return {"mode": "placeholder", "aspect_ratio": ratio,
            "error": "图片模型未配置",
            "data": placeholder_png_bytes(label, ratio, seed), "ext": "png"}


_DONE_RE = re.compile(r"succeeded|success|completed|complete|done|finished", re.I)

_HF_SCRIPT = r"""
import json, sys
from gradio_client import Client, handle_file
space, api, img, prompt, params_json, out = sys.argv[1:7]
p = json.loads(params_json)
p["duration_seconds"] = float(max(2.0, min(float(p.get("duration_seconds") or 3.0), 10.0)))
p.setdefault("last_image", None)   # 可选首尾帧参数：gradio_client 也要求显式传值
c = Client(space, verbose=False)
res = c.predict(input_image=handle_file(img), prompt=prompt, api_name=api, **p)
paths = [str(x) for x in (res if isinstance(res, (list, tuple)) else [res]) if x]
video = next((x for x in paths if str(x).lower().endswith((".mp4", ".webm", ".gif"))), None)
open(out, "w").write(json.dumps({"paths": paths, "video": video}))
"""


def _gen_video_hf_space(vcfg: dict, prompt: str, first_frame_bytes: bytes,
                        duration: float, ratio: str, seed: str) -> dict:
    """通过 gradio_client（uv 运行时注入）匿名调用 HF Space 图生视频。"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        img = td / "first_frame.png"
        img.write_bytes(first_frame_bytes)
        params = dict(vcfg.get("hf_params") or {})
        params["duration_seconds"] = duration
        out_json = td / "result.json"
        timeout = max(600.0, float(vcfg.get("timeout") or 600))
        env = None
        if vcfg.get("hf_token"):  # 免费注册的 HF token → ZeroGPU 配额大幅提高
            env = {**os.environ, "HF_TOKEN": str(vcfg["hf_token"])}
        r = subprocess.run(
            ["uv", "run", "--with", "gradio_client", "--no-project", "python",
             "-c", _HF_SCRIPT, str(vcfg.get("hf_space") or ""), str(vcfg.get("hf_api") or ""),
             str(img), prompt, json.dumps(params), str(out_json)],
            capture_output=True, text=True, timeout=timeout, env=env)
        if not out_json.exists():
            err = (r.stderr or r.stdout or "")[-300:]
            raise RuntimeError(f"gradio_client 调用失败：{err}")
        res = json.loads(out_json.read_text(encoding="utf-8"))
        video_path = res.get("video")
        if not video_path or not Path(video_path).exists():
            raise RuntimeError(f"Space 未返回视频文件：{str(res)[:160]}")
        return {"mode": "model", "data": Path(video_path).read_bytes(), "ext": "mp4",
                "aspect_ratio": ratio}


def gen_video(vcfg: dict, prompt: str, first_frame_bytes: bytes | None,
              duration: float, aspect_ratio: str | None,
              label: str, seed: str = "") -> dict:
    """图生视频：provider=http 走通用两段式（提交+轮询）；provider=hf_gradio 走
    Hugging Face Space（Gradio API 匿名调用）。任何失败 → 占位视频。"""
    ratio = _norm_aspect(aspect_ratio)

    def placeholder(reason: str) -> dict:
        data, ext = placeholder_video_bytes(label, ratio, duration, seed)
        return {"mode": "placeholder", "data": data, "ext": ext,
                "aspect_ratio": ratio, "error": reason}

    if str(vcfg.get("provider") or "http") == "hf_gradio":
        if first_frame_bytes is None:
            return placeholder("hf_gradio 模式需要首帧图")
        try:
            return _gen_video_hf_space(vcfg, prompt, first_frame_bytes,
                                       duration, ratio, seed)
        except Exception as e:  # noqa: BLE001 → 占位
            return placeholder(f"HF Space 图生视频失败：{type(e).__name__}: {str(e)[:140]}")

    if not (vcfg.get("enabled") and vcfg.get("base_url") and vcfg.get("api_key")):
        return placeholder("视频模型未配置")
    base = str(vcfg["base_url"]).rstrip("/")
    headers = {"Authorization": f"Bearer {vcfg['api_key']}"}
    submit_path = str(vcfg.get("submit_path") or "/videos/generations")
    poll_path = str(vcfg.get("poll_path") or "/videos/{task_id}")
    body: dict = {"model": vcfg.get("model") or "", "prompt": prompt,
                  "duration": duration, "aspect_ratio": ratio}
    if first_frame_bytes:
        body["first_frame_image"] = ("data:image/png;base64,"
                                     + base64.b64encode(first_frame_bytes).decode())
    body.update(vcfg.get("extra") or {})
    try:
        r = httpx.post(f"{base}{submit_path}", headers=headers, json=body,
                       timeout=float(vcfg.get("timeout") or 600))
        r.raise_for_status()
        resp = r.json()
        if not isinstance(resp, dict):
            return placeholder(f"视频任务响应异常：{str(resp)[:120]}")
        task_id = (resp.get("id") or resp.get("task_id")
                   or (resp.get("data") or {}).get("task_id"))
        if not task_id:
            return placeholder(f"视频任务未返回 id：{str(resp)[:120]}")
        deadline = time.time() + float(vcfg.get("timeout") or 600)
        interval = max(1.0, float(vcfg.get("interval") or 5))
        url_fields = [f.strip() for f in str(
            vcfg.get("url_fields") or "video_url,output.url,url,data[0].url").split(",")]
        while time.time() < deadline:
            time.sleep(interval)
            pr = httpx.get(f"{base}{poll_path.format(task_id=task_id)}",
                           headers=headers, timeout=60)
            pr.raise_for_status()
            data = pr.json()
            status = str(data.get("status") or data.get("state") or "")
            if _DONE_RE.search(status):
                url = _dig(data, url_fields)
                if not url:
                    return placeholder(f"任务完成但未找到视频地址：{str(data)[:120]}")
                return {"mode": "model", "data": _download(url, 300), "ext": "mp4",
                        "aspect_ratio": ratio}
            if re.search(r"fail|error|cancel", status, re.I):
                return placeholder(f"视频任务失败：{status}")
        return placeholder("视频任务轮询超时")
    except Exception as e:  # noqa: BLE001 → 占位
        return placeholder(f"视频模型调用失败：{type(e).__name__}: {e}")


def _dig(data: dict, fields: list[str]) -> str | None:
    for f in fields:
        cur = data
        ok = True
        for seg in f.split("."):
            if isinstance(seg, str) and seg.endswith("]"):
                name, idx = seg[:-1].split("[", 1)
                try:
                    cur = (cur.get(name) if name else cur)[int(idx)]
                except Exception:  # noqa: BLE001
                    ok = False
                    break
            else:
                cur = cur.get(seg) if isinstance(cur, dict) else None
            if cur is None:
                ok = False
                break
        if ok and isinstance(cur, str) and cur.startswith("http"):
            return cur
    return None


# ---------------------------------------------------------------- 合成
def concat_videos(clips: list[dict], out_path: Path,
                  enforce_ratio: bool = True) -> dict:
    """按 clips 顺序 ffmpeg concat 成长片。clips 元素：{path(绝对), aspect_ratio, duration}。

    返回 {path, duration, count, aspect_ratio}；比例不一致且 enforce_ratio → ValueError。
    """
    if not clips:
        raise ValueError("没有可合成的片段")
    if not has_ffmpeg():
        raise RuntimeError("ffmpeg 不可用，无法合成视频")
    ratios = {c.get("aspect_ratio") or DEFAULT_ASPECT for c in clips}
    if enforce_ratio and len(ratios) > 1:
        raise ValueError(f"片段比例不一致：{'、'.join(sorted(ratios))}"
                         f"（可关闭 merge 节点的 enforce_ratio 强制合成）")
    ratio = sorted(ratios)[0] if len(ratios) == 1 else DEFAULT_ASPECT
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    list_file = out_path.with_suffix(".concat.txt")
    # concat demuxer 按列表文件所在目录解析相对路径 → 片段必须写绝对路径
    list_file.write_text(
        "".join(f"file '{Path(c['path']).resolve()}'\n" for c in clips), encoding="utf-8")
    r = _run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
              "-i", str(list_file), "-c:v", "libx264", "-preset", "veryfast",
              "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_path)],
             timeout=600)
    list_file.unlink(missing_ok=True)
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "ignore")[-400:]
        raise RuntimeError(f"ffmpeg 合成失败：{err}")
    w, h = ASPECTS[ratio]
    return {"path": str(out_path), "duration": round(probe_duration(out_path), 2),
            "count": len(clips), "aspect_ratio": ratio, "size": f"{w}x{h}"}
