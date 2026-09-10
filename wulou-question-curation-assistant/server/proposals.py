"""新目录候选汇总；任何候选都不会直接修改目录或 Excel。"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


class ProposalStore:
    def __init__(self, database_path: Path, minimum: int) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(database_path, check_same_thread=False); self.minimum = minimum; self._lock = threading.RLock()
        self.db.execute("CREATE TABLE IF NOT EXISTS proposal_evidence (scope_key TEXT, cluster_key TEXT, kind TEXT, title TEXT, parent_level3_id TEXT, exercise_id TEXT, reason TEXT, PRIMARY KEY(scope_key,cluster_key,exercise_id))")
        self.db.commit()

    def record(self, question: dict[str, Any], result: dict[str, Any]) -> None:
        proposal = result.get("proposal") or {}
        if not result.get("proposal_required") or not proposal.get("cluster_key"): return
        scope = question.get("scope") or {}; scope_key = f"{scope.get('topic_id','')}|{scope.get('level2_id','')}"
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO proposal_evidence VALUES (?,?,?,?,?,?,?)", (scope_key, str(proposal["cluster_key"]), str(proposal.get("kind", "")), str(proposal.get("title") or ""), str(proposal.get("parent_level3_id") or ""), str(question["exercise_id"]), str(proposal.get("reason") or result.get("reason", ""))))
            self.db.commit()

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute("SELECT scope_key,cluster_key,kind,title,parent_level3_id,COUNT(*),GROUP_CONCAT(exercise_id),GROUP_CONCAT(reason, '\\n') FROM proposal_evidence GROUP BY scope_key,cluster_key,kind,title,parent_level3_id ORDER BY COUNT(*) DESC").fetchall()
        return [{"proposal_id": f"{row[0]}:{row[1]}", "scope_key": row[0], "cluster_key": row[1], "kind": row[2], "proposed_title": row[3] or None, "parent_level3_id": row[4] or None, "distinct_question_count": row[5], "exercise_ids": row[6].split(","), "evidence": row[7].split("\n"), "status": "candidate" if row[5] >= self.minimum else "observe"} for row in rows]

    def close(self) -> None:
        with self._lock: self.db.close()
