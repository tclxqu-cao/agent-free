"""demo 模式：确定性模拟数据走通全链路（无需招聘网站凭据）。"""

from __future__ import annotations

import random
from datetime import date, timedelta

from .db import DB
from .distance import Geocoder, compute_distance
from .models import Job
from .normalize import DEFAULT_SKILL_VOCAB, extract_skills

# (标题, 公司, 薪资min,薪资max, 经验min,经验max, 学历, 技能, 趋势: java/java-arch/ai)
_TEMPLATES = [
    ("Java 开发工程师", "苏州数科信息", 18, 28, 3, 5, "本科",
     ["Java", "Spring Boot", "MySQL", "Redis", "微服务"]),
    ("高级 Java 工程师", "云途科技", 25, 40, 5, 8, "本科",
     ["Java", "Spring Cloud", "Kafka", "Redis", "分布式", "高并发"]),
    ("Java 架构师", "金鸡湖软件", 35, 55, 8, None, "本科",
     ["Java", "架构设计", "DDD", "分库分表", "Kubernetes", "JVM"]),
    ("AI 应用开发工程师", "智核 AI", 28, 45, 3, 6, "本科",
     ["Python", "LLM", "RAG", "LangChain", "Agent", "FastAPI"]),
    ("大模型算法工程师", "深蓝智能", 35, 60, 3, 8, "硕士",
     ["Python", "PyTorch", "Transformer", "大模型", "微调", "SFT"]),
    ("Golang 后端工程师", "极氪数据", 25, 38, 3, 6, "不限",
     ["Golang", "Kafka", "Kubernetes", "MySQL", "微服务"]),
    ("外包 Java 工程师（驻场）", "软通动力", 12, 18, 2, 4, "大专",
     ["Java", "MySQL"]),
    ("资深后端专家", "同程旅行", 40, 65, 8, None, "本科",
     ["Java", "架构设计", "高并发", "分布式事务", "消息队列", "性能优化"]),
    ("技术经理（后端方向）", "企微云", 38, 58, 8, 12, "本科",
     ["Java", "Spring Boot", "Kubernetes", "项目管理", "架构设计", "敏捷"]),
    ("数据平台工程师", "麦禾科技", 22, 35, 3, 6, "本科",
     ["Spark", "Flink", "Hive", "数据仓库", "Kafka"]),
]

_DISTRICTS = ["工业园区", "高新区", "姑苏区", "吴中区"]
_SIZES = ["150-500人", "500-999人", "1000-9999人", "10000人以上"]


def seed_demo_data(db: DB, config: dict, days: int = 10,
                   per_day: int | None = None) -> int:
    """生成 days 天确定性模拟数据（随机种子 42），含薪资上行、Java 缩减/AI 增长、
    部分岗位中途下架、浏览/招呼量日增。返回写入的 (岗位, 天) 次数。"""
    rng = random.Random(42)
    home = config.get("home") or {}
    geocoder = Geocoder(db, home_city=home.get("city"), offline=True)
    today = date.today()
    writes = 0

    for d_off in range(days - 1, -1, -1):
        day = (today - timedelta(days=d_off)).isoformat()
        progress = (days - 1 - d_off) / max(days - 1, 1)  # 0 → 1
        for idx, (title, company, smin, smax, emin, emax, edu, skills) in enumerate(_TEMPLATES):
            # 岗位 #6 中途下架；#9 只在最新一天出现（新增岗位样本）
            if idx == 6 and d_off < days - 4:
                continue
            if idx == 9 and d_off > 0:
                continue
            # Java 基础岗数量收缩 / AI 岗位扩张 → 用概率控制出现
            if idx == 0 and rng.random() < 0.4 * progress:
                continue
            if idx in (3, 4) and rng.random() < 0.2 * (1 - progress):
                continue
            drift = 1 + 0.04 * progress * (2 if idx in (3, 4) else 1)
            salary_min = round(smin * drift, 1)
            salary_max = round(smax * drift, 1)
            district = _DISTRICTS[idx % len(_DISTRICTS)]
            jd_text = (f"岗位职责：负责{title}相关工作，参与核心系统设计与开发；"
                       f"任职要求：掌握 {'/'.join(skills)}，具备良好沟通能力。")
            job = Job(
                site=("demo-boss" if idx % 2 == 0 else "demo-liepin"),
                title=title, company=company,
                url=f"https://demo.example.com/job/{idx}",
                company_size=_SIZES[idx % len(_SIZES)],
                industry="互联网/软件",
                salary_text=f"{salary_min:.0f}-{salary_max:.0f}K·{14 + idx % 3}薪",
                salary_min=salary_min, salary_max=salary_max,
                city=home.get("city", "苏州"), district=district,
                address=f"{home.get('city', '苏州')}{district}智选路{10 + idx}号",
                experience_text=(f"{emin}-{emax}年" if emax else f"{emin}年以上"),
                experience_min=emin, experience_max=emax,
                education=edu,
                responsibilities=[f"负责{title}相关的系统设计与开发",
                                  "参与技术方案评审与攻关"],
                requirements_extra=[f"熟练掌握 {'、'.join(skills[:4])}",
                                    "具备良好的沟通与协作能力"],
                skills=skills + extract_skills(jd_text, DEFAULT_SKILL_VOCAB),
                urgency=("high" if idx in (1, 7) else None),
                urgency_reason=("标题含「急聘」" if idx in (1, 7) else None),
                views=100 + idx * 50 + (days - d_off) * 37 + rng.randint(0, 20),
                greets=10 + idx * 5 + (days - d_off) * 4 + rng.randint(0, 5),
                raw={"distance_basis": "address"},
            )
            # 同一岗位多天共享 job_id → 用固定 id 模拟同一岗位的每日快照
            import hashlib
            job.job_id = hashlib.sha1(f"demo|{idx}".encode()).hexdigest()[:16]
            dist = compute_distance(job, geocoder, home)
            db.upsert_job(job, dist, day)
            writes += 1
        for kw, base in (("Java", 300), ("Java 架构师", 40), ("AI 应用开发", 60)):
            trend = base * (1 - 0.3 * progress) if kw.startswith("Java") \
                else base * (1 + 0.8 * progress)
            db.record_keyword(day, kw, "demo", int(trend + rng.randint(-10, 10)))
        # 每天结束都做消失检测（模拟真实每日运行）
        db.mark_missing(day)
    return writes
