"""字段归一化：薪资、年限、学历、计数、技能词、紧急性、城市区县解析。

所有解析函数对空值/乱文本返回 None 或空列表，绝不抛错。
"""

from __future__ import annotations

import re

# 常见技术栈词典（大小写不敏感；短拉丁词用词边界匹配）
DEFAULT_SKILL_VOCAB: list[str] = [
    "Java", "Python", "Golang", "GO", "C++", "C#", "PHP", "Ruby", "Rust", "Swift",
    "Kotlin", "Scala", "JavaScript", "TypeScript", "Node.js", "Vue", "React",
    "Angular", "小程序", "Spring", "Spring Boot", "Spring Cloud", "MyBatis",
    "Netty", "Django", "Flask", "FastAPI", "MySQL", "Oracle", "PostgreSQL",
    "MongoDB", "Redis", "Memcached", "Elasticsearch", "ClickHouse", "HBase",
    "Kafka", "RabbitMQ", "RocketMQ", "Zookeeper", "Dubbo", "gRPC", "微服务",
    "分布式", "高并发", "高可用", "Docker", "Kubernetes", "K8s", "Jenkins",
    "Terraform", "Ansible", "Linux", "Shell", "Nginx", "Tomcat", "TCP", "HTTP",
    "数据结构", "算法", "多线程", "并发编程", "JVM", "GC", "性能优化", "架构设计",
    "DDD", "领域驱动", "中台", "分库分表", "读写分离", "消息队列", "缓存",
    "分布式事务", "分布式锁", "Service Mesh", "Istio", "DevOps", "SRE", "Prometheus",
    "Grafana", "SkyWalking", "ELK", "大数据", "Hadoop", "Spark", "Flink", "Hive",
    "数据仓库", "数据湖", "ETL", "数据建模", "机器学习", "深度学习", "强化学习",
    "NLP", "自然语言处理", "计算机视觉", "推荐系统", "LLM", "大模型", "AIGC",
    "RAG", "LangChain", "Agent", "Prompt", "PyTorch", "TensorFlow",
    "Transformer", "微调", "SFT", "AI", "云计算", "阿里云", "腾讯云", "AWS",
    "Serverless", "网络安全", "渗透测试", "区块链", "音视频", "WebRTC", "Flutter",
    "React Native", "iOS", "Android", "嵌入式", "自动化测试", "Selenium",
    "JMeter", "性能测试", "单元测试", "项目管理", "敏捷", "Scrum",
]

_EDU_ORDER = ["博士", "硕士", "研究生", "本科", "大专", "中专", "高中", "不限"]

_URGENT_TITLE_WORDS = ["急聘", "急招", "急寻", "火速", "速招", "加急"]
_URGENT_JD_WORDS = ["招满即止", "尽快到岗", "一周内到岗", "即刻到岗", "到岗越快越好"]


def _f(s: str) -> float:
    return float(s)


def parse_salary(text: str | None) -> tuple[float | None, float | None]:
    """解析为月薪区间（K）。支持：25-40K / 25-40K·16薪 / 2-4万 / 20-35万/年 /
    300-500元/天 / 25k-40k / 2万以上 / 面议。"""
    if not text:
        return None, None
    text = text.replace(" ", "").replace("～", "-").replace("–", "-").replace("~", "-")
    text = re.sub(r"(?<=\d)[kK](?=-)", "", text)  # 25k-40k → 25-40k
    if "面议" in text:
        return None, None

    # 日薪：300-500元/天 → 月薪按 21.75 工作日折算
    m = re.search(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)元/天", text)
    if m:
        lo, hi = _f(m.group(1)), _f(m.group(2))
        return round(lo * 21.75 / 1000, 1), round(hi * 21.75 / 1000, 1)

    # 通用区间：数值-数值 + 单位（万/K/千）+ 周期（/年）
    m = re.search(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)(万|千|[kK])?", text)
    if m:
        lo, hi, unit = _f(m.group(1)), _f(m.group(2)), m.group(3)
        factor = 1.0
        if unit == "万":
            factor = 10.0
        elif unit == "千":
            factor = 1.0
        elif unit in ("k", "K") or unit is None:
            factor = 1.0  # 无单位按 K（各站点月薪以 K 计最常见）
        lo, hi = lo * factor, hi * factor
        if unit == "万" and "/年" in text:
            lo, hi = round(lo / 12, 1), round(hi / 12, 1)
        elif unit == "万":
            lo, hi = round(lo, 1), round(hi, 1)
        if lo > hi:
            lo, hi = hi, lo
        return lo, hi

    # 单边：2万以上 / 25K以下 / 20K
    m = re.search(r"(\d+(?:\.\d+)?)(万|[kK])?(以[上下])?", text)
    if m:
        val, unit, side = _f(m.group(1)), m.group(2), m.group(3)
        if unit == "万":
            val *= 10
        if "/年" in text:
            val = round(val / 12, 1)
        if side == "以上":
            return round(val, 1), None
        if side == "以下":
            return None, round(val, 1)
        return round(val, 1), round(val, 1)
    return None, None


