"""前程无忧（51job，we.51job.com）适配器。"""

from __future__ import annotations

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
    CARD = "div.j_joblist .e, .joblist-item"
    SEL = {
        "title": ".jname, .t span.jname at, .jobname",
        "url": "a.e, a[href*='/pc/search/job-detail'] .jname, .jname at",
        "salary": ".sal",
        "info": ".d span, .info span",
        "company": ".cname",
        "company_tags": ".introduction span",
        "posted": ".time",
    }
    DETAIL_RESP = ".jdesc, .desc"

    def logged_in(self, page) -> bool:
        try:
            return page.locator("a:has-text('登录'), .login").count() == 0
        except Exception:
            return True

    def extract(self, page, keyword: str, max_cards: int) -> tuple[list[dict], int | None]:
        page.goto(self.search_link(keyword), wait_until="domcontentloaded")
        page.wait_for_timeout(3500)
        self.dismiss_popups(page)
        self.scroll_feed(page, times=4)
        cards = page.query_selector_all(self.CARD)[:max_cards]
        out: list[dict] = []
        for c in cards:
            infos = c.query_selector_all(self.SEL["info"]) or []
            company_tags = c.query_selector_all(self.SEL["company_tags"]) or []
            out.append({
                "title": text_of(c, self.SEL["title"]),
                "url": abs_url(attr_of(c, "a", "href") or attr_of(c, self.SEL["url"], "href"),
                               self.base_url),
                "salary": text_of(c, self.SEL["salary"]),
                "experience": infos[1].inner_text().strip() if len(infos) > 1 else None,
                "education": infos[2].inner_text().strip() if len(infos) > 2 else None,
                "city_text": infos[0].inner_text().strip() if infos else None,
                "company": text_of(c, self.SEL["company"]),
                "company_size": company_tags[-1].inner_text().strip() if company_tags else None,
                "posted": text_of(c, self.SEL["posted"]),
            })
        total = None
        from ..normalize import parse_count
        t = page_text(page, ".total-count, .j_result")
        if t:
            total = parse_count(t)
        return out, total
