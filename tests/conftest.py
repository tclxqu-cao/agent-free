import shutil
from pathlib import Path

import pytest

from job_agent.db import DB
from job_agent.models import load_config, load_profile


@pytest.fixture
def config_dir(tmp_path) -> Path:
    """把示例配置复制为 tmp 下的真实配置。"""
    src = Path(__file__).resolve().parent.parent / "config"
    dst = tmp_path / "config"
    dst.mkdir()
    for name in ("config.example.yaml", "profile.example.yaml"):
        shutil.copy(src / name, dst / name.replace(".example", ""))
    return dst


@pytest.fixture
def config(config_dir):
    cfg = load_config(config_dir)
    cfg["data_dir"] = config_dir.parent / "data"  # 隔离到 tmp
    return cfg


@pytest.fixture
def profile(config_dir):
    return load_profile(config_dir)


@pytest.fixture
def db(config, config_dir, tmp_path) -> DB:
    return DB(config["data_dir"] / "job_agent.db")
