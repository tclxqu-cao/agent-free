"""前程无忧（51job，we.51job.com）适配器。"""

from __future__ import annotations

import json

from .base import BaseSite, abs_url, attr_of, page_text, text_of


class Job51Site(BaseSite):
    site_id = "job51"
    name = "前程无忧"
    base_url = "https://we.51job.com"
    login_url = "https://login.51job.com/login.php"
    search_url = "https://we.51job.com/pc/search?jobArea={city}&keyword={kw}"
    city_codes = {
        "北京": "010000", "上海": "020000", "广州": "030200", "深圳": "040000",
        "杭州": "080200", "苏州": "070300", "南京": "070200", "成都": "090200",
        "武汉": "180200", "西安": "200200", "天津": "050000", "重庆": "060000",
    }
    city_slugs = {
        "北京": "beijing", "上海": "shanghai", "广州": "guangzhou",
        "深圳": "shenzhen", "杭州": "hangzhou", "苏州": "suzhou",
        "南京": "nanjing", "成都": "chengdu", "武汉": "wuhan",
        "西安": "xian", "天津": "tianjin", "重庆": "chongqing",
    }
    CARD = ".joblist-item, div.j_joblist .e"
    SEL = {
        "title": ".jname, .jobname, .t span.jname at",
        "url": "a.e, a[href*='/pc/search/job-detail'], .jname at",
        "salary": ".sal",
        "info": ".d span, .info span",
        "area": ".area .shrink-0",
        "company": ".cname",
        "company_tags": ".bc .dc, .introduction span",
        "posted": ".time",
    }
    DETAIL_RESP = ".bmsg.job_msg, .jdesc, .desc"

    @staticmethod
    def _sensor_data(card) -> dict:
        raw = attr_of(card, ".joblist-item-job", "sensorsdata")
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _detail_url(self, job_id: object, city_text: object) -> str | None:
        job_id = str(job_id or "").strip()
        city_text = str(city_text or self.home_city or "").strip()
        if not job_id:
            return None
        slug = next((slug for city, slug in self.city_slugs.items()
                     if city in city_text or city_text in city), None)
        if not slug:
            return None
        return f"https://jobs.51job.com/{slug}/{job_id}.html"

    def logged_in(self, page) -> bool:
        try:
            return page.locator("a:has-text('登录'), .login").count() == 0
        except Exception:
            return True

    def detail(self, page, job):
        # 51job serves a slider challenge to the headless detail browser. The
        # search card already exposes skill tags, so keep that verified data.
        return job

    def extract(self, page, keyword: str, max_cards: int) -> tuple[list[dict], int | None]:
        page.goto(self.search_link(keyword), wait_until="domcontentloaded")
        page.wait_for_timeout(3500)
        self.dismiss_popups(page)
        self.scroll_feed(page, times=4)
        cards = page.query_selector_all(self.CARD)[:max_cards]
        out: list[dict] = []
        for c in cards:
            sensor = self._sensor_data(c)
            infos = c.query_selector_all(self.SEL["info"]) or []
            company_tags = c.query_selector_all(self.SEL["company_tags"]) or []
            job_tags = c.query_selector_all(".joblist-item-job .tag, .tag") or []
            city_text = text_of(c, self.SEL["area"]) or sensor.get("jobArea")
            url = self._detail_url(sensor.get("jobId"), city_text)
            if not url:
                url = abs_url(attr_of(c, self.SEL["url"], "href"), self.base_url)
            out.append({
                "title": sensor.get("jobTitle") or text_of(c, self.SEL["title"]),
                "url": url,
                "salary": sensor.get("jobSalary") or text_of(c, self.SEL["salary"]),
                "experience": (sensor.get("jobYear")
                               or (infos[1].inner_text().strip() if len(infos) > 1 else None)),
                "education": (sensor.get("jobDegree")
                              or (infos[2].inner_text().strip() if len(infos) > 2 else None)),
                "city_text": city_text or (infos[0].inner_text().strip() if infos else None),
                "company": text_of(c, self.SEL["company"]),
                "company_size": company_tags[-1].inner_text().strip() if company_tags else None,
                "requirements": "\n".join(
                    tag.inner_text().strip() for tag in job_tags
                    if tag.inner_text().strip()),
                "posted": sensor.get("jobTime") or text_of(c, self.SEL["posted"]),
            })
        total = None
        from ..normalize import parse_count
        t = page_text(page, ".total-count, .j_result")
        if t:
            total = parse_count(t)
        return out, total
