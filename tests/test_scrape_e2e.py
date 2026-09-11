"""浏览器级端到端：本地 mock 招聘站 → 真实 Chromium 抓取 → 解析入库 → 匹配 → 日报。

覆盖此前没有自动化覆盖的完整抓取路径（BrowserSession → extract → parse → store）。
"""

import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

import job_agent.sites as sites_pkg
from job_agent.sites.base import BaseSite, abs_url, attr_of, page_text, text_of
from job_agent.scheduler import run_daily
from job_agent.normalize import parse_count

MOCK_PORT = [0]

SEARCH_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<div class="total">共 42 个职位</div>
<ul id="jobs">
  <li class="job-card">
    <a class="jname" href="/job/1.html">Python 后端工程师【急聘】</a>
    <span class="sal">25-40K·15薪</span>
    <span class="tags"><span>3-5年</span><span>本科</span><span>苏州·工业园区</span></span>
    <a class="cname" href="/company/1">某某云科技</a>
    <span class="csize">1000-9999人</span>
  </li>
  <li class="job-card">
    <a class="jname" href="/job/2.html">高级 Python 工程师</a>
    <span class="sal">30-50K</span>
    <span class="tags"><span>5-10年</span><span>本科</span><span>苏州·昆山市</span></span>
    <a class="cname" href="/company/2">大数智能</a>
    <span class="csize">500-999人</span>
  </li>
  <li class="job-card">
    <a class="jname" href="/job/3.html">外包 Python 开发（驻场）</a>
    <span class="sal">15-20K</span>
    <span class="tags"><span>1-3年</span><span>大专</span><span>苏州·姑苏区</span></span>
    <a class="cname" href="/company/3">软通某某</a>
    <span class="csize">10000人以上</span>
  </li>
  <li class="job-card">
    <a class="jname" href="/job/4.html">算法实习生</a>
    <span class="sal">300-500元/天</span>
    <span class="tags"><span>在校生</span><span>大专</span><span>上海·浦东新区</span></span>
    <a class="cname" href="/company/4">深蓝实验室</a>
    <span class="csize">150-500人</span>
  </li>
</ul></body></html>"""

DETAIL_TMPL = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<h1>{title}</h1>
<div class="jd-resp">{resp}</div>
<div class="jd-req">{req}</div>
</body></html>"""

DETAILS = {
    1: ("Python 后端工程师", "1、负责后端服务的设计与开发，基于 FastAPI 构建接口\n2、参与 Kafka 消息链路维护",
        "熟悉 MySQL 与 Redis，了解 Docker 部署，具备高并发场景经验"),
    2: ("高级 Python 工程师", "负责核心平台架构设计与性能优化",
        "精通 Python 与 Kubernetes，有微服务与分布式事务经验"),
    3: ("外包 Python 开发", "按甲方需求完成开发任务", "会 Python 即可"),
    4: ("算法实习生", "辅助数据清洗与模型训练", "了解 Python 基础"),
}


class MockSite(BaseSite):
    site_id = "mock"
    name = "Mock招聘"
    CARD = "li.job-card"
    SEL = {
        "title": ".jname", "salary": ".sal", "tags": ".tags span",
        "company": ".cname", "company_size": ".csize",
    }
    DETAIL_RESP = ".jd-resp"
    DETAIL_REQ = ".jd-req"

    def __init__(self, home_city=None):
        super().__init__(home_city)
        self.base_url = f"http://127.0.0.1:{MOCK_PORT[0]}"
        self.login_url = self.base_url + "/login.html"
        self.search_url = self.base_url + "/search.html?kw={{kw}}&city={{city}}"

    def logged_in(self, page) -> bool:
        return True

    def extract(self, page, keyword: str, max_cards: int):
        page.goto(self.search_link(keyword), wait_until="domcontentloaded")
        page.wait_for_timeout(300)
        cards = page.query_selector_all(self.CARD)[:max_cards]
        out = []
        for c in cards:
            tags = c.query_selector_all(self.SEL["tags"]) or []
            out.append({
                "title": text_of(c, self.SEL["title"]),
                "url": abs_url(attr_of(c, "a.jname", "href"), self.base_url),
                "salary": text_of(c, self.SEL["salary"]),
                "experience": tags[0].inner_text().strip() if tags else None,
                "education": tags[1].inner_text().strip() if len(tags) > 1 else None,
                "city_text": tags[2].inner_text().strip() if len(tags) > 2 else None,
                "company": text_of(c, self.SEL["company"]),
                "company_size": text_of(c, self.SEL["company_size"]),
            })
        return out, parse_count(page_text(page, ".total"))


