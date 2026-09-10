"""站点适配器基类：DOM 提取(thin) + 纯函数解析(可测)。

extract() 把 DOM 转成统一键的 dict，parse_card_dict() 是纯函数负责 dict→Job 与
归一化 —— 后者用 dict 样例做单元测试，选择器漂移只影响前者。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from urllib.parse import quote

from ..models import Job
from ..normalize import (DEFAULT_SKILL_VOCAB, detect_urgency, extract_skills,
                         parse_city_district, parse_count, parse_education,
                         parse_experience, parse_salary)
from ..distance import DISTRICTS


def text_of(el, selector: str) -> str | None:
    """ElementHandle 内取文本，静默失败。"""
    try:
        node = el.query_selector(selector)
        return node.inner_text().strip() if node else None
    except Exception:
        return None


def attr_of(el, selector: str, attr: str = "href") -> str | None:
    try:
        node = el.query_selector(selector)
        return node.get_attribute(attr) if node else None
    except Exception:
        return None


def page_text(page, selector: str) -> str | None:
    try:
        node = page.query_selector(selector)
        return node.inner_text().strip() if node else None
    except Exception:
        return None


def abs_url(url: str | None, base: str) -> str | None:
    if not url:
        return None
    if url.startswith("http"):
        return url
    return base.rstrip("/") + "/" + url.lstrip("/")


def split_jd_lines(text: str | None) -> list[str]:
    """把 JD 段落拆成条目列表：支持换行与 1、2、3．编号。"""
    if not text:
        return []
    text = text.replace("\r", "")
    parts = re.split(r"\n+|(?:^|\s)\d+[、\.．\)）]\s*", text)
    return [p.strip(" ；;。") for p in parts if p and p.strip(" ；;。")][:30]


class BaseSite(ABC):
    site_id: str = ""
    name: str = ""
    login_url: str = ""
    base_url: str = ""
    search_url: str = ""          # .format(kw=..., city=...)
    city_codes: dict = {}         # 城市→站点城市码；无码则不拼 city 参数

    def __init__(self, home_city: str | None = None):
        self.home_city = home_city

    def city_param(self) -> str:
        if self.home_city:
            for name, code in self.city_codes.items():
                if name in self.home_city or self.home_city in name:
                    return str(code)
        return ""

    def search_link(self, keyword: str) -> str:
        url = self.search_url.format(kw=quote(keyword), city=self.city_param())
        if not self.city_param():  # 去掉空的 city 参数
            url = re.sub(r"[&?]city=(?=&|$)", "", url)
        return url

    # --- DOM 层（站点改版时只需修这里） ---

    @abstractmethod
    def extract(self, page, keyword: str, max_cards: int) -> tuple[list[dict], int | None]:
        """返回 (卡片原始 dict 列表, 搜索结果总数)。"""

    def logged_in(self, page) -> bool:
        """默认 True（无法判断时放行）；子类用登录特征选择器覆写。"""
        return True

    def dismiss_popups(self, page) -> None:
        """关闭弹窗/遮罩，子类可覆写；静默失败。"""
        for sel in ("[class*='close']", ".dialog-close", "[aria-label='关闭']"):
            try:
                for btn in page.query_selector_all(sel)[:3]:
                    btn.click(timeout=500)
            except Exception:
                continue

    def scroll_feed(self, page, times: int = 3) -> None:
        for _ in range(times):
            try:
                page.mouse.wheel(0, 1600)
                page.wait_for_timeout(800)
            except Exception:
                break

    def detail(self, page, job: Job) -> Job:
        """进详情页补充职责/要求/技能；失败返回原 job。"""
        try:
            if not job.url:
                return job
            page.goto(job.url, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            resp = page_text(page, self.DETAIL_RESP if hasattr(self, "DETAIL_RESP") else "body")
            req = page_text(page, self.DETAIL_REQ) if hasattr(self, "DETAIL_REQ") else None
            if resp:
                job.responsibilities = split_jd_lines(resp)[:15]
            if req:
                job.requirements_extra = split_jd_lines(req)[:15]
            jd_text = "\n".join(job.responsibilities + job.requirements_extra)
            if jd_text:
                job.skills = extract_skills(job.title + "\n" + jd_text)
                urgency, reason = detect_urgency(job.title, jd_text, job.posted_text)
                job.urgency, job.urgency_reason = urgency, reason
        except Exception:
            pass
        return job

    # --- 纯函数解析层（可单测） ---

    def parse_card_dict(self, d: dict) -> Job:
        """统一键 dict → Job。键约定（均可缺省）：
        title, company, url, salary, experience, education, city_text, address,
        company_size, industry, responsibilities, requirements, views_text,
        greets_text, hr, posted"""
        job = Job(
            site=self.site_id,
            title=(d.get("title") or "").strip(),
            company=(d.get("company") or "").strip(),
            url=d.get("url") or "",
        )
        job.company_size = d.get("company_size")
        job.industry = d.get("industry")
        job.salary_text = d.get("salary")
        job.salary_min, job.salary_max = parse_salary(d.get("salary"))
        job.experience_text = d.get("experience")
        job.experience_min, job.experience_max = parse_experience(d.get("experience"))
        job.education = parse_education(d.get("education"))
        job.city, job.district = parse_city_district(
            d.get("city_text"), self.home_city, DISTRICTS)
        job.address = d.get("address")
        job.responsibilities = split_jd_lines(d.get("responsibilities"))
        job.requirements_extra = split_jd_lines(d.get("requirements"))
        job.views = parse_count(d.get("views_text"))
        job.greets = parse_count(d.get("greets_text"))
        job.hr_name = d.get("hr")
        job.posted_text = d.get("posted")
        jd_text = "\n".join(job.responsibilities + job.requirements_extra)
        job.skills = extract_skills(job.title + "\n" + jd_text)
        job.urgency, job.urgency_reason = detect_urgency(
            job.title, jd_text, job.posted_text)
        return job
