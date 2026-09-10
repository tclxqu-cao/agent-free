import hashlib

import pytest

from job_agent.models import Job


def make_job(**kw) -> Job:
    defaults = dict(site="boss", title="Java 工程师", company="测试公司",
                    url="https://x/job/1", salary_text="25-40K",
                    salary_min=25.0, salary_max=40.0,
                    experience_text="3-5年", experience_min=3.0, experience_max=5.0,
                    education="本科", city="苏州", district="工业园区",
                    skills=["Java", "Spring Boot", "MySQL"])
    defaults.update(kw)
    job = Job(**defaults)
    return job


RULES = {
    "salary_min": 20, "experience_max": 10, "education_allow": ["不限", "大专", "本科"],
    "cities": ["苏州"], "exclude_keywords": ["外包", "驻场"], "max_distance_km": 50,
    "expected_salary": 30,
}


class TestHardFilter:
    def test_pass(self, profile):
        from job_agent.match import filter_and_score
        res = filter_and_score([make_job()], RULES, profile)
        assert res[0].passed

    def test_excluded(self, profile):
        from job_agent.match import filter_and_score
        res = filter_and_score([make_job(title="Java 外包工程师")], RULES, profile)
        assert not res[0].passed and "外包" in res[0].reasons[0]

    def test_salary_too_low(self, profile):
        from job_agent.match import filter_and_score
        res = filter_and_score([make_job(salary_min=10, salary_max=15)], RULES, profile)
        assert not res[0].passed

    def test_city(self, profile):
        from job_agent.match import filter_and_score
        res = filter_and_score([make_job(city="上海", district=None)], RULES, profile)
        assert not res[0].passed

    def test_education(self, profile):
        from job_agent.match import filter_and_score
        res = filter_and_score([make_job(education="硕士")], RULES, profile)
        assert not res[0].passed

    def test_education_unknown_passes(self, profile):
        from job_agent.match import filter_and_score
        res = filter_and_score([make_job(education=None)], RULES, profile)
        assert res[0].passed

    def test_distance(self, profile):
        from job_agent.match import filter_and_score
        job = make_job()
        job.raw["distance_km"] = 80.0
        res = filter_and_score([job], RULES, profile)
        assert not res[0].passed

    def test_experience(self, profile):
        from job_agent.match import filter_and_score
        res = filter_and_score([make_job(experience_min=15, experience_max=20)],
                               RULES, profile)
        assert not res[0].passed


class TestScore:
    def test_sorted_and_scored(self, profile):
        from job_agent.match import filter_and_score
        good = make_job()
        good.raw["distance_km"] = 3.0
        mid = make_job(salary_min=18, salary_max=25, url="https://x/2")
        mid.raw["distance_km"] = 30.0
        bad = make_job(title="驻场 Java", url="https://x/3")
        res = filter_and_score([mid, bad, good], RULES, profile)
        assert res[0].job is good
        assert res[0].score > res[1].score >= 0
        assert not res[-1].passed

    def test_close_distance_full_points(self, profile):
        from job_agent.match import _score
        job = make_job()
        job.raw["distance_km"] = 3.0
        score, _ = _score(job, RULES, profile)
        # 距离满分 20 + 其他分项 → 总分应超过 60
        assert score > 60


def test_job_id_stable():
    j1 = make_job()
    j2 = make_job()
    assert j1.job_id == j2.job_id
    assert j1.job_id == hashlib.sha1(b"boss|https://x/job/1").hexdigest()[:16]
