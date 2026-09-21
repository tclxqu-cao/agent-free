import shutil
from pathlib import Path

import pytest

from job_agent.db import DB
from job_agent.models import load_config, load_profile


def make_governed_client(app, username: str = "owner"):
    """Create a real initialized/login TestClient without an auth bypass."""
    from fastapi.testclient import TestClient

    client = TestClient(app)
    response = client.post("/api/setup", json={
        "username": username, "display_name": "Test Owner",
        "password": "test-password-123",
    })
    assert response.status_code == 200, response.text
    client.headers.update({
        "X-CSRF-Token": response.json()["csrf_token"],
        "X-Workspace-ID": "default",
    })
    return client


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
