"""趋势分析：数量变化、薪资趋势、学历/年限分布漂移、技能热度、岗位热度、新增/消失。"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, timedelta

from .db import DB, row_to_job
from .normalize import extract_skills

WEEK = 7


def _dates(days: int) -> str:
    return (date.today() - timedelta(days=days - 1)).isoformat()


def _week_bounds(days: int) -> tuple[str, str, str, str]:
    """返回 (本周开始, 今天, 上周开始, 上周结束)。本周=最近 7 天。"""
    today = date.today()
    cur_start = (today - timedelta(days=WEEK - 1)).isoformat()
    prev_start = (today - timedelta(days=2 * WEEK - 1)).isoformat()
    prev_end = (today - timedelta(days=WEEK)).isoformat()
    return cur_start, today.isoformat(), prev_start, prev_end


def count_trend(db: DB, days: int = 30) -> list[dict]:
    """每日 {date, active, missing, new}。new = 当日首次出现的岗位数。"""
    since = _dates(days)
    rows = db.query(
        """SELECT d.date,
                  SUM(CASE WHEN d.status='active' THEN 1 ELSE 0 END) AS active,
                  SUM(CASE WHEN d.status='missing' THEN 1 ELSE 0 END) AS missing
           FROM job_daily d WHERE d.date >= ? GROUP BY d.date ORDER BY d.date""", (since,))
    new_rows = db.query(
        """SELECT j.first_seen AS date, COUNT(*) AS n FROM jobs j
           WHERE j.first_seen >= ? GROUP BY j.first_seen ORDER BY j.first_seen""", (since,))
    new_map = {r["date"]: r["n"] for r in new_rows}
    return [
        {"date": r["date"], "active": r["active"] or 0, "missing": r["missing"] or 0,
         "new": new_map.get(r["date"], 0)}
        for r in rows
    ]


def salary_trend(db: DB, days: int = 30) -> list[dict]:
    since = _dates(days)
    rows = db.query(
        """SELECT date, AVG(salary_min) AS amin, AVG(salary_max) AS amax
           FROM job_daily WHERE status='active' AND salary_min IS NOT NULL
           AND date >= ? GROUP BY date ORDER BY date""", (since,))
    return [{"date": r["date"], "avg_min": round(r["amin"], 1),
             "avg_max": round(r["amax"], 1)} for r in rows]


def _exp_bucket(emin) -> str:
    if emin is None:
        return "不限"
    if emin < 1:
        return "应届"
    if emin < 3:
        return "1-3年"
    if emin < 5:
        return "3-5年"
    if emin < 10:
        return "5-10年"
    return "10年以上"


def distribution_shift(db: DB, field: str = "education") -> dict:
    """基于每日快照的周对比分布（每岗位每周只计一次）：本周 vs 上周占比。

    field: education | experience
    """
    cur_start, _today, prev_start, _prev_end = _week_bounds(WEEK)
    rows = db.query(
        """SELECT date, job_id, education, experience_min FROM job_daily
           WHERE status='active' AND date >= ?""", (prev_start,))
    cur: Counter = Counter()
    prev: Counter = Counter()
    seen = {"cur": set(), "prev": set()}
    for r in rows:
        week = "cur" if r["date"] >= cur_start else "prev"
        if r["job_id"] in seen[week]:
            continue
        seen[week].add(r["job_id"])
        if field == "education":
            key = r["education"] or "未知"
        else:
            key = _exp_bucket(r["experience_min"])
        (cur if week == "cur" else prev)[key] += 1

    def pct(c: Counter) -> dict[str, float]:
        total = sum(c.values()) or 1
        return {k: round(v * 100 / total, 1) for k, v in c.items()}

    return {"cur": pct(cur), "prev": pct(prev)}


def skill_trend(db: DB, days: int = 14, top_n: int = 15,
                vocab: list[str] | None = None) -> list[dict]:
    """基于每日快照的技能词频周对比（每岗位每周去重）：正 delta = 需求升温。"""
    cur_start, _today, prev_start, _prev_end = _week_bounds(days)
    rows = db.query(
        "SELECT date, job_id, skills FROM job_daily WHERE status='active' AND date >= ?",
        (prev_start,))
    cur: Counter = Counter()
    prev: Counter = Counter()
    seen = {"cur": set(), "prev": set()}
    for r in rows:
        week = "cur" if r["date"] >= cur_start else "prev"
        if r["job_id"] in seen[week]:
            continue
        seen[week].add(r["job_id"])
        (cur if week == "cur" else prev).update(json.loads(r["skills"] or "[]"))
    merged = set(cur) | set(prev)
    items = [{"skill": s, "cur": cur.get(s, 0), "prev": prev.get(s, 0),
              "delta": cur.get(s, 0) - prev.get(s, 0)} for s in merged]
    items.sort(key=lambda x: (-x["cur"], -x["delta"]))
    return items[:top_n]


def hot_jobs(db: DB, days: int = 7, top_n: int = 10,
             exclude: list[str] | None = None) -> list[dict]:
    """单岗位热度：近 N 天 浏览量/招呼量 增量排序。exclude 命中标题/公司的岗位跳过。"""
    since = _dates(days)
    rows = db.query(
        """SELECT d.job_id, MAX(d.date) AS last_date,
                  MAX(d.views) AS v_last, MIN(d.views) AS v_first,
                  MAX(d.greets) AS g_last, MIN(d.greets) AS g_first
           FROM job_daily d WHERE d.date >= ? AND d.status='active'
           GROUP BY d.job_id""", (since,))
    out = []
    for r in rows:
        v_delta = (r["v_last"] or 0) - (r["v_first"] or 0) if r["v_last"] is not None else None
        g_delta = (r["g_last"] or 0) - (r["g_first"] or 0) if r["g_last"] is not None else None
        if v_delta or g_delta:
            job_rows = db.query(
                "SELECT title, company FROM jobs WHERE job_id = ?", (r["job_id"],))
            title = job_rows[0]["title"] if job_rows else "?"
            company = job_rows[0]["company"] if job_rows else "?"
            if exclude and any(w in f"{title}{company}" for w in exclude):
                continue
            out.append({
                "job_id": r["job_id"],
                "title": title,
                "company": company,
                "views_delta": v_delta, "greets_delta": g_delta,
            })
    out.sort(key=lambda x: -((x["views_delta"] or 0) + 2 * (x["greets_delta"] or 0)))
    return out[:top_n]


def new_and_missing(db: DB, day: str | None = None) -> tuple[list, list]:
    day = day or date.today().isoformat()
    new_rows = db.query("SELECT * FROM jobs WHERE first_seen = ?", (day,))
    missing_rows = db.query(
        """SELECT j.* FROM job_daily d JOIN jobs j ON d.job_id = j.job_id
           WHERE d.date = ? AND d.status = 'missing'""", (day,))
    return [row_to_job(r) for r in new_rows], [row_to_job(r) for r in missing_rows]


def keyword_trend(db: DB, days: int = 30) -> list[dict]:
    """关键词维度的结果总数趋势（用于数量变化分析）。"""
    since = _dates(days)
    rows = db.query(
        """SELECT date, keyword, SUM(total_count) AS total FROM keywords_daily
           WHERE date >= ? GROUP BY date, keyword ORDER BY keyword, date""", (since,))
    by_kw: dict[str, list[dict]] = {}
    for r in rows:
        by_kw.setdefault(r["keyword"], []).append(
            {"date": r["date"], "total": r["total"]})
    return [{"keyword": k, "series": v} for k, v in by_kw.items()]


def requirement_drift(db: DB) -> dict:
    """岗位整体要求变化的汇总（供报告与 LLM 使用）。"""
    return {
        "education": distribution_shift(db, "education"),
        "experience": distribution_shift(db, "experience"),
        "skills": skill_trend(db),
    }


def skills_in_text(text: str, vocab: list[str] | None = None) -> list[str]:
    return extract_skills(text, vocab)
