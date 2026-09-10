"""Boss直聘适配器。选择器基于 2025 前后页面结构，站点改版时只需调整类顶常量。"""

from __future__ import annotations

from .base import BaseSite, abs_url, attr_of, page_text, text_of


class BossSite(BaseSite):
    site_id = "boss"
    name = "Boss直聘"
    base_url = "https://www.zhipin.com"
    login_url = "https://www.zhipin.com/web/user/?ka=header-login"
    search_url = "https://www.zhipin.com/web/geek/job?query={kw}&city={city}"
    city_codes = {
        "北京": 101010100, "上海": 101020100, "广州": 101280100, "深圳": 101280600,
        "杭州": 101210100, "苏州": 101210400, "南京": 101190100, "成都": 101270100,
        "武汉": 101200100, "西安": 101110100, "天津": 101030100, "重庆": 101040100,
    }
    CARD = "li.job-card-wrapper"
    SEL = {
        "title": ".job-name",
        "url": ".job-name a, a.job-card-left",
        "area": ".job-area",
        "salary": ".salary",
        "tags": ".job-info .filter-labels span",
        "company": ".company-name a",
        "company_tags": ".company-tag-list li",
        "hr": ".job-info .pub-info",
    }

    def logged_in(self, page) -> bool:
        # 未登录时页头出现登录按钮
        if "安全验证" in (page_text(page, "body") or ""):
            raise RuntimeError("Boss直聘触发安全验证，请在浏览器中手动通过后重试")
        try:
            return page.locator(".header-login-btn").count() == 0
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
            exp = tags[0].inner_text().strip() if len(tags) > 0 else None
            edu = tags[1].inner_text().strip() if len(tags) > 1 else None
            out.append({
                "title": text_of(c, self.SEL["title"]),
                "url": abs_url(attr_of(c, self.SEL["url"]), self.base_url),
                "salary": text_of(c, self.SEL["salary"]),
                "experience": exp,
                "education": edu,
                "city_text": text_of(c, self.SEL["area"]),
                "company": text_of(c, self.SEL["company"]),
                "company_size": company_tags[-1].inner_text().strip() if company_tags else None,
                "hr": text_of(c, self.SEL["hr"]),
            })
        total = None
        try:
            t = page_text(page, ".job-total") or page_text(page, ".search-job-result span")
            if t and any(ch.isdigit() for ch in t):
                from ..normalize import parse_count
                total = parse_count(t)
        except Exception:
            pass
        return out, total
