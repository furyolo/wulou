"""仅保存可追溯的分类结果，不保存浏览器登录凭据。"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


class ResultCache:
    """按“题目 + 当前题湖目录叶子”保存每个分类语境的最新结果。"""

    _TABLE = "classification_results"
    _LEGACY_TABLE = "classification_results_legacy_v1"
    _MANUAL_TABLE = "manual_classification_overrides"
    _MANUAL_AUDIT_TABLE = "manual_classification_audit"

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._lock = threading.RLock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """无损升级旧表，旧记录单独保留，避免猜测其原始三级、四级目录。"""
        with self._lock:
            exists = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (self._TABLE,)
            ).fetchone()
            if exists:
                columns = {
                    str(row[1]) for row in self._connection.execute(f"PRAGMA table_info({self._TABLE})")
                }
                if "source_catalogue_id" not in columns:
                    legacy_exists = self._connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (self._LEGACY_TABLE,)
                    ).fetchone()
                    if legacy_exists:
                        raise RuntimeError("发现未完成的缓存表迁移，请先处理 classification_results_legacy_v1")
                    self._connection.execute(f"ALTER TABLE {self._TABLE} RENAME TO {self._LEGACY_TABLE}")
            self._connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._TABLE} (
                    exercise_id TEXT NOT NULL,
                    source_catalogue_id TEXT NOT NULL,
                    stable_code TEXT,
                    cache_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (exercise_id, source_catalogue_id)
                )
                """
            )
            columns = {
                str(row[1]) for row in self._connection.execute(f"PRAGMA table_info({self._TABLE})")
            }
            if "stable_code" not in columns:
                self._connection.execute(f"ALTER TABLE {self._TABLE} ADD COLUMN stable_code TEXT")
            self._connection.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self._TABLE}_stable_code ON {self._TABLE}(stable_code)"
            )
            # 人工修正与模型缓存分表保存：规则或模型更新不能覆盖人工决定。
            self._connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._MANUAL_TABLE} (
                    exercise_id TEXT NOT NULL,
                    source_catalogue_id TEXT NOT NULL,
                    stable_code TEXT,
                    original_target_path_json TEXT NOT NULL,
                    target_path_json TEXT NOT NULL,
                    accepted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (exercise_id, source_catalogue_id)
                )
                """
            )
            self._connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._MANUAL_AUDIT_TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exercise_id TEXT NOT NULL,
                    source_catalogue_id TEXT NOT NULL,
                    stable_code TEXT,
                    original_target_path_json TEXT NOT NULL,
                    target_path_json TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._connection.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self._MANUAL_TABLE}_stable_code ON {self._MANUAL_TABLE}(stable_code)"
            )
            self._connection.commit()

    def get(self, cache_key: str, exercise_id: str, source_catalogue_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                f"""
                SELECT payload_json FROM {self._TABLE}
                WHERE exercise_id = ? AND source_catalogue_id = ? AND cache_key = ?
                """,
                (exercise_id, source_catalogue_id, cache_key),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def put(
        self,
        cache_key: str,
        exercise_id: str,
        source_catalogue_id: str,
        payload: dict[str, Any],
        stable_code: str = "",
    ) -> None:
        normalized_stable_code = str(stable_code).strip() or None
        with self._lock:
            self._connection.execute(
                f"""
                INSERT INTO {self._TABLE} (exercise_id, source_catalogue_id, stable_code, cache_key, payload_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(exercise_id, source_catalogue_id) DO UPDATE SET
                    stable_code = COALESCE(excluded.stable_code, stable_code),
                    cache_key = excluded.cache_key,
                    payload_json = excluded.payload_json,
                    created_at = CURRENT_TIMESTAMP
                """,
                (
                    exercise_id,
                    source_catalogue_id,
                    normalized_stable_code,
                    cache_key,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            self._connection.commit()

    def delete_by_exercise_ids(self, exercise_ids: list[str]) -> int:
        """删除指定题目在所有三级、四级目录语境中的缓存。"""
        normalized = sorted({str(item).strip() for item in exercise_ids if str(item).strip()})
        if not normalized:
            return 0
        placeholders = ",".join("?" for _ in normalized)
        with self._lock:
            cursor = self._connection.execute(
                f"DELETE FROM {self._TABLE} WHERE exercise_id IN ({placeholders})", normalized
            )
            self._connection.commit()
        return cursor.rowcount

    def get_manual_override(self, exercise_id: str, source_catalogue_id: str) -> dict[str, Any] | None:
        """读取当前目录语境下已经写入题湖的人工修正。"""
        with self._lock:
            row = self._connection.execute(
                f"""
                SELECT stable_code, original_target_path_json, target_path_json, accepted_at, updated_at
                FROM {self._MANUAL_TABLE}
                WHERE exercise_id = ? AND source_catalogue_id = ?
                """,
                (exercise_id, source_catalogue_id),
            ).fetchone()
        if not row:
            return None
        return {
            "stable_code": row[0],
            "original_target_path": json.loads(row[1]),
            "target_path": json.loads(row[2]),
            "accepted_at": row[3],
            "updated_at": row[4],
        }

    def put_manual_override(
        self,
        *,
        exercise_id: str,
        source_catalogue_id: str,
        stable_code: str,
        original_target_path: list[str],
        target_path: list[str],
    ) -> dict[str, Any]:
        """写入最新人工修正，并追加不可变审计事件。"""
        normalized_stable_code = stable_code.strip() or None
        original_json = json.dumps(original_target_path, ensure_ascii=False, separators=(",", ":"))
        target_json = json.dumps(target_path, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._connection.execute(
                f"""
                INSERT INTO {self._MANUAL_TABLE} (
                    exercise_id, source_catalogue_id, stable_code,
                    original_target_path_json, target_path_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(exercise_id, source_catalogue_id) DO UPDATE SET
                    stable_code = COALESCE(excluded.stable_code, stable_code),
                    original_target_path_json = excluded.original_target_path_json,
                    target_path_json = excluded.target_path_json,
                    accepted_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (exercise_id, source_catalogue_id, normalized_stable_code, original_json, target_json),
            )
            self._connection.execute(
                f"""
                INSERT INTO {self._MANUAL_AUDIT_TABLE} (
                    exercise_id, source_catalogue_id, stable_code,
                    original_target_path_json, target_path_json, event_type
                ) VALUES (?, ?, ?, ?, ?, 'accepted')
                """,
                (exercise_id, source_catalogue_id, normalized_stable_code, original_json, target_json),
            )
            self._connection.commit()
        override = self.get_manual_override(exercise_id, source_catalogue_id)
        if not override:
            raise RuntimeError("人工修正保存后无法读取")
        return override

    def close(self) -> None:
        with self._lock:
            self._connection.close()
