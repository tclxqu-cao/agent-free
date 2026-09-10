"""拉勾网适配器。"""

from __future__ import annotations

from .base import BaseSite, abs_url, attr_of, page_text, text_of


class LagouSite(BaseSite):
    site_id = "lagou"
    name = "拉勾网"
    base_url = "https://www.lagou.com"
    login_url = "https://passport.lagou.com/login/login.html"
    search_url = "https://www.lagou.com/jobs/list_{kw}?city={city}"
    city_codes = {
        "北京": "北京", "上海": "上海", "广州": "广州", "深圳": "深圳", "杭州": "杭州",
        "苏州": "苏州", "南京": "南京", "成都": "成都", "武汉": "武汉", "西安": "西安",
        "天津": "天津", "重庆": "重庆",
    }
    CARD = "#job_list li, .s_position_list li, div.item__RjdOA"
    SEL = {
        "title": ".position_name .name, .p_top h3, [class*='positionname']",
        "url": ".position_name a, .p_top a, a[href*='/jobs/']",
        "salary": ".money, .p_bot .money, [class*='salary']",
        "tags": ".p_info span, .li_b_l span, [class*='position'] .format span",
        "company": ".company_name a, [class*='company'] .name",
        "company_tags": ".industry span, .company_industry",
        "posted": ".format-time, .p_top .time",
    }
    DETAIL_RESP = ".job-detail, .job_bt .job-detail, dd.job_detail"

    def logged_in(self, page) -> bool:
        try:
            return page.locator("a:has-text('登录')").count() == 0
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
            out.append({
                "title": text_of(c, self.SEL["title"]),
                "url": abs_url(attr_of(c, self.SEL["url"]), self.base_url),
                "salary": text_of(c, self.SEL["salary"]),
                "experience": tags[0].inner_text().strip() if tags else None,
                "education": tags[1].inner_text().strip() if len(tags) > 1 else None,
                "city_text": text_of(c, ".add, .position-address, em"),
                "company": text_of(c, self.SEL["company"]),
                "company_size": (tags[-1].inner_text().strip()
                                 if len(tags) > 2 else None),
                "posted": text_of(c, self.SEL["posted"]),
            })
        total = None
        from ..normalize import parse_count
        t = page_text(page, ".total_count span, .positionCount span")
        if t:
            total = parse_count(t)
        return out, total
