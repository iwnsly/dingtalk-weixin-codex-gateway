from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Callable


def _read_unlocked(path: Path) -> list[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        return value if isinstance(value, list) else []
    except (OSError, ValueError):
        return []


def load_jobs(path: Path) -> list[dict]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        return _read_unlocked(path)


def mutate_jobs(path: Path, mutate: Callable[[list[dict]], None]) -> list[dict]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        jobs = _read_unlocked(path)
        mutate(jobs)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        return jobs


def update_job(path: Path, job_id: str, values: dict, remove: tuple[str, ...] = ()) -> bool:
    found = False

    def apply(jobs: list[dict]) -> None:
        nonlocal found
        for job in jobs:
            if str(job.get("id", "")) != job_id:
                continue
            job.update(values)
            for key in remove:
                job.pop(key, None)
            found = True
            break

    mutate_jobs(path, apply)
    return found


def claim_job(path: Path, job_id: str, *, trigger: str, run_at: str, today: str) -> dict | None:
    claimed: dict | None = None

    def claim(jobs: list[dict]) -> None:
        nonlocal claimed
        for job in jobs:
            if str(job.get("id", "")) != job_id:
                continue
            if trigger == "manual":
                if not job.get("run_requested_at"):
                    return
            else:
                if job.get("run_requested_at") or not job.get("enabled", True) or job.get("last_sent_date") == today:
                    return
            job.pop("run_requested_at", None)
            job.pop("last_error", None)
            job.update({
                "last_status": "running",
                "last_run_at": run_at,
                "last_trigger": trigger,
            })
            claimed = dict(job)
            return

    mutate_jobs(path, claim)
    return claimed


def build_prompt(job: dict, today: str) -> str:
    custom = str(job.get("prompt", "")).strip()
    if custom:
        return custom.replace("{date}", today)
    if job.get("type") != "daily_fortune":
        raise ValueError(f"不支持的定时任务类型：{job.get('type') or '未配置'}")
    return f"""请为黄克生成 {today} 的今日运势并直接给出可发送给用户的中文正文。
依据：公历 1982 年 7 月 13 日 00:30，出生地河南开封。时区使用 Asia/Shanghai。
要求：结合传统八字/民俗角度，包含整体、事业、财运、健康、人际、幸运色、幸运数字和今日建议；控制在 500 字以内，表达具体、克制，不要声称能够科学预测，不要提供医疗、投资等高风险确定性结论。开头写“今日运势｜{today}”，结尾注明“仅供民俗文化参考”。不要解释生成过程。"""
