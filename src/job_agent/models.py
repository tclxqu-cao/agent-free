"""数据模型：岗位 Job 与统一的配置加载。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(config_dir: Path) -> dict:
    """加载 config.yaml，合并默认值；profile.yaml 单独加载。"""
    cfg = _read_yaml(config_dir / "config.yaml")
    example = _read_yaml(config_dir / "config.example.yaml")
    for key, val in example.items():
        cfg.setdefault(key, val)
    return cfg


def load_profile(config_dir: Path) -> dict:
    return _read_yaml(config_dir / "profile.yaml")


def data_dir(config: dict, config_path: Path) -> Path:
    base = Path(config.get("data_dir", config_path.parent.parent / "data"))
    base.mkdir(parents=True, exist_ok=True)
    (base / "auth").mkdir(exist_ok=True)
    (base / "dumps").mkdir(exist_ok=True)
    return base


@dataclass
class Job:
    """一条岗位记录。缺失字段一律 None / 空列表，绝不抛错。"""

    site: str
    title: str
    company: str
    url: str
    job_id: str = ""
    company_size: str | None = None
    industry: str | None = None
    salary_text: str | None = None
    salary_min: float | None = None   # 月薪下限（K）
    salary_max: float | None = None   # 月薪上限（K）
    city: str | None = None
    district: str | None = None
    address: str | None = None
    experience_text: str | None = None
    experience_min: float | None = None
    experience_max: float | None = None
    education: str | None = None
    responsibilities: list[str] = field(default_factory=list)   # 岗位职责
    requirements_extra: list[str] = field(default_factory=list)  # 特殊要求/任职要求
    skills: list[str] = field(default_factory=list)
    urgency: str | None = None          # high / medium / low
    urgency_reason: str | None = None
    views: int | None = None            # 浏览量
    greets: int | None = None           # 招呼量
    hr_name: str | None = None
    posted_text: str | None = None      # 如 “3天前”
    raw: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.job_id:
            self.job_id = hashlib.sha1(f"{self.site}|{self.url}".encode()).hexdigest()[:16]

    @property
    def salary_mid(self) -> float | None:
        if self.salary_min is None and self.salary_max is None:
            return None
        lo = self.salary_min if self.salary_min is not None else self.salary_max
        hi = self.salary_max if self.salary_max is not None else self.salary_min
        return (lo + hi) / 2
