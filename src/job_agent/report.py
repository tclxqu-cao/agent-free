"""Markdown 日报生成。"""

from __future__ import annotations

from pathlib import Path

from .advisor import AdvisorReport
from .analysis import (count_trend, distribution_shift, hot_jobs, keyword_trend,
                       new_and_missing, requirement_drift, salary_trend, skill_trend)
from .match import MatchResult


def _table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) if c is not None else "—" for c in r) + " |")
    return "\n".join(out)


def _fmt_dist(shift: dict) -> str:
    keys = sorted(set(shift["cur"]) | set(shift["prev"]),
                  key=lambda k: -(shift["cur"].get(k, 0)))
    rows = [[k, f"{shift['cur'].get(k, 0)}%", f"{shift['prev'].get(k, 0)}%",
             f"{round(shift['cur'].get(k, 0) - shift['prev'].get(k, 0), 1):+}"]
            for k in keys]
    return _table(["类别", "本周占比", "上周占比", "变化"], rows)


def write_daily_report(out_dir: Path, day: str, matches: list[MatchResult],
                       advisor: AdvisorReport, run_stats: dict | None = None,
                       db=None, config: dict | None = None) -> Path:
    passed = [m for m in matches if m.passed]
    failed = [m for m in matches if not m.passed]
    lines: list[str] = [f"# 岗位日报 · {day}", ""]

    lines += ["## 一、今日匹配岗位", ""]
    if passed:
        rows = []
        for m in passed[:30]:
            dist = m.job.raw.get("distance_km")
            rows.append([
                m.job.title, m.job.company, m.job.salary_text or "—",
                m.job.experience_text or "—", m.job.education or "—",
                f"{m.job.city or ''}{('·' + m.job.district) if m.job.district else ''}" or "—",
                f"{dist} km" if dist is not None else "—",
                {"high": "🔥高", "medium": "中"}.get(m.job.urgency, "—"),
                m.job.views if m.job.views is not None else "—",
                m.job.greets if m.job.greets is not None else "—",
                m.job.company_size or "—",
                f"{m.score:.0f}",
            ])
        lines += [_table(
            ["岗位", "公司", "薪资", "年限", "学历", "地点", "距家",
             "紧急", "浏览", "招呼", "规模", "评分"], rows), ""]
        lines += ["### 岗位职责与特殊要求（前 10）", ""]
        for m in passed[:10]:
            lines.append(f"**{m.job.title} @ {m.job.company}**（{m.job.url}）")
            if m.job.responsibilities:
                lines.append("- 职责：" + "；".join(m.job.responsibilities[:5]))
            if m.job.requirements_extra:
                lines.append("- 要求：" + "；".join(m.job.requirements_extra[:5]))
            if advisor.match_notes.get(m.job.job_id):
                lines.append(f"- 点评：{advisor.match_notes[m.job.job_id]}")
            lines.append("")
    else:
        lines += ["今日无匹配岗位。", ""]

    if db is not None:
        count = count_trend(db)
        salary = salary_trend(db)
        skills = skill_trend(db)
        kw = keyword_trend(db)
        exclude = ((config or {}).get("rules") or {}).get("exclude_keywords")
        hot = hot_jobs(db, exclude=exclude)
        new, missing = new_and_missing(db, day)

        lines += ["## 二、岗位数量趋势", "",
                  _table(["日期", "活跃", "新增", "下架"],
                         [[c["date"], c["active"], c["new"], c["missing"]]
                          for c in count[-14:]]), ""]
        if kw:
            lines += ["### 关键词结果数趋势", ""]
            lines += [_table(
                ["关键词", "最早总数", "最新总数", "变化"],
                [[k["keyword"],
                  k["series"][0]["total"] if k["series"] else "—",
                  k["series"][-1]["total"] if k["series"] else "—",
                  (k["series"][-1]["total"] or 0) - (k["series"][0]["total"] or 0)
                  if k["series"] else "—"] for k in kw]), ""]

        lines += ["## 三、岗位要求变化", "", "### 薪资趋势", "",
                  _table(["日期", "平均下限K", "平均上限K"],
                         [[s["date"], s["avg_min"], s["avg_max"]] for s in salary[-14:]]),
                  "", "### 学历要求分布", "", _fmt_dist(distribution_shift(db, "education")),
                  "", "### 年限要求分布", "", _fmt_dist(distribution_shift(db, "experience")),
                  "", "### 技能热度（本周 vs 上周）", "",
                  _table(["技能", "本周岗位数", "上周岗位数", "变化"],
                         [[s["skill"], s["cur"], s["prev"], f"{s['delta']:+}"]
                          for s in skills[:15]]), ""]

        if hot:
            lines += ["## 四、岗位热度变化（近7天增量）", "",
                      _table(["岗位", "公司", "浏览增量", "招呼增量"],
                             [[h["title"], h["company"], h["views_delta"],
                               h["greets_delta"]] for h in hot]), ""]

        lines += ["## 五、新增 / 下架岗位（今日）", ""]
        lines.append(f"新增 {len(new)} 个：" +
                     ("；".join(f"{j.title}@{j.company}" for j in new[:15]) or "无"))
        lines.append("")
        lines.append(f"下架 {len(missing)} 个：" +
                     ("；".join(f"{j.title}@{j.company}" for j in missing[:15]) or "无"))
        lines.append("")

    lines += ["## 六、学习建议（结合你的经历）", ""]
    lines += [f"- {x}" for x in advisor.learning] or ["- 今日无新增缺口建议。"]
    lines += ["", "## 七、更上一层（进阶建议）", ""]
    lines += [f"- {x}" for x in advisor.level_up] or ["- 数据积累中，暂无进阶分析。"]
    lines += ["", "## 八、未来趋势展望", ""]
    lines += [f"- {x}" for x in advisor.trends] or ["- 数据积累中，暂无趋势判断。"]
    lines += ["", f"---\n*匹配池 {len(matches)} 条（通过 {len(passed)} / 淘汰 {len(failed)}）"
              f" · 建议来源：{'LLM' if advisor.llm_used else '规则引擎'}*"]

    if run_stats:
        errors = run_stats.get("errors") or []
        lines += ["", "## 九、抓取状态", ""]
        if errors:
            lines += [f"- ⚠️ {e['site']}「{e['keyword']}」：{e['error']}" for e in errors]
        else:
            lines.append(f"- 全部正常，共抓取 {run_stats.get('total_jobs', 0)} 条")

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
