import hashlib
from datetime import date, timedelta

import pytest

from job_agent.db import DB, row_to_job
from job_agent.models import Job


def make_job(idx=1, **kw) -> Job:
    defaults = dict(site="boss", title=f"Java 工程师 {idx}", company="测试公司",
                    url=f"https://x/job/{idx}", salary_min=20.0, salary_max=30.0,
                    views=100, greets=10, skills=["Java", "Redis"])
    defaults.update(kw)
    return Job(**defaults)


def upsert(db: DB, job: Job, day: str, dist=12.5):
    db.upsert_job(job, dist, day)


class TestUpsert:
    def test_idempotent(self, db):
        day = date.today().isoformat()
        job = make_job()
        upsert(db, job, day)
        upsert(db, job, day)
        rows = db.query("SELECT * FROM jobs")
        assert len(rows) == 1
        daily = db.query("SELECT * FROM job_daily")
        assert len(daily) == 1 and daily[0]["status"] == "active"

    def test_first_seen_kept(self, db):
        d1 = (date.today() - timedelta(days=1)).isoformat()
        d2 = date.today().isoformat()
        upsert(db, make_job(), d1)
        upsert(db, make_job(views=200), d2)
        row = db.query("SELECT * FROM jobs")[0]
        assert row["first_seen"] == d1 and row["last_seen"] == d2
        # 今日快照记录了新 views
        today_row = db.query(
            "SELECT * FROM job_daily WHERE date = ?", (d2,))[0]
        assert today_row["views"] == 200

    def test_distance_persisted(self, db):
        upsert(db, make_job(), date.today().isoformat(), dist=7.7)
        row = db.query("SELECT * FROM job_daily")[0]
        assert row["distance_km"] == 7.7


class TestMarkMissing:
    def test_missing_marked(self, db):
        d1 = (date.today() - timedelta(days=1)).isoformat()
        d2 = date.today().isoformat()
        upsert(db, make_job(1), d1)
        upsert(db, make_job(2, url="https://x/job/2"), d1)
        # 第二天只见到 job1
        upsert(db, make_job(1), d2)
        n = db.mark_missing(d2)
        assert n == 1
        status = db.query(
            "SELECT status FROM job_daily WHERE date=? AND job_id=?",
            (d2, make_job(2).job_id))[0]["status"]
        assert status == "missing"


class TestRoundTrip:
    def test_row_to_job(self, db):
        day = date.today().isoformat()
        job = make_job(responsibilities=["做事情"], requirements_extra=["会 Java"],
                       raw={"extra_key": 1})
        upsert(db, job, day)
        row = db.query("SELECT * FROM jobs")[0]
        job2 = row_to_job(row)
        assert job2.title == job.title
        assert job2.responsibilities == ["做事情"]
        assert job2.requirements_extra == ["会 Java"]
        assert job2.raw["extra_key"] == 1
        assert job2.skills == ["Java", "Redis"]

    def test_record_keyword_and_run(self, db):
        day = date.today().isoformat()
        db.record_keyword(day, "Java", "boss", 300)
        db.record_keyword(day, "Java", "boss", 320)
        db.record_run(day, "boss", "Java", 10, "ok")
        assert db.query("SELECT total_count FROM keywords_daily")[0]["total_count"] == 320
        assert db.query("SELECT * FROM runs")[0]["count"] == 10
