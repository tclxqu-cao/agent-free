"""顾问层：结合个人经历给出 匹配点评 / 学习清单 / 进阶差距 / 趋势展望。

LLM（OpenAI 兼容 API）优先；未启用或失败时降级为规则版。
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

import httpx

from .analysis import (count_trend, hot_jobs, new_and_missing, requirement_drift,
                       salary_trend)
from .match import MatchResult
from .models import Job


@dataclass
class AdvisorReport:
    match_notes: dict[str, str] = field(default_factory=dict)   # job_id → 一句话点评
    learning: list[str] = field(default_factory=list)           # 建议学习（缺口）
    level_up: list[str] = field(default_factory=list)           # 更上一层需要学
    trends: list[str] = field(default_factory=list)             # 未来趋势判断
    llm_used: bool = False


def llm_chat(llm_cfg: dict, system: str, user: str) -> str | None:
    """OpenAI 兼容 chat/completions；任何失败返回 None（调用方降级）。"""
    if not llm_cfg or not llm_cfg.get("enabled") or not llm_cfg.get("api_key"):
        return None
    try:
        r = httpx.post(
            f"{llm_cfg['base_url'].rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {llm_cfg['api_key']}"},
            json={
                "model": llm_cfg.get("model", "deepseek-chat"),
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "temperature": 0.3,
            },
            timeout=float(llm_cfg.get("timeout", 90)),
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception:
        return None


def _jobs_digest(jobs: list[Job], max_jobs: int = 10) -> str:
    lines = []
    for j in jobs[:max_jobs]:
        jd = "；".join((j.responsibilities + j.requirements_extra)[:4])
        lines.append(f"- {j.title} @ {j.company} | {j.salary_text or ''} | "
                     f"{j.experience_text or ''} | {j.education or ''} | 技能: "
                     f"{'/'.join(j.skills[:8])} | JD片段: {jd[:120]}")
    return "\n".join(lines)


def _analysis_digest(count: list[dict], salary: list[dict], skills: list[dict],
                     dist_shift: dict) -> str:
    tail = count[-7:]
    sal_tail = salary[-7:]
    hot = [s["skill"] for s in skills[:8]]
    return json.dumps({
        "近7天岗位数": tail, "近7天平均薪资K": sal_tail,
        "技能热度": hot, "学历分布": dist_shift.get("education"),
        "经验分布": dist_shift.get("experience"),
    }, ensure_ascii=False)


def _llm_advisor(db, config: dict, profile: dict,
                 matches: list[MatchResult]) -> AdvisorReport | None:
    """LLM 路径；解析失败返回 None。"""
    system = (
        "你是资深职业规划顾问。根据用户画像、候选岗位JD和岗位市场统计数据，"
        "严格输出 JSON（不要 markdown 代码块），字段："
        '{"match_notes":[{"job_id":"...","note":"一句话匹配点评(为什么合适/差什么)"}],'
        '"learning":["建议学习的技能/知识，带优先级理由"],'
        '"level_up":["针对更高层级岗位(架构师/专家/经理)的能力差距与学习建议"],'
        '"trends":["基于数据的岗位市场趋势判断，2-4条"]}'
    )
    drift = requirement_drift(db)
    user = json.dumps({
        "用户画像": {k: profile.get(k) for k in ("current_role", "years", "education",
                                                "skills", "summary", "target_next")},
        "候选岗位": _jobs_digest([m.job for m in matches if m.passed]),
        "市场统计": _analysis_digest(count_trend(db), salary_trend(db),
                                    drift["skills"], drift),
    }, ensure_ascii=False)
    raw = llm_chat(config.get("llm") or {}, system, user)
    if not raw:
        return None
    try:
        data = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
        return AdvisorReport(
            match_notes={m["job_id"]: m["note"] for m in data.get("match_notes", [])},
            learning=data.get("learning", []),
            level_up=data.get("level_up", []),
            trends=data.get("trends", []),
            llm_used=True,
        )
    except Exception:
        return None


def fallback_advice(db, profile: dict, matches: list[MatchResult]) -> AdvisorReport:
    """规则降级版：技能缺口 / 高薪岗位进阶清单 / 数据驱动的趋势陈述。"""
    passed = [m for m in matches if m.passed]
    report = AdvisorReport()
    for m in passed[:20]:
        if m.reasons:
            report.match_notes[m.job.job_id] = "；".join(m.reasons[:3])

    my_skills = set(profile.get("skills") or [])
    jd_counter: Counter = Counter()
    for m in passed:
        for s in m.job.skills or []:
            if s not in my_skills:
                jd_counter[s] += 1
    report.learning = [
        f"{skill}：{n} 个匹配岗位要求而你的技能清单未包含，建议优先补齐"
        for skill, n in jd_counter.most_common(8)
    ]

    # 进阶：薪资高于匹配池中位数的岗位 = “更上一层”样本
    mids = sorted(m.job.salary_mid for m in passed if m.job.salary_mid is not None)
    if mids:
        median = mids[len(mids) // 2]
        senior = [m for m in passed
                  if m.job.salary_mid is not None and m.job.salary_mid > median]
        senior_counter: Counter = Counter()
        for m in senior:
            for s in m.job.skills or []:
                if s not in my_skills:
                    senior_counter[s] += 1
        roles = "、".join((profile.get("target_next") or {}).get("roles") or ["更高层级岗位"])
        report.level_up = [
            f"目标（{roles}）样本岗位（薪资高于中位数 {median:.0f}K）高频要求的进阶技能："
            + "、".join(s for s, _ in senior_counter.most_common(8)),
            "除技术外建议积累：技术方案主导经验、跨团队协作与带教（对应架构师/专家岗 JD 常见软性要求）",
        ]

    # 趋势：数据转陈述
    count = count_trend(db, 14)
    salary = salary_trend(db, 14)
    skills = requirement_drift(db)["skills"]
    if len(count) >= 2:
        first, last = count[0]["active"], count[-1]["active"]
        direction = "上升" if last > first else ("下降" if last < first else "持平")
        report.trends.append(
            f"近两周活跃岗位数 {first} → {last}（{direction}），"
            f"最新单日新增 {count[-1]['new']} 个、下架 {count[-1]['missing']} 个")
    if len(salary) >= 2:
        f, l = salary[0], salary[-1]
        report.trends.append(
            f"平均月薪区间 {f['avg_min']}-{f['avg_max']}K → {l['avg_min']}-{l['avg_max']}K")
    rising = [s["skill"] for s in skills if s["delta"] > 0][:5]
    falling = [s["skill"] for s in skills if s["delta"] < 0][:3]
    if rising:
        report.trends.append("需求升温的技能：" + "、".join(rising))
    if falling:
        report.trends.append("热度回落的技能：" + "、".join(falling))
    return report


def advise(db, config: dict, config_path, profile: dict,
           matches: list[MatchResult]) -> AdvisorReport:
    if (config.get("llm") or {}).get("enabled"):
        llm_result = None
        try:
            llm_result = _llm_advisor(db, config, profile, matches)
        except Exception:
            llm_result = None
        if llm_result:
            return llm_result
    return fallback_advice(db, profile, matches)
