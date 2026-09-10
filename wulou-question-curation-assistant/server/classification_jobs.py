"""浏览器实时分类作业的内存状态台账。

题目原文仍只停留在本机内存；已完成的最终结果会由 ResultCache 持久化。
服务重启后未完成作业会消失，但不会影响已经写入缓存的结果。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class ClassificationJob:
    job_id: str
    questions: list[dict[str, Any]]
    status: str = "queued"
    stage: str = "queued"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    routing_completed: int = 0
    result_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    failed_exercise_ids: list[str] = field(default_factory=list)
    error: str | None = None


class ClassificationJobStore:
    """线程安全的短期作业状态；前端只需轮询一个 job_id。"""

    def __init__(self) -> None:
        self._jobs: dict[str, ClassificationJob] = {}
        self._lock = threading.RLock()

    def create(self, questions: list[dict[str, Any]]) -> ClassificationJob:
        job = ClassificationJob(job_id=f"classify-{uuid.uuid4().hex}", questions=list(questions))
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> ClassificationJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise ValueError("未找到分类作业；服务重启后请重新提交当前页")
            return job

    def set_running(self, job_id: str, stage: str) -> None:
        with self._lock:
            job = self.get(job_id)
            job.status = "running"
            job.stage = stage
            job.updated_at = datetime.now(timezone.utc).isoformat()

    def set_stage(self, job_id: str, stage: str, routing_completed: int | None = None) -> None:
        with self._lock:
            job = self.get(job_id)
            job.stage = stage
            if routing_completed is not None:
                job.routing_completed = routing_completed
            job.updated_at = datetime.now(timezone.utc).isoformat()

    def record_result(self, job_id: str, result: dict[str, Any], *, failed: bool = False) -> None:
        exercise_id = str(result.get("exercise_id", "")).strip()
        if not exercise_id:
            raise ValueError("分类结果缺少 exercise_id")
        with self._lock:
            job = self.get(job_id)
            job.result_by_id[exercise_id] = dict(result)
            if failed and exercise_id not in job.failed_exercise_ids:
                job.failed_exercise_ids.append(exercise_id)
            job.updated_at = datetime.now(timezone.utc).isoformat()

    def complete(self, job_id: str) -> None:
        with self._lock:
            job = self.get(job_id)
            job.status = "completed"
            job.stage = "completed"
            job.updated_at = datetime.now(timezone.utc).isoformat()

    def fail(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self.get(job_id)
            job.status = "failed"
            job.stage = "failed"
            job.error = error[:500]
            job.updated_at = datetime.now(timezone.utc).isoformat()

    def snapshot(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self.get(job_id)
            ordered_results = [
                dict(job.result_by_id[str(question["exercise_id"])])
                for question in job.questions
                if str(question["exercise_id"]) in job.result_by_id
            ]
            return {
                "job_id": job.job_id,
                "status": job.status,
                "stage": job.stage,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
                "total": len(job.questions),
                "completed": len(ordered_results),
                "pending": len(job.questions) - len(ordered_results),
                "routing_completed": job.routing_completed,
                "failed_exercise_ids": list(job.failed_exercise_ids),
                "error": job.error,
                "results": ordered_results,
            }
