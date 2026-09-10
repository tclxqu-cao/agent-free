"""规则匹配引擎：硬过滤 + 软评分（0-100）。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Job


@dataclass
class MatchResult:
    job: Job
    score: float = 0.0
    passed: bool = True
    reasons: list[str] = field(default_factory=list)  # 淘汰原因 / 加分点

    @property
    def distance_km(self) -> float | None:
        return self.job.raw.get("distance_km")


def _hard_filter(job: Job, rules: dict) -> str | None:
    title_company = f"{job.title} {job.company}"
    for w in rules.get("exclude_keywords") or []:
        if w and w in title_company:
            return f"命中排除词「{w}」"
    if rules.get("salary_min") and job.salary_max is not None \
            and job.salary_max < rules["salary_min"]:
        return f"薪资上限 {job.salary_max}K 低于下限 {rules['salary_min']}K"
    cities = rules.get("cities") or []
    if cities and job.city and not any(c in job.city or job.city in c for c in cities):
        return f"城市 {job.city} 不在 {cities}"
    allow = rules.get("education_allow") or []
    if allow and job.education and job.education not in allow:
        return f"学历要求 {job.education} 不在允许列表"
    max_km = rules.get("max_distance_km")
    dist = job.raw.get("distance_km")
    if max_km and dist is not None and dist > max_km:
        return f"距家 {dist}km 超过上限 {max_km}km"
    exp_max = rules.get("experience_max")
    if exp_max and job.experience_min is not None and job.experience_min > exp_max:
        return f"要求 {job.experience_min}年 超出经验上限 {exp_max}年"
    return None


def _score(job: Job, rules: dict, profile: dict) -> tuple[float, list[str]]:
    score, notes = 0.0, []

    # 薪资（30）：薪资中点 / 期望薪资
    expected = float(rules.get("expected_salary") or profile.get("expectations", {})
                     .get("salary_min") or job.salary_mid or 1)
    if job.salary_mid is not None and expected:
        ratio = min(job.salary_mid / expected, 1.3)
        score += min(30.0, 30.0 * ratio / 1.3)  # ratio ≥1.3 → 满分
        notes.append(f"薪资 {job.salary_mid:.0f}K/期望 {expected:.0f}K")

    # 距离（20）：<=5km 满分，5..max_km 线性衰减
    dist = job.raw.get("distance_km")
    max_km = float(rules.get("max_distance_km") or 50)
    if dist is None:
        score += 12.0
    else:
        score += 20.0 if dist <= 5 else max(0.0, 20.0 * (1 - (dist - 5) / max_km))
        notes.append(f"距家 {dist}km")

    # 技能重合（30）：岗位技能 ∩ 我的技能 / 我的技能
    my_skills = set(profile.get("skills") or [])
    if my_skills:
        overlap = my_skills & set(job.skills or [])
        score += 30.0 * len(overlap) / len(my_skills)
        if overlap:
            notes.append("技能命中 " + "/".join(sorted(overlap)[:5]))

    # 经验贴合（20）：年限区间包含我的年限
    years = profile.get("years")
    if years and job.experience_min is not None:
        hi = job.experience_max if job.experience_max is not None else 99
        if job.experience_min <= years <= hi:
            score += 20.0
        else:
            gap = job.experience_min - years if years < job.experience_min else years - hi
            score += max(0.0, 20.0 - 4.0 * gap)
    else:
        score += 12.0
    return round(score, 1), notes


def filter_and_score(jobs: list[Job], rules: dict, profile: dict) -> list[MatchResult]:
    results: list[MatchResult] = []
    for job in jobs:
        reason = _hard_filter(job, rules)
        if reason:
            results.append(MatchResult(job=job, passed=False, reasons=[reason]))
            continue
        score, notes = _score(job, rules, profile)
        results.append(MatchResult(job=job, score=score, passed=True, reasons=notes))
    results.sort(key=lambda r: (not r.passed, -r.score))
    return results
