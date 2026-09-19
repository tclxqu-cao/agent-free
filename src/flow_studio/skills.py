"""技能：SKILL.md 风格的指令包（YAML frontmatter + Markdown 正文）。

data/skills/<id>.md —— frontmatter 提供 name/description（给路由与 LLM 选
技能看），正文是注入提示词的指令内容。技能节点可直接把正文交给 LLM，
智能体也可把整份指令装配进 system 提示词。
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import yaml

_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)


class SkillStore:
    def __init__(self, base: Path):
        self.dir = Path(base)
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- 读取
    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("*.md")):
            try:
                skill = self._parse(p)
                out.append({"id": p.stem, "name": skill["name"],
                            "description": skill["description"],
                            "chars": len(skill["content"]),
                            "updated_at": _mtime(p)})
            except Exception:  # noqa: BLE001 单文件损坏不拖垮列表
                continue
        return out

    def get(self, skill_id: str) -> dict | None:
        p = self._path(skill_id)
        if p is None:
            return None
        skill = self._parse(p)
        skill["id"] = p.stem
        return skill

    # ---------------------------------------------------------------- 写入
    def save(self, skill_id: str, name: str, description: str,
             content: str) -> dict:
        sid = "".join(c for c in str(skill_id or "").strip()
                      if c.isalnum() or c in "-_")
        if not sid:
            raise ValueError("技能 id 只允许字母数字-_")
        front = yaml.safe_dump({"name": name or sid,
                                "description": description or ""},
                               allow_unicode=True, sort_keys=False).strip()
        self._path_or_new(sid).write_text(
            f"---\n{front}\n---\n\n{content or ''}\n", encoding="utf-8")
        return {"id": sid, "name": name or sid,
                "description": description or "", "content": content or ""}

    def delete(self, skill_id: str) -> bool:
        p = self._path(skill_id)
        if p is None:
            return False
        p.unlink()
        return True

    def seed_if_empty(self, skills: list[dict]) -> int:
        if any(self.dir.glob("*.md")):
            return 0
        for s in skills:
            self.save(s["id"], s["name"], s.get("description", ""),
                      s.get("content", ""))
        return len(skills)

    # ---------------------------------------------------------------- 内部
    def _path(self, skill_id: str) -> Path | None:
        sid = str(skill_id or "").strip()
        if not re.fullmatch(r"[\w-]+", sid):
            return None
        p = self.dir / f"{sid}.md"
        return p if p.exists() else None

    def _path_or_new(self, skill_id: str) -> Path:
        return self.dir / f"{skill_id}.md"

    @staticmethod
    def _parse(p: Path) -> dict:
        raw = p.read_text(encoding="utf-8")
        meta: dict = {}
        m = _FRONT.match(raw)
        if m:
            try:
                meta = yaml.safe_load(m.group(1)) or {}
            except yaml.YAMLError:
                meta = {}
        content = raw[m.end():] if m else raw
        return {"name": str(meta.get("name") or p.stem),
                "description": str(meta.get("description") or ""),
                "content": content.strip()}


def _mtime(p: Path) -> str:
    return dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")


DEFAULT_SKILLS = [
    {
        "id": "writing-polish",
        "name": "中文写作润色",
        "description": "把给出的文字改写得更通顺、专业，保留原意与格式",
        "content": (
            "你是一名中文编辑。请把用户给出的文字润色一遍：\n"
            "1. 修正语病与错别字，统一标点；\n"
            "2. 让表达更通顺、专业，但保留原有口吻与信息，不新增事实；\n"
            "3. 直接输出润色后的全文，不要解释。\n")
    },
    {
        "id": "code-review",
        "name": "代码评审",
        "description": "按清单评审代码：正确性 / 边界 / 可读性 / 安全",
        "content": (
            "你是一名资深工程师，请按以下清单评审用户给出的代码：\n"
            "1. 正确性：逻辑错误、边界条件、异常处理；\n"
            "2. 可读性：命名、结构、重复代码；\n"
            "3. 安全：注入、越权、敏感信息；\n"
            "4. 性能：明显的复杂度问题。\n"
            "按【严重 / 建议 / 可选】三级输出问题清单，每条给出修改示例。\n")
    },
]
