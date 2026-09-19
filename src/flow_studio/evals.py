"""Agent 评测中心：标准问题集 × 多目标执行 → 自动校验 + 步骤对比。

- 评测集（suite）：标准问题 + 期望（contains / not_contains / regex / equals /
  可选 LLM 评判标准），JSON 存 data/evals/suites/。
- 目标（target）：platform=平台智能体；cli=任意命令行 agent（codex exec、
  claude -p 等，{prompt} 占位）；http=任意 HTTP chat 端点。
- 运行记录：逐用例保存答案、执行步骤（平台目标有完整工具轨迹）、校验明细、
  耗时；两次 run 可逐用例对比，定位哪个目标在哪类问题上出问题。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import uuid
from pathlib import Path

import httpx

SNIPPET = 200          # 对比视图里答案摘要长度
CASE_TIMEOUT = 300     # 单用例默认超时秒


# ---------------------------------------------------------------- 校验
def run_checks(answer: str, expect: dict, llm_judge=None) -> list[dict]:
    """按期望逐条校验。llm_judge(fn(criteria, answer)->(score, reason)) 可选。"""
    checks: list[dict] = []
    text = str(answer or "")
    low = text.lower()

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:200]})

    for kw in expect.get("contains") or []:
        add(f"包含[{kw}]", str(kw).lower() in low,
            "" if str(kw).lower() in low else "答案中未找到该关键词")
    for kw in expect.get("not_contains") or []:
        add(f"不含[{kw}]", str(kw).lower() not in low,
            "" if str(kw).lower() not in low else "答案中出现了不该出现的内容")
    if expect.get("regex"):
        try:
            ok = bool(re.search(expect["regex"], text))
        except re.error as e:
            add("正则", False, f"正则非法：{e}")
        else:
            add(f"正则[{expect['regex']}]", ok, "" if ok else "答案未匹配该模式")
    if expect.get("equals"):
        add("全等", text.strip() == str(expect["equals"]).strip())
    if expect.get("llm_criteria") and llm_judge:
        try:
            score, reason = llm_judge(expect["llm_criteria"], text)
        except Exception as e:  # noqa: BLE001
            add("LLM评判", False, f"评判失败：{e}")
        else:
            add(f"LLM评判≥{expect.get('llm_min_score', 60)}分",
                score >= int(expect.get("llm_min_score", 60)),
                f"{score}分：{reason}")
    if not checks:
        add("非空", bool(text.strip()), "期望为空时仅要求答案非空")
    return checks


# ---------------------------------------------------------------- 目标执行
class TargetRunner:
    """把三类目标统一成 execute(target, question) -> {answer, steps, error}。"""

    def __init__(self, agent_rt=None, agent_store=None):
        self.agent_rt = agent_rt
        self.agent_store = agent_store

    def execute(self, target: dict, question: str,
                session_id: str | None = None) -> dict:
        ttype = target.get("type")
        try:
            if ttype == "platform":
                return self._platform(target, question, session_id)
            if ttype == "cli":
                return self._cli(target, question)
            if ttype == "http":
                return self._http(target, question)
            return {"answer": "", "steps": [], "error": f"未知目标类型：{ttype}"}
        except subprocess.TimeoutExpired:
            return {"answer": "", "steps": [],
                    "error": f"超时（{float(target.get('timeout') or CASE_TIMEOUT)}s）"}
        except Exception as e:  # noqa: BLE001 单目标失败不拖垮整场评测
            return {"answer": "", "steps": [], "error": f"{type(e).__name__}: {e}"}

    def _platform(self, target: dict, question: str,
                  session_id: str | None) -> dict:
        if self.agent_rt is None or self.agent_store is None:
            return {"answer": "", "steps": [], "error": "智能体运行时未初始化"}
        agent = self.agent_store.get(str(target.get("id") or ""))
        if agent is None:
            return {"answer": "", "steps": [], "error": f"智能体不存在：{target.get('id')}"}
        out = self.agent_rt.run(agent, question, session_id=session_id)
        return {"answer": out.get("text", ""), "steps": out.get("steps", []),
                "error": out.get("error")}

    def _cli(self, target: dict, question: str) -> dict:
        command = str(target.get("command") or "").strip()
        if not command:
            return {"answer": "", "steps": [], "error": "缺少 command"}
        args = [str(a).replace("{prompt}", question)
                for a in (target.get("args") or ["{prompt}"])]
        timeout = float(target.get("timeout") or CASE_TIMEOUT)
        proc = subprocess.run([command, *args], capture_output=True, text=True,
                              timeout=timeout)  # noqa: S603 目标由用户在面板配置
        answer = (proc.stdout or "").strip()
        err = None
        if proc.returncode != 0:
            err = f"退出码 {proc.returncode}：{(proc.stderr or '')[-300:]}"
        return {"answer": answer or (proc.stderr or "").strip(),
                "steps": [], "error": err}

    def _http(self, target: dict, question: str) -> dict:
        url = str(target.get("url") or "").strip()
        if not url:
            return {"answer": "", "steps": [], "error": "缺少 url"}
        resp = httpx.post(url, json={"message": question},
                          headers=target.get("headers") or {},
                          timeout=float(target.get("timeout") or CASE_TIMEOUT))
        resp.raise_for_status()
        data = resp.json()
        for key in ("text", "output", "reply", "answer", "content"):
            if isinstance(data, dict) and isinstance(data.get(key), str):
                return {"answer": data[key], "steps": [], "error": None}
        if isinstance(data, dict) and data.get("choices"):
            msg = data["choices"][0].get("message") or {}
            return {"answer": msg.get("content") or "", "steps": [], "error": None}
        return {"answer": resp.text[:5000], "steps": [], "error": None}


# ---------------------------------------------------------------- 存储
class EvalStore:
    def __init__(self, base: Path):
        self.base = Path(base)
        self.suites_dir = self.base / "suites"
        self.runs_dir = self.base / "runs"
        self.suites_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    # ---- 评测集
    def list_suites(self) -> list[dict]:
        out = []
        for p in sorted(self.suites_dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                data["cases"] = len(data.get("cases") or [])
                out.append(data)
            except Exception:  # noqa: BLE001
                continue
        return out

    def get_suite(self, suite_id: str) -> dict | None:
        p = self.suites_dir / f"{suite_id}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def seed_if_empty(self, suite: dict) -> int:
        if any(self.suites_dir.glob("*.json")):
            return 0
        self.save_suite(suite)
        return 1

    def save_suite(self, data: dict) -> dict:
        sid = "".join(c for c in str(data.get("id") or "").strip()
                      if c.isalnum() or c in "-_")
        if not sid:
            raise ValueError("评测集 id 只允许字母数字-_")
        merged = {"id": sid,
                  "name": str(data.get("name") or sid),
                  "description": str(data.get("description") or ""),
                  "cases": list(data.get("cases") or []),
                  "updated_at": dt.datetime.now().isoformat(timespec="seconds")}
        for c in merged["cases"]:
            if not isinstance(c, dict) or not str(c.get("question") or "").strip():
                raise ValueError("用例缺少 question")
            c.setdefault("id", uuid.uuid4().hex[:8])
            c.setdefault("expect", {})
            c.setdefault("note", "")
        (self.suites_dir / f"{sid}.json").write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        return merged

    def delete_suite(self, suite_id: str) -> bool:
        p = self.suites_dir / f"{suite_id}.json"
        if not p.exists():
            return False
        p.unlink()
        return True

    # ---- 运行记录
    def save_run(self, run: dict) -> dict:
        (self.runs_dir / f"{run['run_id']}.json").write_text(
            json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
        return run

    def list_runs(self, suite_id: str | None = None, limit: int = 30) -> list[dict]:
        runs = []
        for p in sorted(self.runs_dir.glob("*.json"),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if suite_id and data.get("suite_id") != suite_id:
                continue
            targets = []
            for t in data.get("targets") or []:
                passed = sum(1 for r in data.get("results", [])
                             if r["target_key"] == t["key"] and r["pass"])
                total = sum(1 for r in data.get("results", [])
                            if r["target_key"] == t["key"])
                targets.append({"key": t["key"], "name": t.get("name", t["key"]),
                                "passed": passed, "total": total})
            runs.append({"run_id": data["run_id"], "suite_id": data.get("suite_id"),
                         "started_at": data.get("started_at"), "targets": targets})
            if len(runs) >= limit:
                break
        return runs

    def get_run(self, run_id: str) -> dict | None:
        p = self.runs_dir / f"{run_id}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


DEFAULT_EVAL_TARGETS = [
    {"key": "codex", "type": "cli", "name": "Codex CLI",
     "command": "codex", "args": ["exec", "--skip-git-repo-check", "{prompt}"]},
    {"key": "claude", "type": "cli", "name": "Claude CLI",
     "command": "claude", "args": ["-p", "{prompt}"]},
]


# ---------------------------------------------------------------- 编排
def run_suite(suite: dict, targets: list[dict], store: EvalStore,
              runner: TargetRunner, llm_judge=None) -> dict:
    """执行整场评测：每个用例 × 每个目标，逐条校验并落盘。"""
    norm_targets = []
    for t in targets:
        t = dict(t)
        t["key"] = str(t.get("key") or t.get("type") or "target")
        if t["type"] == "platform" and t.get("id"):
            t["key"] = f"platform:{t['id']}"
        t.setdefault("name", t["key"])
        norm_targets.append(t)

    run_id = uuid.uuid4().hex[:12]
    results: list[dict] = []
    for target in norm_targets:
        for case in suite.get("cases") or []:
            t0 = dt.datetime.now()
            out = runner.execute(target, case["question"],
                                 session_id=f"eval-{run_id}")
            answer = out.get("answer") or ""
            checks = (run_checks(answer, case.get("expect") or {}, llm_judge)
                      if not out.get("error") else [])
            results.append({
                "target_key": target["key"], "target_name": target.get("name"),
                "case_id": case["id"], "question": case["question"],
                "answer": answer[:20000], "steps": out.get("steps") or [],
                "checks": checks,
                "pass": bool(checks) and all(c["ok"] for c in checks),
                "error": out.get("error"),
                "ms": int((dt.datetime.now() - t0).total_seconds() * 1000)})
    return store.save_run({
        "run_id": run_id, "suite_id": suite["id"], "suite_name": suite.get("name"),
        "targets": norm_targets, "results": results,
        "started_at": dt.datetime.now().isoformat(timespec="seconds")})


def compare_runs(run_a: dict, run_b: dict) -> dict:
    """逐用例对比两次运行：通过率、答案差异、仅一方失败的位置。"""
    suite_id = run_a.get("suite_id")
    questions = {r["case_id"]: r["question"] for r in run_a.get("results", [])}
    questions.update({r["case_id"]: r["question"] for r in run_b.get("results", [])})
    by = {}
    for r in (*run_a.get("results", []), *run_b.get("results", [])):
        by[(r["target_key"], r["case_id"])] = r

    keys_a = [t["key"] for t in run_a.get("targets", [])]
    keys_b = [t["key"] for t in run_b.get("targets", [])]
    rows = []
    for case_id, question in questions.items():
        row = {"case_id": case_id, "question": question, "cells": []}
        for key in dict.fromkeys([*keys_a, *keys_b]):
            r = by.get((key, case_id))
            row["cells"].append({
                "target_key": key, "pass": bool(r and r["pass"]),
                "error": r.get("error") if r else "未执行",
                "answer": (r or {}).get("answer", "")[:SNIPPET],
                "ms": (r or {}).get("ms", 0)})
        # 定位问题：同一用例两目标结论不一致时标记
        passes = [c["pass"] for c in row["cells"]]
        row["diff"] = len(set(passes)) > 1
        rows.append(row)
    return {"suite_id": suite_id,
            "run_a": run_a.get("run_id"), "run_b": run_b.get("run_id"),
            "rows": rows,
            "diff_count": sum(1 for r in rows if r["diff"])}
