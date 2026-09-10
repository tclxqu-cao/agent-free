import json

import pytest

from job_agent.advisor import AdvisorReport, advise, fallback_advice, llm_chat
from job_agent.match import MatchResult
from job_agent.models import Job


def make_match(title="Java 工程师", url="u1", salary=(25.0, 40.0),
               skills=None, dist=8.0, passed=True) -> MatchResult:
    job = Job(site="boss", title=title, company="公司", url=url,
              salary_min=salary[0], salary_max=salary[1],
              skills=skills or ["Java", "Kafka"])
    job.raw["distance_km"] = dist
    return MatchResult(job=job, score=70.0 if passed else 0, passed=passed,
                       reasons=["薪资 32K/期望 30K"] if passed else ["命中排除词"])


@pytest.fixture
def demo_db(db, config):
    from job_agent.demo import seed_demo_data
    seed_demo_data(db, config, days=8)
    return db


class TestFallback:
    def test_learning_gap(self, demo_db, profile):
        matches = [make_match(skills=["Java", "Kafka", "LangChain", "RAG"])]
        rep = fallback_advice(demo_db, profile, matches)
        # profile 无 LangChain/RAG → 应出现在学习建议
        assert any("LangChain" in x for x in rep.learning)
        assert rep.match_notes
        assert not rep.llm_used

    def test_level_up(self, demo_db, profile):
        matches = [
            make_match(url="u1", salary=(25.0, 40.0), skills=["Java"]),
            make_match(url="u2", title="资深专家", salary=(50.0, 80.0),
                       skills=["Java", "Kubernetes", "架构设计"]),
        ]
        rep = fallback_advice(demo_db, profile, matches)
        assert rep.level_up
        assert any("架构" in x or "Kubernetes" in x for x in rep.level_up)

    def test_trends_from_data(self, demo_db, profile):
        rep = fallback_advice(demo_db, profile, [make_match()])
        assert rep.trends
        assert any("岗位数" in t or "技能" in t or "月薪" in t for t in rep.trends)

    def test_empty_matches(self, demo_db, profile):
        rep = fallback_advice(demo_db, profile, [])
        assert rep.learning == []
        assert isinstance(rep, AdvisorReport)


class TestLLM:
    def test_disabled_returns_none(self, config):
        assert llm_chat({"enabled": False}, "sys", "user") is None
        assert llm_chat({"enabled": True, "base_url": "http://x",
                         "api_key": ""}, "sys", "user") is None

    def test_network_error_returns_none(self, config, monkeypatch):
        import httpx
        def boom(*a, **kw):
            raise httpx.ConnectError("refused")
        monkeypatch.setattr(httpx, "post", boom)
        cfg = {"enabled": True, "base_url": "http://localhost:1/v1",
               "api_key": "k", "model": "m", "timeout": 1}
        assert llm_chat(cfg, "sys", "user") is None

    def test_advise_falls_back_without_llm(self, demo_db, config, config_dir,
                                           profile):
        matches = [make_match()]
        rep = advise(demo_db, config, config_dir, profile, matches)
        assert rep.llm_used is False

    def test_advise_uses_llm_when_ok(self, demo_db, config, config_dir, profile,
                                     monkeypatch):
        config = dict(config)
        config["llm"] = {"enabled": True, "base_url": "http://x/v1",
                         "api_key": "k", "model": "m", "timeout": 5}
        payload = json.dumps({
            "match_notes": [{"job_id": "u1", "note": "匹配度高"}],
            "learning": ["学 RAG"], "level_up": ["补架构"], "trends": ["AI 岗位增长"],
        })
        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return {"choices": [{"message": {"content": payload}}]}
        import job_agent.advisor as adv
        monkeypatch.setattr(adv.httpx, "post", lambda *a, **kw: FakeResp())
        rep = advise(demo_db, config, config_dir, profile, [make_match()])
        assert rep.llm_used is True
        assert rep.learning == ["学 RAG"]
        assert rep.match_notes == {"u1": "匹配度高"}

    def test_advise_falls_back_on_bad_json(self, demo_db, config, config_dir,
                                           profile, monkeypatch):
        config = dict(config)
        config["llm"] = {"enabled": True, "base_url": "http://x/v1",
                         "api_key": "k", "model": "m", "timeout": 5}
        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return {"choices": [{"message": {"content": "不是JSON"}}]}
        import job_agent.advisor as adv
        monkeypatch.setattr(adv.httpx, "post", lambda *a, **kw: FakeResp())
        rep = advise(demo_db, config, config_dir, profile, [make_match()])
        assert rep.llm_used is False
