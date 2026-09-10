"""每日调度：watch 循环 + 完整 daily workflow。"""

from __future__ import annotations

import datetime as dt
import logging
import time
from pathlib import Path

from .advisor import advise
from .analysis import count_trend
from .db import DB, row_to_job
from .match import filter_and_score
from .models import data_dir, load_config, load_profile
from .report import write_daily_report

log = logging.getLogger("job_agent")


def today_jobs(db: DB, day: str) -> list:
    rows = db.query("SELECT * FROM jobs WHERE last_seen = ?", (day,))
    jobs = []
    for r in rows:
        job = row_to_job(r)
        snap = db.query(
            "SELECT distance_km, views, greets FROM job_daily WHERE date=? AND job_id=?",
            (day, job.job_id))
        if snap:
            s = snap[0]
            if s["distance_km"] is not None:
                job.raw.setdefault("distance_km", s["distance_km"])
            if job.views is None and s["views"] is not None:
                job.views = s["views"]
            if job.greets is None and s["greets"] is not None:
                job.greets = s["greets"]
        jobs.append(job)
    return jobs


def run_daily(config_dir: Path, skip_scrape: bool = False,
              config: dict | None = None, db: DB | None = None) -> dict:
    """完整 workflow：scrape(可选) → match → analyze → advise → report。"""
    import datetime as _dt

    config_dir = Path(config_dir)
    config = config or load_config(config_dir)
    profile = load_profile(config_dir)
    db = db or DB(data_dir(config, config_dir) / "job_agent.db")
    day = _dt.date.today().isoformat()
    out_root = config_dir.parent / "output"

    run_stats = {"date": day, "total_jobs": 0, "runs": [], "errors": []}
    if not skip_scrape:
        from .sites import scrape_all
        run_stats = scrape_all(config, config_dir, db)
    else:
        db.mark_missing(day)

    jobs = today_jobs(db, day)
    matches = filter_and_score(jobs, config.get("rules") or {}, profile)
    advisor = advise(db, config, config_dir, profile, matches)
    report_path = write_daily_report(out_root / day, day, matches, advisor,
                                     run_stats, db=db, config=config)
    passed = sum(1 for m in matches if m.passed)
    summary = {"date": day, "jobs": len(jobs), "passed": passed,
               "report": str(report_path), "llm_used": advisor.llm_used,
               "errors": run_stats.get("errors", [])}
    log.info("daily 完成：%s", summary)
    return summary


def run_watch(config_dir: Path) -> None:
    """每日定时循环（到 config.schedule.at 执行 run_daily）。Ctrl-C 退出。"""
    at = (load_config(Path(config_dir)).get("schedule") or {}).get("at", "08:30")
    log.info("watch 启动，每日 %s 执行（也可用 launchd/crontab 代替，见 README）", at)
    while True:
        now = dt.datetime.now()
        h, m = map(int, at.split(":"))
        nxt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        wait = (nxt - now).total_seconds()
        log.info("距下次执行 %.0f 分钟", wait / 60)
        try:
            time.sleep(wait)
        except KeyboardInterrupt:
            raise
        try:
            run_daily(config_dir)
        except Exception as e:
            log.exception("daily 执行失败：%s", e)
