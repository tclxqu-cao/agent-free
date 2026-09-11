"""CLI 入口：job-agent {init,login,login-status,scrape,analyze,report,daily,watch,demo}"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from .models import load_config, load_profile

log = logging.getLogger("job_agent")

SITES_HELP = "站点: boss / zhilian / liepin / job51 / lagou"


def _default_config_dir() -> Path:
    return Path("config")


def _ensure_config(config_dir: Path) -> None:
    if not (config_dir / "config.yaml").exists():
        sys.exit(f"缺少 {config_dir}/config.yaml，先执行: job-agent init")


def cmd_init(args) -> None:
    config_dir = Path(args.config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    pkg_examples = Path(__file__).resolve().parent.parent.parent / "config"
    for name in ("config.example.yaml", "profile.example.yaml"):
        src = pkg_examples / name
        dst = config_dir / name.replace(".example", "")
        if dst.exists():
            print(f"[init] 已存在，跳过: {dst}")
        elif src.exists():
            shutil.copy(src, dst)
            print(f"[init] 已生成: {dst}")
        else:
            sys.exit(f"[init] 找不到示例配置 {src}")
    print("[init] 完成。下一步：\n"
          "  1. 编辑 config/config.yaml（家的坐标、关键词、规则、LLM）\n"
          "  2. 编辑 config/profile.yaml（你的技能与经历）\n"
          f"  3. 逐站点登录：job-agent login --site boss   （{SITES_HELP}）\n"
          "  4. 跑一次全流程：job-agent daily   （或先用 job-agent demo 体验）")


def cmd_login(args) -> None:
    _ensure_config(Path(args.config_dir))
    from .browser import login_flow
    config = load_config(Path(args.config_dir))
    login_flow(args.site, config, Path(args.config_dir), timeout=args.timeout)
    print("[login] 完成。验证：job-agent login-status")


def cmd_login_status(args) -> None:
    _ensure_config(Path(args.config_dir))
    from .browser import BrowserSession
    from .sites import REGISTRY, get_site
    config = load_config(Path(args.config_dir))
    enabled = args.site and [args.site] or config.get("sites") or list(REGISTRY)
    with BrowserSession(config, Path(args.config_dir)) as bs:
        for site_id in enabled:
            site = get_site(site_id, (config.get("home") or {}).get("city"))
            if not bs.has_auth(site_id):
                print(f"[{site.name}] ❌ 无登录态（执行 login --site {site_id}）")
                continue
            try:
                page = bs.open(site_id)
                page.goto(site.login_url, wait_until="domcontentloaded")
                ok = site.logged_in(page)
                print(f"[{site.name}] {'✅ 登录态有效' if ok else '⚠️ 已失效，请重新 login'}")
            except Exception as e:
                print(f"[{site.name}] ⚠️ 检测失败：{e}")


def cmd_scrape(args) -> None:
    _ensure_config(Path(args.config_dir))
    from .db import DB
    from .models import data_dir
    from .sites import scrape_all
    config_dir = Path(args.config_dir)
    config = load_config(config_dir)
    db = DB(data_dir(config, config_dir) / "job_agent.db")
    stats = scrape_all(config, config_dir, db,
                       sites=[args.site] if args.site else None)
    print(f"[scrape] 完成：入库 {stats['total_jobs']} 条；"
          f"异常 {len(stats['errors'])} 个")
    for e in stats["errors"]:
        print(f"  ⚠️ {e['site']}「{e['keyword']}」：{e['error']}")


def cmd_analyze(args) -> None:
    _ensure_config(Path(args.config_dir))
    from .analysis import (count_trend, distribution_shift, hot_jobs, keyword_trend,
                           requirement_drift, salary_trend)
    from .db import DB
    from .models import data_dir
    config_dir = Path(args.config_dir)
    config = load_config(config_dir)
    db = DB(data_dir(config, config_dir) / "job_agent.db")
    print("== 数量趋势 ==")
    for c in count_trend(db, days=args.days)[-14:]:
        print(f"  {c['date']}  活跃 {c['active']:4d}  新增 {c['new']:3d}  下架 {c['missing']:3d}")
    print("== 薪资趋势 ==")
    for s in salary_trend(db, days=args.days)[-14:]:
        print(f"  {s['date']}  {s['avg_min']}-{s['avg_max']}K")
    print("== 学历分布（本周 vs 上周）==")
    for k, v in distribution_shift(db, "education")["cur"].items():
        print(f"  {k}: {v}%")
    print("== 技能热度 Top10 ==")
    for s in requirement_drift(db)["skills"][:10]:
        print(f"  {s['skill']}: 本周 {s['cur']}  上周 {s['prev']}  ({s['delta']:+})")
    print("== 关键词总数趋势 ==")
    for k in keyword_trend(db, days=args.days):
        series = k["series"]
        if series:
            print(f"  {k['keyword']}: {series[0]['total']} → {series[-1]['total']}")
    print("== 热度增量 Top5 ==")
    for h in hot_jobs(db)[:5]:
        print(f"  {h['title']} @ {h['company']}  浏览+{h['views_delta']}  招呼+{h['greets_delta']}")


def cmd_report(args) -> None:
    _ensure_config(Path(args.config_dir))
    from .advisor import advise
    from .db import DB
    from .models import data_dir
    from .scheduler import today_jobs
    from .match import filter_and_score
    from .report import write_daily_report
    import datetime as dt
    config_dir = Path(args.config_dir)
    config = load_config(config_dir)
    profile = load_profile(config_dir)
    db = DB(data_dir(config, config_dir) / "job_agent.db")
    day = args.date or dt.date.today().isoformat()
    jobs = today_jobs(db, day)
    matches = filter_and_score(jobs, config.get("rules") or {}, profile)
    advisor = advise(db, config, config_dir, profile, matches)
    path = write_daily_report(config_dir.parent / "output" / day, day, matches,
                              advisor, db=db, config=config)
    print(f"[report] 已生成: {path}")


def cmd_daily(args) -> None:
    _ensure_config(Path(args.config_dir))
    from .scheduler import run_daily
    summary = run_daily(Path(args.config_dir), skip_scrape=args.skip_scrape)
    print(f"[daily] {summary['date']} 岗位 {summary['jobs']} 条，匹配 {summary['passed']} 条"
          f"（建议来源：{'LLM' if summary['llm_used'] else '规则引擎'}）")
    print(f"[daily] 日报: {summary['report']}")
    for e in summary["errors"]:
        print(f"  ⚠️ {e['site']}「{e['keyword']}」：{e['error']}")


def cmd_watch(args) -> None:
    _ensure_config(Path(args.config_dir))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from .scheduler import run_watch
    run_watch(Path(args.config_dir))


def cmd_demo(args) -> None:
    from .db import DB
    from .demo import seed_demo_data
    from .models import data_dir
    config_dir = Path(args.config_dir)
    config = load_config(config_dir)
    profile = load_profile(config_dir)
    db_path = data_dir(config, config_dir) / "job_agent.db"
    if db_path.exists() and not args.keep:
        db_path.unlink()
    db = DB(db_path)
    n = seed_demo_data(db, config, days=args.days)
    print(f"[demo] 已生成 {args.days} 天模拟数据（{n} 条岗位快照）")
    from .scheduler import run_daily
    summary = run_daily(config_dir, skip_scrape=True, config=config, db=db)
    print(f"[demo] 匹配 {summary['passed']} 条（建议来源：{'LLM' if summary['llm_used'] else '规则引擎'}）")
    print(f"[demo] 日报: {summary['report']}")


def main(argv: list[str] | None = None) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config-dir", default="config", help="配置目录（默认 ./config）")
    parser = argparse.ArgumentParser(
        prog="job-agent", parents=[common],
        description="智能应聘 Agent：抓取招聘网站岗位 → 趋势分析 → 匹配与学习建议")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str):
        return sub.add_parser(name, parents=[common], help=help_text)

    add("init", "初始化配置文件").set_defaults(func=cmd_init)

    p = add("login", "有头浏览器手动登录某站点并保存登录态")
    p.add_argument("--site", required=True, help=SITES_HELP)
    p.add_argument("--timeout", type=float, default=240,
                   help="等待登录完成的秒数（默认 240）")
    p.set_defaults(func=cmd_login)

    p = add("login-status", "检查各站点登录态")
    p.add_argument("--site", default=None, help="只查该站点")
    p.set_defaults(func=cmd_login_status)

    p = add("scrape", "抓取一次（不生成报告）")
    p.add_argument("--site", default=None, help="只抓该站点")
    p.set_defaults(func=cmd_scrape)

    p = add("analyze", "输出趋势分析")
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(func=cmd_analyze)

    p = add("report", "从已有数据重新生成指定日期报告")
    p.add_argument("--date", default=None, help="YYYY-MM-DD，默认今天")
    p.set_defaults(func=cmd_report)

    p = add("daily", "每日全流程：抓取→分析→建议→报告")
    p.add_argument("--skip-scrape", action="store_true", help="跳过抓取，只用库中数据")
    p.set_defaults(func=cmd_daily)

    add("watch", "常驻定时执行 daily（见 config schedule.at）").set_defaults(func=cmd_watch)

    p = add("demo", "生成模拟数据并跑通全链路（无需网站凭据）")
    p.add_argument("--days", type=int, default=10)
    p.add_argument("--keep", action="store_true", help="保留已有数据库")
    p.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
