"""Route application logs to the currently executing Flow node, per context."""

from __future__ import annotations

import logging
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar


_consumer: ContextVar = ContextVar("flow_node_log_consumer", default=None)
_capturing: ContextVar = ContextVar("flow_node_log_capture", default=False)
_install_lock = threading.Lock()
_handler = None


def redact_log(text: str) -> str:
    """Keep traceback frames intact while removing common credential values."""
    text = re.sub(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[REDACTED]@", text)
    keys = (r"api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|"
            r"secret|authorization|proxy-authorization|cookie|set-cookie")
    # Quoted values can include spaces or semicolons (passwords, Cookie headers).
    text = re.sub(
        rf"(?i)([\"']?(?:{keys})[\"']?\s*[:=]\s*)(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')",
        lambda m: m[1] + m[2][0] + "[REDACTED]" + m[2][-1], text)
    # An unquoted header comprises its entire line, not only the first word.
    text = re.sub(r"(?im)(\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*)[^\r\n]+",
                  r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(\bBearer\s+)[\w.\-+/=]+", r"\1[REDACTED]", text)
    return re.sub(
        rf"(?i)([\"']?(?:{keys})[\"']?\s*[:=]\s*[\"']?)([^\s&\"'<>]+)",
        r"\1[REDACTED]", text)


class _NodeLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        consumer = _consumer.get()
        if consumer is None or _capturing.get():
            return
        if not record.name.startswith(("flow_studio", "job_agent")):
            return
        token = _capturing.set(True)
        try:
            trace = logging.Formatter().formatException(record.exc_info) if record.exc_info else None
            consumer(record.levelname.lower(), redact_log(record.getMessage()),
                     redact_log(trace) if trace else None)
        finally:
            _capturing.reset(token)


def install_log_capture() -> None:
    global _handler
    with _install_lock:
        if _handler is not None:
            return
        _handler = _NodeLogHandler(logging.INFO)
        logging.getLogger().addHandler(_handler)
        for name in ("flow_studio", "job_agent"):
            logger = logging.getLogger(name)
            if logger.getEffectiveLevel() > logging.INFO:
                logger.setLevel(logging.INFO)


@contextmanager
def capture_node_logs(consumer):
    install_log_capture()
    token = _consumer.set(consumer)
    try:
        yield
    finally:
        _consumer.reset(token)
