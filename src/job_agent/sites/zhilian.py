"""智联招聘适配器。"""

from __future__ import annotations

from .base import BaseSite, abs_url, attr_of, page_text, text_of


class ZhilianSite(BaseSite):
    site_id = "zhilian"
    name = "智联招聘"
    base_url = "https://www.zhaopin.com"
    login_url = "https://passport.zhaopin.com/login"
    search_url = "https://sou.zhaopin.com/?jl={city}&kw={kw}"
    city_codes = {
        "北京": 530, "上海": 538, "广州": 763, "深圳": 765, "杭州": 653,
        "苏州": 636, "南京": 635, "成都": 801, "武汉": 736, "西安": 854,
        "天津": 531, "重庆": 551,
    }
    CARD = ".joblist-box__item, div.positionlist div.jobinfo"
    SEL = {
        "title": ".jobinfo__name, .iteminfo__line1__jobname span",
        "url": "a[href*='/job_detail/'], .iteminfo__line1__jobname a",
        "salary": ".jobinfo__salary",
        "other": ".jobinfo__other-content span, .iteminfo__line2__jobdesc span",
        "company": ".companyinfo__name a, .company__name",
        "company_tags": ".companyinfo__tag span",
    }
    DETAIL_RESP = ".describle .describle__detail-content, .summary-job-detail"
    DETAIL_REQ = ".describle .describle__detail-content"

    def logged_in(self, page) -> bool:
        # 智联首页/搜索页未登录时存在 *no-login* 类的登录入口；已登录则消失
        try:
            return page.locator("[class*='no-login']").count() == 0
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
            others = c.query_selector_all(self.SEL["other"]) or []
            exp = others[0].inner_text().strip() if len(others) > 0 else None
            edu = others[1].inner_text().strip() if len(others) > 1 else None
            company_tags = c.query_selector_all(self.SEL["company_tags"]) or []
            out.append({
                "title": text_of(c, self.SEL["title"]),
                "url": abs_url(attr_of(c, self.SEL["url"]), self.base_url),
                "salary": text_of(c, self.SEL["salary"]),
                "experience": exp,
                "education": edu,
                "city_text": others[2].inner_text().strip() if len(others) > 2 else None,
                "company": text_of(c, self.SEL["company"]),
                "industry": company_tags[0].inner_text().strip() if company_tags else None,
                "company_size": company_tags[-1].inner_text().strip() if len(company_tags) > 1 else None,
            })
        total = None
        from ..normalize import parse_count
        t = page_text(page, ".joblist-box__main em, .soutian-total")
        if t:
            total = parse_count(t)
        return out, total
