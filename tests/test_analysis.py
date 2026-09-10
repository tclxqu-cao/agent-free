import pytest

from job_agent.analysis import (count_trend, distribution_shift, hot_jobs,
                                new_and_missing, requirement_drift, salary_trend,
                                skill_trend)
from job_agent.demo import seed_demo_data


@pytest.fixture
def demo_db(db, config):
    """10 天确定性模拟数据。"""
    n = seed_demo_data(db, config, days=10)
    assert n > 0
    return db


class TestWithDemoData:
    def test_count_trend(self, demo_db):
        trend = count_trend(demo_db, days=14)
        assert len(trend) == 10
        assert all(c["active"] > 0 for c in trend)
        # 有岗位中途下架/中途出现 → 出现 missing 或 new 信号
        assert any(c["new"] > 0 for c in trend)

    def test_salary_trend(self, demo_db):
        trend = salary_trend(demo_db, days=14)
        assert len(trend) == 10
        assert all(s["avg_max"] > s["avg_min"] > 0 for s in trend)

    def test_distribution(self, demo_db):
        edu = distribution_shift(demo_db, "education")
        assert sum(edu["cur"].values()) == pytest.approx(100, abs=1)
        exp = distribution_shift(demo_db, "experience")
        assert exp["cur"]

    def test_skill_trend(self, demo_db):
        skills = skill_trend(demo_db)
        assert skills
        top = skills[0]
        assert top["cur"] > 0
        assert {"skill", "cur", "prev", "delta"} <= set(top)

    def test_hot_jobs(self, demo_db):
        hot = hot_jobs(demo_db, days=7)
        assert hot
        assert all(h["views_delta"] > 0 for h in hot)

    def test_new_and_missing_today(self, demo_db):
        new, missing = new_and_missing(demo_db)
        assert isinstance(new, list) and isinstance(missing, list)
        # demo 中 AI 岗位后期才出现 → 今天应有新增
        assert len(new) > 0

    def test_requirement_drift_keys(self, demo_db):
        drift = requirement_drift(demo_db)
        assert {"education", "experience", "skills"} == set(drift)


class TestControlled:
    def test_count_trend_missing(self, db):
        """构造精确场景：job1 连续两天，job2 第一天后消失。"""
        from datetime import date, timedelta
        from job_agent.models import Job
        d1 = (date.today() - timedelta(days=1)).isoformat()
        d2 = date.today().isoformat()
        for day, jobs in ((d1, ("1", "2")), (d2, ("1",))):
            for i in jobs:
                db.upsert_job(Job(site="boss", title=f"T{i}", company="C",
                                  url=f"https://x/{i}"), None, day)
        db.mark_missing(d2)
        trend = {c["date"]: c for c in count_trend(db, days=7)}
        assert trend[d1]["active"] == 2
        assert trend[d2]["active"] == 1 and trend[d2]["missing"] == 1
