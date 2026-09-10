"""端到端：demo 数据 → run_daily(跳过抓取) → 报告生成。"""

import pytest

from job_agent.demo import seed_demo_data
from job_agent.scheduler import run_daily


@pytest.fixture
def demo_ready(db, config, config_dir):
    seed_demo_data(db, config, days=8)
    return config, config_dir, db


class TestE2E:
    def test_run_daily_produces_report(self, demo_ready):
        config, config_dir, db = demo_ready
        summary = run_daily(config_dir, skip_scrape=True, config=config, db=db)
        assert summary["passed"] > 0
        content = open(summary["report"], encoding="utf-8").read()
        for section in ("今日匹配岗位", "岗位数量趋势", "岗位要求变化",
                        "学习建议", "更上一层", "未来趋势展望", "新增 / 下架岗位"):
            assert section in content, f"报告缺少章节: {section}"
        # 匹配岗位表包含关键字段列
        for col in ("薪资", "年限", "学历", "距家", "紧急", "浏览", "招呼", "规模"):
            assert col in content
        # 规则引擎版建议（无 LLM）
        assert "规则引擎" in content

    def test_report_excludes_outsourcing(self, demo_ready):
        config, config_dir, db = demo_ready
        summary = run_daily(config_dir, skip_scrape=True, config=config, db=db)
        content = open(summary["report"], encoding="utf-8").read()
        assert "外包" not in content  # demo 数据中的外包岗位应被硬过滤
