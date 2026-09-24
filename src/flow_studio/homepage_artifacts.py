"""Validated, atomic last-success cache for public portfolio artifacts."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path


class HomepageArtifactStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, skill: str) -> Path:
        if not re.fullmatch(r"portfolio-[a-z0-9-]+", str(skill or "")):
            raise ValueError("非法 Portfolio Skill")
        return self.root / f"{skill}.json"

    def get(self, skill: str) -> dict | None:
        path = self._path(skill)
        if not path.exists():
            return None
        return validate_artifact(json.loads(path.read_text(encoding="utf-8")), skill)

    def save(self, artifact: dict, skill: str) -> dict:
        clean = validate_artifact(artifact, skill)
        path = self._path(skill)
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        return clean

    def all(self) -> dict[str, dict]:
        output = {}
        for path in sorted(self.root.glob("portfolio-*.json")):
            try:
                skill = path.stem
                output[skill] = validate_artifact(
                    json.loads(path.read_text(encoding="utf-8")), skill)
            except (ValueError, OSError, json.JSONDecodeError):
                continue
        return output


def validate_artifact(value: object, expected_skill: str | None = None) -> dict:
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise ValueError("Portfolio artifact 版本无效")
    actual_skill = str(value.get("skill") or "")
    if not re.fullmatch(r"portfolio-[a-z0-9-]+", actual_skill):
        raise ValueError("Portfolio artifact Skill 无效")
    if expected_skill is not None and actual_skill != expected_skill:
        raise ValueError("Portfolio artifact Skill 不匹配")
    title = str(value.get("title") or "").strip()
    blocks = value.get("blocks")
    if not title or len(title) > 160 or not isinstance(blocks, list) or not 1 <= len(blocks) <= 24:
        raise ValueError("Portfolio artifact 结构无效")
    clean_blocks = []
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError("Portfolio artifact block 无效")
        kind = block.get("type")
        if kind == "text":
            text = str(block.get("text") or "")
            if not text or len(text) > 24_000:
                raise ValueError("Portfolio artifact 文本无效")
        elif kind in {"image", "video"}:
            src = str(block.get("src") or block.get("url") or "")
            if not _safe_url(src):
                raise ValueError("Portfolio artifact 媒体地址无效")
        elif kind == "html":
            html = str(block.get("html") or "")
            if (not html or len(html) > 80_000
                    or re.search(r"<\s*(?:script|iframe|object|embed|form|style|link|meta)\b|\son\w+\s*=|javascript:", html, re.I)):
                raise ValueError("Portfolio artifact HTML 无效")
        else:
            raise ValueError("Portfolio artifact block 类型无效")
        clean_block = dict(block)
        if kind in {"image", "video"}:
            clean_block["src"] = src
            clean_block.pop("url", None)
        clean_blocks.append(clean_block)
    return {**value, "schemaVersion": 1, "skill": actual_skill,
            "title": title, "blocks": clean_blocks}


def _safe_url(value: str) -> bool:
    if re.match(r"^https?://", value, re.I):
        return True
    return bool(re.match(r"^(?:/)?assets/[a-zA-Z0-9_./-]+$", value)) \
        and ".." not in value.split("/")
