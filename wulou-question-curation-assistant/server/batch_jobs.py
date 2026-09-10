"""本地 Batch 作业台账；原题 JSONL 只保留在 Git 忽略的本地目录。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any


class BatchStore:
    def __init__(self, root: Path) -> None:
        self.root = root; self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "batch-jobs.sqlite3", check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS batch_jobs (job_id TEXT PRIMARY KEY, provider_batch_id TEXT, status TEXT NOT NULL, input_file TEXT NOT NULL, result_file TEXT, metadata_json TEXT NOT NULL)")
        self.db.commit()

    def create(self, jsonl: str, questions: list[dict[str, Any]]) -> dict[str, Any]:
        job_id = f"batch-{uuid.uuid4().hex}"
        input_file = self.root / f"{job_id}.jsonl"
        with input_file.open("w", encoding="utf-8", newline="\n") as file: file.write(jsonl)
        metadata = {"job_id": job_id, "status": "exported", "question_count": len(questions), "exercise_ids": [str(q["exercise_id"]) for q in questions], "questions": questions}
        self.db.execute("INSERT INTO batch_jobs VALUES (?, NULL, ?, ?, NULL, ?)", (job_id, "exported", str(input_file), json.dumps(metadata, ensure_ascii=False)))
        self.db.commit(); return metadata

    def get(self, job_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT provider_batch_id,status,input_file,result_file,metadata_json FROM batch_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row: raise ValueError("未找到批处理任务")
        data = json.loads(row[4]); data.update({"provider_batch_id": row[0], "status": row[1], "input_file": row[2], "result_file": row[3]}); return data

    def update(self, job_id: str, *, status: str, provider_batch_id: str | None = None, result_file: str | None = None) -> None:
        current = self.get(job_id)
        self.db.execute("UPDATE batch_jobs SET status=?, provider_batch_id=?, result_file=? WHERE job_id=?", (status, provider_batch_id or current.get("provider_batch_id"), result_file or current.get("result_file"), job_id)); self.db.commit()

    def input_bytes(self, job_id: str) -> bytes: return Path(self.get(job_id)["input_file"]).read_bytes()
    def save_result(self, job_id: str, data: bytes) -> str:
        path = self.root / f"{job_id}.result.jsonl"; path.write_bytes(data); self.update(job_id, status="completed", result_file=str(path)); return str(path)
    def close(self) -> None: self.db.close()