@pytest.fixture
def mock_server(tmp_path):
    root = tmp_path / "site"
    (root / "job").mkdir(parents=True)
    (root / "search.html").write_text(SEARCH_HTML, encoding="utf-8")
    for i, (title, resp, req) in DETAILS.items():
        (root / "job" / f"{i}.html").write_text(
            DETAIL_TMPL.format(title=title, resp=resp, req=req), encoding="utf-8")

    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Quiet, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    MOCK_PORT[0] = server.server_address[1]
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.fixture
def scraped(db, config, config_dir, mock_server, monkeypatch):
    """真实浏览器抓取 mock 站点入库，返回 (db, config, config_dir)。"""
    config = dict(config)
    config["search"]["keywords"] = ["Python"]
    config["search"]["max_per_keyword"] = 10
    config["search"]["fetch_detail"] = True
    config["browser"]["delay"] = [0, 0]
    config["sites"] = ["mock"]
    monkeypatch.setitem(sites_pkg.REGISTRY, "mock", MockSite)
    monkeypatch.setattr(sites_pkg, "human_delay", lambda *a, **k: None)
    from job_agent.sites import scrape_all
    stats = scrape_all(config, config_dir, db)
    assert stats["errors"] == [], stats["errors"]
    return db, config, config_dir


class TestScrapeE2E:
    def test_scraped_into_db(self, scraped):
        db, config, config_dir = scraped
        rows = db.query("SELECT * FROM jobs ORDER BY title")
        assert len(rows) == 4
        titles = {r["title"] for r in rows}
        assert "Python 后端工程师【急聘】" in titles
        # 详情页职责/要求已补全
        row = [r for r in rows if "急聘" in r["title"]][0]
        import json
        assert json.loads(row["responsibilities"])[0].startswith("负责后端服务")
        assert "FastAPI" in json.loads(row["skills"])
        # 关键词总数与运行日志
        assert db.query("SELECT total_count FROM keywords_daily")[0]["total_count"] == 42
        assert db.query("SELECT status FROM runs")[0]["status"] == "ok"

    def test_normalized_fields(self, scraped):
        db, config, config_dir = scraped
        row = db.query("SELECT * FROM jobs WHERE title LIKE '%急聘%'")[0]
        assert (row["salary_min"], row["salary_max"]) == (25.0, 40.0)
        assert (row["experience_min"], row["experience_max"]) == (3.0, 5.0)
        assert row["education"] == "本科"
        assert (row["city"], row["district"]) == ("苏州", "工业园区")
        assert row["urgency"] == "high"
        assert row["company_size"] == "1000-9999人"

    def test_daily_salary_conversion(self, scraped):
        db, config, config_dir = scraped
        row = db.query("SELECT * FROM jobs WHERE title = '算法实习生'")[0]
        assert abs(row["salary_min"] - 6.5) < 0.1   # 300-500元/天 → 月薪 K
        assert abs(row["salary_max"] - 10.9) < 0.1
        assert row["city"] == "上海"

    def test_distance_computed(self, scraped):
        db, config, config_dir = scraped
        rows = db.query("SELECT * FROM jobs WHERE title LIKE '%急聘%'")[0]
        import json
        daily = db.query("SELECT distance_km FROM job_daily")[0]
        assert daily["distance_km"] is not None and daily["distance_km"] >= 0

    def test_full_pipeline_report(self, scraped):
        db, config, config_dir = scraped
        summary = run_daily(config_dir, skip_scrape=True, config=config, db=db)
        content = open(summary["report"], encoding="utf-8").read()
        # 匹配岗位出现在报告
        assert "Python 后端工程师" in content
        assert "高级 Python 工程师" in content
        # 外包被硬过滤、异地+低薪被淘汰
        assert "外包 Python" not in content
        assert "算法实习生" not in content
