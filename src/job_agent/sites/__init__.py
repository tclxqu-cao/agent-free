"""站点注册表 + 抓取编排（scrape → normalize → distance → store）。"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from ..browser import BrowserSession, human_delay
from ..db import DB
from ..distance import Geocoder, compute_distance
from ..models import data_dir
from .base import BaseSite
from .boss import BossSite
from .job51 import Job51Site
from .lagou import LagouSite
from .liepin import LiepinSite
from .zhilian import ZhilianSite

log = logging.getLogger(__name__)

REGISTRY: dict[str, type[BaseSite]] = {
    "boss": BossSite,
    "zhilian": ZhilianSite,
    "liepin": LiepinSite,
    "job51": Job51Site,
    "lagou": LagouSite,
}


def get_site(site_id: str, home_city: str | None = None) -> BaseSite:
    if site_id not in REGISTRY:
        raise KeyError(f"未知站点: {site_id}，可用: {list(REGISTRY)}")
    return REGISTRY[site_id](home_city=home_city)


def dump_page(config: dict, config_path: Path, site_id: str, keyword: str, page) -> Path:
    """调试：保存页面 HTML 与截图到 data/dumps/。"""
    base = data_dir(config, config_path) / "dumps"
    base.mkdir(parents=True, exist_ok=True)
    name = f"{date.today().isoformat()}_{site_id}_{keyword.replace(' ', '_')}"
    html = base / f"{name}.html"
    try:
        html.write_text(page.content(), encoding="utf-8")
    except Exception:
        log.exception("抓取页面留证失败：站点=%s，关键词=%s", site_id, keyword)
    return html


def scrape_site_keyword(site: BaseSite, page, keyword: str, max_cards: int,
                        fetch_detail: bool, db: DB, geocoder: Geocoder,
                        home: dict, today: str, config: dict,
                        config_path: Path) -> int:
    """单站点单关键词抓取入库，返回入库岗位数。异常向上抛。

    extract 首次异常（如页面跳转竞态）自动重试一次；
    0 结果时 dump 页面留证，便于自主运行期间排查选择器漂移。
    """
    if not site.logged_in(page):
        raise RuntimeError("登录态失效，请先执行: job-agent login --site " + site.site_id)
    try:
        cards, total = site.extract(page, keyword, max_cards)
    except Exception:
        log.exception("岗位列表提取失败，稍后重试：站点=%s，关键词=%s",
                      site.site_id, keyword)
        human_delay(config, 3, 6)
        cards, total = site.extract(page, keyword, max_cards)
    db.record_keyword(today, keyword, site.site_id, total)
    if not cards:
        log.warning("岗位列表为空，保存页面留证：站点=%s，关键词=%s",
                    site.site_id, keyword)
        dump_page(config, config_path, site.site_id, keyword, page)
    count = 0
    for d in cards:
        job = site.parse_card_dict(d)
        if not job.title or not job.url:
            continue
        if fetch_detail:
            try:
                job = site.detail(page, job)
            except Exception:
                log.exception("岗位详情抓取失败，保留列表信息：站点=%s，关键词=%s",
                              site.site_id, keyword)
            human_delay(config, 1, 2)
        dist = compute_distance(job, geocoder, home)
        db.upsert_job(job, dist, today)
        count += 1
    return count


def scrape_all(config: dict, config_path: Path, db: DB,
               sites: list[str] | None = None) -> dict:
    """完整抓取 workflow：遍历启用站点 × 关键词；单站点失败不影响其余。"""
    today = date.today().isoformat()
    home = config.get("home") or {}
    scfg = config.get("search") or {}
    keywords = scfg.get("keywords") or []
    max_cards = int(scfg.get("max_per_keyword", 30))
    fetch_detail = bool(scfg.get("fetch_detail", True))
    geocoder = Geocoder(db, amap_key=config.get("amap_key") or "",
                        home_city=home.get("city"))
    stats: dict = {"date": today, "total_jobs": 0, "runs": [], "errors": []}
    enabled = sites or config.get("sites") or list(REGISTRY)

    log.info("岗位抓取开始：站点 %s 个，关键词 %s 个", len(enabled), len(keywords))
    with BrowserSession(config, config_path) as bs:
        for site_id in enabled:
            log.info("招聘站点开始：%s", site_id)
            site = get_site(site_id, home.get("city"))
            page = bs.open(site_id)
            for kw in keywords:
                log.info("岗位关键词抓取开始：站点=%s，关键词=%s", site_id, kw)
                try:
                    count = scrape_site_keyword(
                        site, page, kw, max_cards, fetch_detail,
                        db, geocoder, home, today, config, config_path)
                    db.record_run(today, site_id, kw, count, "ok")
                    stats["runs"].append({"site": site_id, "keyword": kw, "count": count})
                    stats["total_jobs"] += count
                    log.info("岗位关键词抓取完成：站点=%s，关键词=%s，入库 %s 条",
                             site_id, kw, count)
                except Exception as e:
                    log.exception("岗位关键词抓取失败：站点=%s，关键词=%s", site_id, kw)
                    db.record_run(today, site_id, kw, 0, "error", str(e)[:300])
                    stats["errors"].append({"site": site_id, "keyword": kw, "error": str(e)})
                    try:
                        dump_page(config, config_path, site_id, kw, page)
                    except Exception:
                        log.exception("抓取失败后的页面留证失败：站点=%s，关键词=%s",
                                      site_id, kw)
                human_delay(config)
            log.info("招聘站点结束：%s", site_id)
        try:
            db.mark_missing(today)
        except Exception:
            log.exception("更新失效岗位标记失败")
    log.info("岗位抓取完成：入库 %s 条，失败 %s 项",
             stats["total_jobs"], len(stats["errors"]))
    return stats
