"""站点适配器纯函数解析测试（parse_card_dict，用真实格式样例 dict）。"""

import json

import pytest

from job_agent.sites import REGISTRY, get_site


@pytest.fixture
def home_city():
    return "苏州"


@pytest.fixture
def sites(home_city):
    return {sid: get_site(sid, home_city) for sid in REGISTRY}


SAMPLES = {
    "boss": {
        "title": "高级Java开发工程师【急聘】",
        "company": "苏州某网络科技有限公司",
        "url": "https://www.zhipin.com/job_detail/abc.html",
        "salary": "25-40K·16薪",
        "experience": "5-10年",
        "education": "本科",
        "city_text": "苏州·工业园区",
        "company_size": "1000-9999人",
        "hr": "张女士 · 刚刚活跃",
    },
    "zhilian": {
        "title": "Java开发工程师",
        "company": "智联测试公司",
        "url": "https://www.zhaopin.com/job_detail/x",
        "salary": "2-4万",
        "experience": "3-5年",
        "education": "本科及以上",
        "city_text": "苏州",
        "company_size": "500-999人",
    },
    "liepin": {
        "title": "Java 架构师",
        "company": "猎聘测试公司",
        "url": "https://www.liepin.com/job/x",
        "salary": "20-35万/年",
        "experience": "8-12年",
        "education": "本科",
        "city_text": "苏州工业园区",
        "posted": "3天前",
    },
    "job51": {
        "title": "前端开发（Vue3）",
        "company": "51测试公司",
        "url": "https://we.51job.com/pc/search/job-detail/x",
        "salary": "300-500元/天",
        "experience": "1-3年",
        "education": "大专及以上",
        "city_text": "苏州姑苏区",
    },
    "lagou": {
        "title": "Golang 后端开发",
        "company": "拉勾测试公司",
        "url": "https://www.lagou.com/jobs/x.html",
        "salary": "25k-40k",
        "experience": "3-5年",
        "education": "不限",
        "city_text": "苏州·吴中区",
        "company_size": "150-500人",
    },
}


def test_registry_complete():
    assert set(REGISTRY) == {"boss", "zhilian", "liepin", "job51", "lagou"}


def test_search_link_city_param():
    site = get_site("boss", "苏州")
    assert "city=101210400" in site.search_link("Java")
    site2 = get_site("boss", None)
    assert "city=" not in site2.search_link("Java")


def test_job51_extracts_current_card_contract(monkeypatch):
    class Node:
        def __init__(self, text="", attrs=None):
            self.text = text
            self.attrs = attrs or {}

        def inner_text(self):
            return self.text

        def get_attribute(self, name):
            return self.attrs.get(name)

    class Card:
        nodes = {
            ".joblist-item-job": Node(attrs={"sensorsdata": json.dumps({
                "jobId": "162453184", "jobTitle": "Java开发工程师",
                "jobSalary": "20-40万/年", "jobArea": "成都",
                "jobYear": "5年及以上", "jobDegree": "本科",
                "jobTime": "2026-09-24 10:00:00",
            }, ensure_ascii=False)}),
            ".jname, .jobname, .t span.jname at": Node("错误标题"),
            ".area .shrink-0": Node("成都·武侯区"),
            ".cname": Node("测试公司"),
        }

        def query_selector(self, selector):
            return self.nodes.get(selector)

        def query_selector_all(self, selector):
            if selector == ".bc .dc, .introduction span":
                return [Node("计算机软件"), Node("1000-5000人")]
            if selector == ".joblist-item-job .tag, .tag":
                return [Node("Java"), Node("Spring Boot")]
            return []

    class Page:
        def goto(self, *_args, **_kwargs):
            return None

        def wait_for_timeout(self, *_args):
            return None

        def query_selector_all(self, selector):
            return [Card()] if selector == ".joblist-item, div.j_joblist .e" else []

        def query_selector(self, _selector):
            return None

    site = get_site("job51", "成都")
    monkeypatch.setattr(site, "dismiss_popups", lambda _page: None)
    monkeypatch.setattr(site, "scroll_feed", lambda _page, times: None)

    cards, total = site.extract(Page(), "Java", 10)

    assert total is None
    assert cards == [{
        "title": "Java开发工程师",
        "url": "https://jobs.51job.com/chengdu/162453184.html",
        "salary": "20-40万/年",
        "experience": "5年及以上",
        "education": "本科",
        "city_text": "成都·武侯区",
        "company": "测试公司",
        "company_size": "1000-5000人",
        "requirements": "Java\nSpring Boot",
        "posted": "2026-09-24 10:00:00",
    }]

    job = site.parse_card_dict(cards[0])
    assert (job.city, job.district) == ("成都", "武侯区")
    assert job.requirements_extra == ["Java", "Spring Boot"]


class TestParseCards:
    def test_boss(self, sites):
        job = sites["boss"].parse_card_dict(SAMPLES["boss"])
        assert job.site == "boss"
        assert job.title.startswith("高级Java")
        assert (job.salary_min, job.salary_max) == (25.0, 40.0)
        assert (job.experience_min, job.experience_max) == (5.0, 10.0)
        assert job.education == "本科"
        assert (job.city, job.district) == ("苏州", "工业园区")
        assert job.company_size == "1000-9999人"
        assert job.urgency == "high"  # 标题含急聘
        assert job.job_id and job.url.startswith("https://")

    def test_zhilian(self, sites):
        job = sites["zhilian"].parse_card_dict(SAMPLES["zhilian"])
        assert (job.salary_min, job.salary_max) == (20.0, 40.0)
        assert job.education == "本科"
        assert (job.city, job.district) == ("苏州", None)
        assert job.company_size == "500-999人"

    def test_liepin(self, sites):
        job = sites["liepin"].parse_card_dict(SAMPLES["liepin"])
        assert abs(job.salary_min - 16.7) < 0.1  # 万/年 → K/月
        assert abs(job.salary_max - 29.2) < 0.1
        assert (job.experience_min, job.experience_max) == (8.0, 12.0)
        assert (job.city, job.district) == ("苏州", "工业园区")
        assert job.posted_text == "3天前"

    def test_job51(self, sites):
        job = sites["job51"].parse_card_dict(SAMPLES["job51"])
        assert abs(job.salary_min - 6.5) < 0.1  # 日薪折算月薪
        assert abs(job.salary_max - 10.9) < 0.1
        assert job.education == "大专"
        assert (job.city, job.district) == ("苏州", "姑苏区")

    def test_lagou(self, sites):
        job = sites["lagou"].parse_card_dict(SAMPLES["lagou"])
        assert (job.salary_min, job.salary_max) == (25.0, 40.0)
        assert (job.city, job.district) == ("苏州", "吴中区")
        assert "Golang" in job.skills

    def test_minimal_dict_no_crash(self, sites):
        for sid, site in sites.items():
            job = site.parse_card_dict({})
            assert job.site == sid and job.job_id

    def test_skills_from_jd_text(self, sites):
        d = dict(SAMPLES["boss"])
        d["responsibilities"] = "1、负责基于 Spring Boot 的微服务开发\n2、使用 Kafka 做消息分发"
        d["requirements"] = "熟悉 Kubernetes 与 Redis，有大模型应用经验加分"
        job = sites["boss"].parse_card_dict(d)
        assert {"Spring Boot", "微服务", "Kafka", "Kubernetes", "Redis"} <= set(job.skills)
        assert len(job.responsibilities) == 2
