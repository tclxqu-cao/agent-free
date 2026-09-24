"""team-agent voice-service TTS adapter and WAV validation."""

from __future__ import annotations

import io
import ipaddress
import wave
from urllib.parse import urlsplit, urlunsplit

import httpx

from .run_events import redact_log

MAX_TTS_TEXT = 600
MAX_WAV_BYTES = 64 * 1024 * 1024


def _is_loopback(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_voice_url(value: str) -> str:
    """Return a normalized HTTP(S) base URL with no credentials/query/fragment."""
    raw = str(value or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("TTS 服务地址必须是有效的 http/https URL")
    if parsed.username or parsed.password:
        raise ValueError("TTS 服务地址不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("TTS 服务地址不能包含 query 或 fragment")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def wav_duration(data: bytes) -> float:
    """Validate PCM-compatible WAV bytes and return their duration in seconds."""
    if not data or len(data) > MAX_WAV_BYTES:
        raise ValueError("TTS 返回的 WAV 为空或超过 64 MiB")
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("TTS 返回的内容不是有效 WAV")
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            rate = wav.getframerate()
            frames = wav.getnframes()
            if rate <= 0 or frames <= 0 or wav.getnchannels() <= 0:
                raise ValueError("TTS 返回的 WAV 没有有效音频帧")
            return round(frames / rate, 3)
    except (wave.Error, EOFError) as exc:
        raise ValueError("TTS 返回的 WAV 结构损坏") from exc


def synthesize_wav(config: dict, text: str, *, voice: str = "", speed: float = 1.0,
                   session_id: str = "", generation: int = 0) -> bytes:
    """Generate a complete WAV through team-agent's compatibility HTTP endpoint."""
    if not config.get("enabled", True):
        raise RuntimeError("TTS 未启用")
    content = str(text or "").strip()
    if not content:
        raise ValueError("配音文本不能为空")
    if len(content) > MAX_TTS_TEXT:
        raise ValueError(f"单段配音不能超过 {MAX_TTS_TEXT} 个字符")
    try:
        rate = float(speed)
    except (TypeError, ValueError) as exc:
        raise ValueError("配音语速必须是数字") from exc
    if rate < 0.5 or rate > 2.0:
        raise ValueError("配音语速必须在 0.5–2.0 之间")

    base_url = validate_voice_url(config.get("base_url") or "http://127.0.0.1:17863")
    parsed = urlsplit(base_url)
    token = str(config.get("token") or "").strip()
    if not _is_loopback(parsed.hostname) and not token:
        raise ValueError("非本机 TTS 服务必须配置 token")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = {
        "sessionId": str(session_id or "flow-studio"),
        "generation": max(0, int(generation)),
        "text": content,
        "voice": str(voice or config.get("voice") or "Serena").strip() or "Serena",
        "speed": rate,
    }
    try:
        response = httpx.post(
            f"{base_url}/v1/tts", headers=headers, json=payload,
            timeout=float(config.get("timeout") or 120))
        if response.status_code >= 400:
            error = ""
            try:
                body = response.json()
                error = str(body.get("error") or "") if isinstance(body, dict) else ""
            except ValueError:
                pass
            detail = f"：{redact_log(error)[:160]}" if error else ""
            raise RuntimeError(f"TTS 服务返回 HTTP {response.status_code}{detail}")
        data = response.content
    except httpx.TimeoutException as exc:
        raise RuntimeError("TTS 服务请求超时") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"TTS 服务连接失败：{type(exc).__name__}") from exc
    wav_duration(data)
    return data
