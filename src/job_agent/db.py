"""SQLite 存储层：岗位表 + 逐日快照 + 关键词趋势 + 运行日志 + geocode 缓存。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import Job

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    site TEXT NOT NULL,
    title TEXT,
    company TEXT,
    company_size TEXT,
    industry TEXT,
    salary_text TEXT,
    salary_min REAL,
    salary_max REAL,
    city TEXT,
    district TEXT,
    address TEXT,
    experience_text TEXT,
    experience_min REAL,
    experience_max REAL,
    education TEXT,
    responsibilities TEXT,
    requirements_extra TEXT,
    skills TEXT,
    urgency TEXT,
    url TEXT,
    first_seen TEXT,
    last_seen TEXT,
    extra TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_site ON jobs(site);
CREATE TABLE IF NOT EXISTS job_daily (
    date TEXT NOT NULL,
    job_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    salary_min REAL,
    salary_max REAL,
    views INTEGER,
    greets INTEGER,
    distance_km REAL,
    education TEXT,
    experience_min REAL,
    skills TEXT,
    PRIMARY KEY (date, job_id)
);
CREATE TABLE IF NOT EXISTS keywords_daily (
    date TEXT NOT NULL,
    keyword TEXT NOT NULL,
    site TEXT NOT NULL,
    total_count INTEGER,
    PRIMARY KEY (date, keyword, site)
);
CREATE TABLE IF NOT EXISTS runs (
    ts TEXT,
    date TEXT,
    site TEXT,
    keyword TEXT,
    count INTEGER,
    status TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS geocode_cache (
    address TEXT PRIMARY KEY,
    lat REAL,
    lng REAL
);
"""


def _tolist(value) -> str:
    return json.dumps(value or [], ensure_ascii=False)


def row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        site=row["site"],
        title=row["title"] or "",
        company=row["company"] or "",
        url=row["url"] or "",
        job_id=row["job_id"],
        company_size=row["company_size"],
        industry=row["industry"],
        salary_text=row["salary_text"],
        salary_min=row["salary_min"],
        salary_max=row["salary_max"],
        city=row["city"],
        district=row["district"],
        address=row["address"],
        experience_text=row["experience_text"],
        experience_min=row["experience_min"],
        experience_max=row["experience_max"],
        education=row["education"],
        responsibilities=json.loads(row["responsibilities"] or "[]"),
        requirements_extra=json.loads(row["requirements_extra"] or "[]"),
        skills=json.loads(row["skills"] or "[]"),
        urgency=row["urgency"],
        raw=json.loads(row["extra"] or "{}"),
    )


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """旧库平滑加列。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(job_daily)")}
        for name, typ in (("education", "TEXT"), ("experience_min", "REAL"),
                          ("skills", "TEXT")):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE job_daily ADD COLUMN {name} {typ}")

    def close(self) -> None:
        self.conn.close()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    # ---------- 写入 ----------

    def upsert_job(self, job: Job, distance_km: float | None, date: str) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO jobs (job_id, site, title, company, company_size, industry,
                     salary_text, salary_min, salary_max, city, district, address,
                     experience_text, experience_min, experience_max, education,
                     responsibilities, requirements_extra, skills, urgency, url,
                     first_seen, last_seen, extra)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(job_id) DO UPDATE SET
                     title=excluded.title, company=excluded.company,
                     company_size=excluded.company_size, industry=excluded.industry,
                     salary_text=excluded.salary_text, salary_min=excluded.salary_min,
                     salary_max=excluded.salary_max, city=excluded.city,
                     district=excluded.district, address=excluded.address,
                     experience_text=excluded.experience_text,
                     experience_min=excluded.experience_min,
                     experience_max=excluded.experience_max,
                     education=excluded.education,
                     responsibilities=excluded.responsibilities,
                     requirements_extra=excluded.requirements_extra,
                     skills=excluded.skills, urgency=excluded.urgency,
                     last_seen=excluded.last_seen, extra=excluded.extra""",
                (
                    job.job_id, job.site, job.title, job.company, job.company_size,
                    job.industry, job.salary_text, job.salary_min, job.salary_max,
                    job.city, job.district, job.address, job.experience_text,
                    job.experience_min, job.experience_max, job.education,
                    _tolist(job.responsibilities), _tolist(job.requirements_extra),
                    _tolist(job.skills), job.urgency, job.url, date, date,
                    json.dumps(job.raw, ensure_ascii=False),
                ),
            )
            self.conn.execute(
                """INSERT INTO job_daily (date, job_id, status, salary_min, salary_max,
                     views, greets, distance_km, education, experience_min, skills)
                   VALUES (?,?, 'active', ?,?,?,?,?,?,?,?)
                   ON CONFLICT(date, job_id) DO UPDATE SET
                     salary_min=excluded.salary_min, salary_max=excluded.salary_max,
                     views=excluded.views, greets=excluded.greets,
                     distance_km=excluded.distance_km, status='active',
                     education=excluded.education,
                     experience_min=excluded.experience_min, skills=excluded.skills""",
                (date, job.job_id, job.salary_min, job.salary_max,
                 job.views, job.greets, distance_km,
                 job.education, job.experience_min, _tolist(job.skills)),
            )

    def record_keyword(self, date: str, keyword: str, site: str, total_count: int | None) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO keywords_daily (date, keyword, site, total_count)
                   VALUES (?,?,?,?)
                   ON CONFLICT(date, keyword, site) DO UPDATE SET total_count=excluded.total_count""",
                (date, keyword, site, total_count),
            )

    def record_run(self, date: str, site: str, keyword: str, count: int,
                   status: str, error: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO runs (ts, date, site, keyword, count, status, error) "
                "VALUES (datetime('now','localtime'), ?,?,?,?,?,?)",
                (date, site, keyword, count, status, error),
            )

    def mark_missing(self, date: str) -> int:
        """首次消失检测：最后快照为 active 且其后再无任何记录的历史岗位，
        今天记 missing（每个消失周期只标一次，重新抓到后再消失会再次标记）。"""
        with self.conn:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO job_daily (date, job_id, status)
                   SELECT ?, job_id, 'missing' FROM jobs j
                   WHERE j.last_seen < ?
                     AND NOT EXISTS (
                         SELECT 1 FROM job_daily d
                         WHERE d.job_id = j.job_id AND d.date > j.last_seen)""",
                (date, date),
            )
            return cur.rowcount

    # ---------- geocode 缓存 ----------

    def geocode_get(self, address: str) -> tuple[float, float] | None:
        rows = self.query("SELECT lat, lng FROM geocode_cache WHERE address = ?", (address,))
        if rows and rows[0]["lat"] is not None:
            return rows[0]["lat"], rows[0]["lng"]
        return None

    def geocode_put(self, address: str, lat: float, lng: float) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO geocode_cache (address, lat, lng) VALUES (?,?,?)",
                (address, lat, lng),
            )
