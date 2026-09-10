from job_agent.normalize import (DEFAULT_SKILL_VOCAB, detect_urgency,
                                extract_skills, parse_city_district,
                                parse_count, parse_education, parse_experience,
                                parse_salary)
from job_agent.distance import DISTRICTS


class TestSalary:
    def test_k_range(self):
        assert parse_salary("25-40K") == (25.0, 40.0)

    def test_k_range_with_months(self):
        assert parse_salary("25-40K·16薪") == (25.0, 40.0)

    def test_wan(self):
        assert parse_salary("2-4万") == (20.0, 40.0)

    def test_wan_yearly(self):
        lo, hi = parse_salary("20-35万/年")
        assert abs(lo - 16.7) < 0.1 and abs(hi - 29.2) < 0.1

    def test_daily(self):
        lo, hi = parse_salary("300-500元/天")
        assert abs(lo - 6.5) < 0.1 and abs(hi - 10.9) < 0.1

    def test_lowercase_k_between(self):
        assert parse_salary("25k-40k") == (25.0, 40.0)

    def test_negotiable(self):
        assert parse_salary("面议") == (None, None)

    def test_one_sided(self):
        assert parse_salary("2万以上") == (20.0, None)
        assert parse_salary("25K以下") == (None, 25.0)

    def test_garbage(self):
        assert parse_salary(None) == (None, None)
        assert parse_salary("") == (None, None)
        assert parse_salary("薪资优厚") == (None, None)


class TestExperience:
    def test_range(self):
        assert parse_experience("3-5年") == (3.0, 5.0)
        assert parse_experience("5-10年经验") == (5.0, 10.0)

    def test_unlimited(self):
        assert parse_experience("经验不限") == (None, None)
        assert parse_experience("应届生") == (None, None)

    def test_one_sided(self):
        assert parse_experience("1年以下") == (0.0, 1.0)
        assert parse_experience("10年以上") == (10.0, None)


class TestEducation:
    def test_basic(self):
        assert parse_education("本科") == "本科"
        assert parse_education("大专及以上") == "大专"
        assert parse_education("学历不限") == "不限"
        assert parse_education("硕士及以上") == "硕士"
        assert parse_education("研究生") == "硕士"

    def test_unknown(self):
        assert parse_education(None) is None
        assert parse_education("博士优先".replace("博士", "")) is None


class TestCount:
    def test_basic(self):
        assert parse_count("1.2万") == 12000
        assert parse_count("876+") == 876
        assert parse_count("345人浏览") == 345
        assert parse_count(None) is None


class TestSkills:
    def test_extract(self):
        text = "熟悉 Java/Kafka，了解 Kubernetes 与大模型，用过 Go"
        skills = extract_skills(text, DEFAULT_SKILL_VOCAB)
        assert set(skills) >= {"Java", "Kafka", "Kubernetes", "大模型"}

    def test_no_false_positive_in_words(self):
        # “email” 不应命中 AI
        assert "AI" not in extract_skills("send me an email", DEFAULT_SKILL_VOCAB)

    def test_empty(self):
        assert extract_skills("", DEFAULT_SKILL_VOCAB) == []


class TestUrgency:
    def test_title(self):
        level, reason = detect_urgency("Java 高级工程师【急聘】", None, None)
        assert level == "high" and "急聘" in reason

    def test_jd(self):
        level, _ = detect_urgency("Java 工程师", "到岗时间：招满即止", None)
        assert level == "high"

    def test_fresh_post(self):
        level, reason = detect_urgency("Java 工程师", "职责描述", "2天前")
        assert level == "medium"

    def test_none(self):
        assert detect_urgency("Java 工程师", "常规JD", "两周前") == (None, None)


class TestCityDistrict:
    def test_city_dot_district(self):
        assert parse_city_district("苏州·工业园区", "苏州", DISTRICTS) == ("苏州", "工业园区")

    def test_district_only(self):
        assert parse_city_district("工业园区", "苏州", DISTRICTS) == ("苏州", "工业园区")

    def test_other_city(self):
        assert parse_city_district("上海市浦东新区", "苏州", DISTRICTS) == ("上海", "浦东新区")

    def test_unknown(self):
        assert parse_city_district(None, "苏州", DISTRICTS) == (None, None)
