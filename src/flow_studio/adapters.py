"""job_agent 能力适配：把应聘 agent 的核心动作注册进 AgentRegistry。

job_agent 不可导入（依赖缺失）时不注册，编排画布其余功能不受影响。
所有动作入参 / 出参均为 JSON 兼容 dict，供引擎与画布直接消费。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .registry import AgentRegistry

log = logging.getLogger("flow_studio")


def register_job_agent(registry: AgentRegistry, config_dir: Path) -> bool:
    """注册 job_agent 全部能力；成功返回 True，依赖缺失返回 False。"""
    try:
        from job_agent import analysis as ja_analysis
        from job_agent.db import DB
        from job_agent.demo import seed_demo_data
        from job_agent.models import data_dir, load_config, load_profile
        from job_agent.scheduler import run_daily, today_jobs
        from job_agent.match import filter_and_score
    except ImportError as e:  # pragma: no cover - 环境缺依赖时
        log.warning("job_agent 不可用，相关 agent 节点未注册：%s", e)
        return False

    config_dir = Path(config_dir)

    def _ctx(args: dict):
        config = load_config(config_dir)
        profile = load_profile(config_dir)
        db = DB(data_dir(config, config_dir) / "job_agent.db")
        return config, profile, db

    def _job_view(m) -> dict:
        return {
            "title": m.job.title, "company": m.job.company,
            "salary_text": m.job.salary_text,
            "experience_text": m.job.experience_text, "education": m.job.education,
            "city": m.job.city, "district": m.job.district,
            "distance_km": m.job.raw.get("distance_km"),
            "urgency": m.job.urgency, "views": m.job.views, "greets": m.job.greets,
            "company_size": m.job.company_size, "score": round(m.score, 1),
            "passed": m.passed, "reasons": m.reasons[:5],
            "skills": m.job.skills[:10], "url": m.job.url,
        }

    def _jobs_text(matches: list) -> str:
        lines = []
        for m in matches[:20]:
            dist = m.job.raw.get("distance_km")
            lines.append(
                f"- {m.job.title} @ {m.job.company} | {m.job.salary_text or '薪资面议'}"
                f" | {m.job.education or '学历不限'}"
                f" | 距家{f'{dist:.0f}km' if dist is not None else '?'}"
                f" | 评分 {m.score:.0f} | {('；'.join(m.reasons[:2])) if m.reasons else ''}")
        return "\n".join(lines)

    def demo_seed(args: dict) -> dict:
        config, _, db = _ctx(args)
        if args.get("reset", True):
            db_path = db.path
            db.close()
            db_path.unlink(missing_ok=True)
            db = DB(db_path)
        days = int(args.get("days") or 10)
        n = seed_demo_data(db, config, days=days)
        return {"seeded_snapshots": n, "days": days,
                "text": f"已生成 {days} 天确定性模拟数据（{n} 条岗位快照）"}

    def match_today(args: dict) -> dict:
        config, profile, db = _ctx(args)
        import datetime as dt

        day = args.get("day") or dt.date.today().isoformat()
        jobs = today_jobs(db, day)
        matches = filter_and_score(jobs, config.get("rules") or {}, profile)
        passed = [m for m in matches if m.passed]
        return {"day": day, "total": len(jobs), "passed_count": len(passed),
                "jobs": [_job_view(m) for m in passed[:20]],
                "text": _jobs_text(passed) or "（无命中岗位）"}

    def daily(args: dict) -> dict:
        summary = run_daily(config_dir, skip_scrape=bool(args.get("skip_scrape")))
        errors = summary.get("errors") or []
        err_text = "".join(f"\n  ⚠️ {e.get('site')}「{e.get('keyword')}」：{e.get('error')}"
                           for e in errors)
        return {"date": summary["date"], "jobs": summary["jobs"],
                "passed": summary["passed"], "report": summary["report"],
                "llm_used": summary["llm_used"], "errors": errors,
                "text": f"{summary['date']} 在招 {summary['jobs']} 个、命中 "
                        f"{summary['passed']} 个（建议来源："
                        f"{'LLM' if summary['llm_used'] else '规则引擎'}）\n"
                        f"日报：{summary['report']}{err_text}"}

    def scrape(args: dict) -> dict:
        config, _, db = _ctx(args)
        from job_agent.sites import scrape_all

        stats = scrape_all(config, config_dir, db,
                           sites=[args["site"]] if args.get("site") else None)
        return {"total_jobs": stats.get("total_jobs"), "errors": stats.get("errors", [])}

    def analyze(args: dict) -> dict:
        config, _, db = _ctx(args)
        days = int(args.get("days") or 30)
        drift = ja_analysis.requirement_drift(db)
        count = ja_analysis.count_trend(db, days)[-7:]
        salary = ja_analysis.salary_trend(db, days)[-7:]
        top = drift["skills"][:8]
        text = "近 7 天：" + "；".join(
            f"{c['date']} 活跃 {c['active']}" for c in count[-3:])
        if len(salary) >= 2:
            text += (f"｜平均月薪 {salary[0]['avg_min']}-{salary[0]['avg_max']}K → "
                     f"{salary[-1]['avg_min']}-{salary[-1]['avg_max']}K")
        if top:
            text += "｜技能热度：" + "、".join(s["skill"] for s in top[:6])
        return {
            "days": days,
            "count_trend": ja_analysis.count_trend(db, days)[-14:],
            "salary_trend": salary,
            "top_skills": top,
            "hot_jobs": ja_analysis.hot_jobs(db)[:5],
            "text": text,
        }

    def report(args: dict) -> dict:
        import datetime as dt

        from job_agent.advisor import advise
        from job_agent.report import write_daily_report

        config, profile, db = _ctx(args)
        day = args.get("date") or dt.date.today().isoformat()
        jobs = today_jobs(db, day)
        matches = filter_and_score(jobs, config.get("rules") or {}, profile)
        advisor = advise(db, config, config_dir, profile, matches)
        path = write_daily_report(config_dir.parent / "output" / day, day, matches,
                                  advisor, db=db)
        return {"report": str(path), "jobs": len(jobs),
                "passed": sum(1 for m in matches if m.passed),
                "text": f"日报已生成：{path}（岗位 {len(jobs)} / 命中 "
                        f"{sum(1 for m in matches if m.passed)}）"}

    def login_status(args: dict) -> dict:
        from job_agent.browser import BrowserSession
        from job_agent.sites import REGISTRY, get_site

        config, _, _ = _ctx(args)
        enabled = [args["site"]] if args.get("site") else (config.get("sites") or list(REGISTRY))
        out = []
        with BrowserSession(config, config_dir) as bs:
            for site_id in enabled:
                out.append({"site": site_id,
                            "has_auth": bs.has_auth(site_id)})
        return {"sites": out}

    registry.register("job_agent", "demo_seed", demo_seed,
                      "生成 N 天确定性模拟数据（无需招聘网站凭据，演示/验证用）",
                      [{"key": "days", "type": "number", "required": False,
                        "description": "生成天数，默认 10"},
                       {"key": "reset", "type": "bool", "required": False,
                        "description": "是否清库重建，默认 true"}])
    registry.register("job_agent", "match_today", match_today,
                      "按个人规则匹配今日岗位（硬过滤 + 0-100 评分），返回列表",
                      [{"key": "day", "type": "string", "required": False,
                        "description": "YYYY-MM-DD，默认今天"}])
    registry.register("job_agent", "daily", daily,
                      "每日全流程：抓取→匹配→建议→日报（skip_scrape 可只看库内数据）",
                      [{"key": "skip_scrape", "type": "bool", "required": False,
                        "description": "跳过抓取，默认 false"}])
    registry.register("job_agent", "scrape", scrape,
                      "对启用站点抓取一次并入库（需登录态）",
                      [{"key": "site", "type": "string", "required": False,
                        "description": "只抓该站点，默认全部启用站点"}])
    registry.register("job_agent", "analyze", analyze,
                      "岗位数量 / 薪资 / 技能热度趋势与热度增量",
                      [{"key": "days", "type": "number", "required": False,
                        "description": "回看天数，默认 30"}])
    registry.register("job_agent", "report", report,
                      "从库内数据生成指定日期 Markdown 日报",
                      [{"key": "date", "type": "string", "required": False,
                        "description": "YYYY-MM-DD，默认今天"}])
    registry.register("job_agent", "login_status", login_status,
                      "检查各招聘站点登录态")
    return True