def parse_experience(text: str | None) -> tuple[float | None, float | None]:
    """解析年限区间。支持：3-5年 / 1年以下 / 5年以上 / 经验不限 / 应届生 / 3年。"""
    if not text:
        return None, None
    text = text.replace(" ", "")
    if any(w in text for w in ("不限", "无经验", "在校", "应届")):
        return None, None
    m = re.search(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)年", text)
    if m:
        return _f(m.group(1)), _f(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)年以上", text)
    if m:
        return _f(m.group(1)), None
    m = re.search(r"(\d+(?:\.\d+)?)年以下", text)
    if m:
        return 0.0, _f(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)年", text)
    if m:
        val = _f(m.group(1))
        return val, val
    return None, None


def parse_education(text: str | None) -> str | None:
    """归一化学历要求：研究生→硕士；未识别返回 None。"""
    if not text:
        return None
    if "硕士" in text or "研究生" in text:
        return "硕士"
    for token in ("博士", "本科", "大专", "中专", "高中", "不限"):
        if token in text:
            return token
    return None


def parse_count(text: str | None) -> int | None:
    """"1.2万"→12000；"876+"→876；"345人浏览"→345。"""
    if not text:
        return None
    text = text.replace(",", "").replace(" ", "")
    m = re.search(r"(\d+(?:\.\d+)?)(万|千)?", text)
    if not m:
        return None
    val = _f(m.group(1))
    unit = m.group(2)
    if unit == "万":
        val *= 10000
    elif unit == "千":
        val *= 1000
    return int(val)


def extract_skills(text: str, vocab: list[str] | None = None) -> list[str]:
    """从 JD 文本中抽取技能词（按词典顺序去重）。"""
    if not text:
        return []
    vocab = vocab if vocab is not None else DEFAULT_SKILL_VOCAB
    found: list[str] = []
    for term in vocab:
        if len(term) <= 3 and term.isascii():
            if re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE):
                found.append(term)
        elif term in text:
            found.append(term)
    return found


def detect_urgency(title: str | None, jd_text: str | None,
                   posted_text: str | None) -> tuple[str | None, str | None]:
    """紧急性：标题急聘词→high；JD 到岗紧迫词→high；2天内新发布→medium。"""
    title = title or ""
    jd = jd_text or ""
    posted = posted_text or ""
    for w in _URGENT_TITLE_WORDS:
        if w in title:
            return "high", f"标题含「{w}」"
    for w in _URGENT_JD_WORDS:
        if w in jd:
            return "high", f"JD含「{w}」"
    if re.search(r"(今天|昨天|1天|2天|\d+小时)", posted):
        return "medium", f"新发布（{posted}）"
    return None, None


def parse_city_district(text: str | None, home_city: str | None,
                        city_districts: dict | None) -> tuple[str | None, str | None]:
    """从 “苏州·工业园区” / “苏州市工业园区” / “工业园区” 等文本解析城市与区县。"""
    if not text:
        return None, None
    text = text.strip()
    city: str | None = None
    district: str | None = None
    if city_districts:
        for name in city_districts:
            if name and name in text:
                city = name
                break
        if city:
            for d in city_districts[city]:
                if d != "__city__" and d in text:
                    district = d
                    break
        elif home_city and home_city in city_districts:
            # 文本只有区名（如 “工业园区”）→ 按家乡城市匹配
            for d in city_districts[home_city]:
                if d != "__city__" and d in text:
                    return home_city, d
    if city is None and text:
        city = re.sub(r"(市|地区|自治州)$", "", text) if len(text) <= 6 else None
        if city and home_city and home_city not in city:
            pass
        elif city and home_city and home_city in city:
            city = home_city
    return city, district
