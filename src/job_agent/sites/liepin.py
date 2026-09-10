"""猎聘适配器。"""

from __future__ import annotations

from .base import BaseSite, abs_url, attr_of, page_text, text_of


class LiepinSite(BaseSite):
    site_id = "liepin"
    name = "猎聘"
    base_url = "https://www.liepin.com"
    login_url = "https://login.liepin.com/account/login"
    search_url = "https://www.liepin.com/zhaopin/?key={kw}&city={city}"
    city_codes = {
        "北京": "010", "上海": "020", "广州": "050020", "深圳": "050090",
        "杭州": "070020", "苏州": "070080", "南京": "070030",
        "成都": "280020", "武汉": "170020", "西安": "270010", "天津": "030", "重庆": "040",
    }
    CARD = "div.job-card-pc-container, .job-list-box li, .sojob-result li"
    SEL = {
        "title": ".job-title, .job-info h3 a",
        "url": "a[href*='/job/'], .job-info h3 a",
        "salary": ".job-salary, .text-warning",
        "tags": ".job-labels span, .labels span",
        "company": ".company-name a, .company-sojob span",
        "company_tags": ".company-tags span, .job-dq at",
        "posted": ".job-time, time",
    }
    DETAIL_RESP = ".job-detail-container, .content-word, .job-detail"

    def logged_in(self, page) -> bool:
        try:
            return page.locator("a:has-text('登录'), [data-nick='登录']").count() == 0
        except Exception:
            return True

    def extract(self, page, keyword: str, max_cards: int) -> tuple[list[dict], int | None]:
        page.goto(self.search_link(keyword), wait_until="domcontentloaded")
        page.wait_for_timeout(3500)
        self.dismiss_popups(page)
        self.scroll_feed(page)
        cards = page.query_selector_all(self.CARD)[:max_cards]
        out: list[dict] = []
        for c in cards:
            tags = c.query_selector_all(self.SEL["tags"]) or []
            company_tags = c.query_selector_all(self.SEL["company_tags"]) or []
            out.append({
                "title": text_of(c, self.SEL["title"]),
                "url": abs_url(attr_of(c, self.SEL["url"]), self.base_url),
                "salary": text_of(c, self.SEL["salary"]),
                "experience": tags[1].inner_text().strip() if len(tags) > 1 else None,
                "education": tags[2].inner_text().strip() if len(tags) > 2 else None,
                "city_text": tags[0].inner_text().strip() if tags else None,
                "company": text_of(c, self.SEL["company"]),
                "company_size": company_tags[-1].inner_text().strip() if company_tags else None,
                "posted": text_of(c, self.SEL["posted"]),
            })
        total = None
        from ..normalize import parse_count
        t = page_text(page, ".total-job-count, .job-count")
        if t:
            total = parse_count(t)
        return out, total
