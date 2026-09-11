"""CLI 进程级端到端：子进程执行 job-agent 子命令链。"""

import subprocess
import sys
from pathlib import Path


def run_cli(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    r = subprocess.run(
        [sys.executable, "-m", "job_agent", "--config-dir", "config", *args],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert r.returncode == 0, f"cmd {args} failed:\n{r.stdout}\n{r.stderr}"
    return r


def test_cli_init_creates_configs(tmp_path):
    r = run_cli(tmp_path, "init")
    assert (tmp_path / "config" / "config.yaml").exists()
    assert (tmp_path / "config" / "profile.yaml").exists()
    assert "init" in r.stdout


def test_cli_init_never_overwrites(tmp_path):
    run_cli(tmp_path, "init")
    custom = "# my custom config"
    (tmp_path / "config" / "config.yaml").write_text(custom, encoding="utf-8")
    run_cli(tmp_path, "init")
    assert (tmp_path / "config" / "config.yaml").read_text(encoding="utf-8") == custom


def test_cli_full_chain(tmp_path):
    """init → demo → analyze → daily --skip-scrape → report 全链路。"""
    run_cli(tmp_path, "init")

    r = run_cli(tmp_path, "demo", "--days", "6")
    assert "模拟数据" in r.stdout and "日报" in r.stdout
    reports = list((tmp_path / "output").glob("*/report.md"))
    assert len(reports) == 1
    content = reports[0].read_text(encoding="utf-8")
    assert "今日匹配岗位" in content

    r = run_cli(tmp_path, "analyze")
    assert "数量趋势" in r.stdout and "薪资趋势" in r.stdout

    r = run_cli(tmp_path, "daily", "--skip-scrape")
    assert "匹配" in r.stdout and "日报" in r.stdout

    r = run_cli(tmp_path, "report")
    assert "已生成" in r.stdout


def test_cli_demo_keep_preserves_db(tmp_path):
    run_cli(tmp_path, "init")
    run_cli(tmp_path, "demo", "--days", "4")
    db_path = tmp_path / "data" / "job_agent.db"
    assert db_path.exists()
    first = db_path.stat().st_mtime_ns
    run_cli(tmp_path, "demo", "--days", "4", "--keep")
    assert db_path.stat().st_mtime_ns >= first


def test_cli_requires_config(tmp_path):
    r = subprocess.run(
        [sys.executable, "-m", "job_agent", "--config-dir", "config", "daily"],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode != 0
    assert "init" in (r.stdout + r.stderr)
