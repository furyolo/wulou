"""仅保存可追溯的分类结果，不保存浏览器登录凭据。"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any


class ResultCache:
    """按“题目 + 当前题湖目录叶子”保存每个分类语境的最新结果。"""

    _TABLE = "classification_results"
    _LEGACY_TABLE = "classification_results_legacy_v1"
    _MANUAL_TABLE = "manual_classification_overrides"
    _MANUAL_AUDIT_TABLE = "manual_classification_audit"
    _MOVE_HISTORY_TABLE = "catalogue_move_history"

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
            self._connection.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self._MANUAL_TABLE}_exercise_updated "
                f"ON {self._MANUAL_TABLE}(exercise_id, updated_at DESC)"
            )
            # 工作成果只记录题湖已经回读确认的真实移动，独立于可清除的模型缓存。
            self._connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._MOVE_HISTORY_TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exercise_id TEXT NOT NULL,
                    stable_code TEXT,
                    source_catalogue_id TEXT NOT NULL,
                    target_catalogue_id TEXT NOT NULL,
                    original_path_json TEXT NOT NULL,
                    target_path_json TEXT NOT NULL,
                    moved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._connection.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self._MOVE_HISTORY_TABLE}_exercise_moved "
                f"ON {self._MOVE_HISTORY_TABLE}(exercise_id, moved_at DESC, id DESC)"
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
            # 分类结论由题目内容、目录版本和规则版本决定；题目移入目标叶子后，
            # 仍应复用同一结论，不能因来源目录变化再次请求模型。
            if not row:
                row = self._connection.execute(
                    f"""
                    SELECT payload_json FROM {self._TABLE}
                    WHERE exercise_id = ? AND cache_key = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (exercise_id, cache_key),
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
        """读取人工修正：当前目录优先，其次是同题最新人工决定。

        题目移动到人工指定的目标叶子后，当前目录 ID 会变成目标目录 ID，
        不能因此退回旧的模型缓存。若同题在当前目录没有记录，采用最新一次
        已写入题湖的人工决定；当前目录存在记录时仍以其为准。
        """
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
                row = self._connection.execute(
                    f"""
                    SELECT stable_code, original_target_path_json, target_path_json, accepted_at, updated_at
                    FROM {self._MANUAL_TABLE}
                    WHERE exercise_id = ?
                    ORDER BY updated_at DESC, rowid DESC
                    LIMIT 1
                    """,
                    (exercise_id,),
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

    def record_catalogue_move(
        self,
        *,
        exercise_id: str,
        stable_code: str,
        source_catalogue_id: str,
        target_catalogue_id: str,
        original_path: list[str],
        target_path: list[str],
    ) -> dict[str, Any]:
        """追加一次已由题湖回读确认的目录移动，不受缓存清理影响。"""
        if source_catalogue_id == target_catalogue_id:
            raise ValueError("原目录与目标目录相同，不应记录为移动")
        with self._lock:
            cursor = self._connection.execute(
                f"""
                INSERT INTO {self._MOVE_HISTORY_TABLE} (
                    exercise_id, stable_code, source_catalogue_id, target_catalogue_id,
                    original_path_json, target_path_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    exercise_id,
                    stable_code.strip() or None,
                    source_catalogue_id,
                    target_catalogue_id,
                    json.dumps(original_path, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(target_path, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            self._connection.commit()
            row = self._connection.execute(
                f"SELECT moved_at FROM {self._MOVE_HISTORY_TABLE} WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return {"record_id": cursor.lastrowid, "moved_at": row[0]}

    def catalogue_move_report(
        self,
        now: datetime | None = None,
        selected_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """按 UTC+8 的指定日期或日期范围汇报每题最近一次移动。"""
        utc8 = timezone(timedelta(hours=8))
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            raise ValueError("工作成果查询时间必须携带时区")

        def parse_local_date(value: str | None) -> date:
            try:
                return date.fromisoformat(value or "")
            except (TypeError, ValueError) as error:
                raise ValueError("日期必须使用 YYYY-MM-DD 格式") from error

        if selected_date is not None and (start_date is not None or end_date is not None):
            raise ValueError("日期与日期范围不能同时提供")
        if selected_date is not None:
            local_start_date = parse_local_date(selected_date)
            local_end_date = local_start_date
            period_label = f"{local_start_date.isoformat()} 工作成果"
        elif start_date is not None or end_date is not None:
            if start_date is None or end_date is None:
                raise ValueError("日期范围必须同时提供起始日期和截止日期")
            local_start_date = parse_local_date(start_date)
            local_end_date = parse_local_date(end_date)
            if local_end_date < local_start_date:
                raise ValueError("截止日期不能早于起始日期")
            period_label = (
                f"{local_start_date.isoformat()} 工作成果"
                if local_start_date == local_end_date
                else f"{local_start_date.isoformat()} 至 {local_end_date.isoformat()} 工作成果"
            )
        else:
            local_start_date = current_time.astimezone(utc8).date()
            local_end_date = local_start_date
            period_label = f"{local_start_date.isoformat()} 今日"
        local_start = datetime.combine(local_start_date, time.min, tzinfo=utc8)
        local_end = datetime.combine(local_end_date + timedelta(days=1), time.min, tzinfo=utc8)
        # SQLite CURRENT_TIMESTAMP 以 UTC 的 YYYY-MM-DD HH:MM:SS 保存；采用左闭右开区间，
        # 可以准确包含北京时间 00:00:00，又不会包含次日零点的记录。
        utc_start = local_start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        utc_end = local_end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT exercise_id, stable_code, original_path_json, target_path_json, moved_at
                FROM {self._MOVE_HISTORY_TABLE}
                WHERE moved_at >= ? AND moved_at < ?
                ORDER BY moved_at DESC, id DESC
                """,
                (utc_start, utc_end),
            ).fetchall()
        latest_by_exercise: dict[str, dict[str, Any]] = {}
        for exercise_id, stable_code, original_json, target_json, moved_at in rows:
            if exercise_id in latest_by_exercise:
                continue
            latest_by_exercise[exercise_id] = {
                "stable_code": stable_code or "",
                "original_path": json.loads(original_json),
                "target_path": json.loads(target_json),
                "moved_at": moved_at,
            }
        records = list(latest_by_exercise.values())
        topics = sorted({record["target_path"][0] for record in records if record["target_path"]})
        return {
            "summary": {
                "classified_count": len(records),
                "topics": topics,
                "period": {
                    "label": period_label,
                    "date": local_start_date.isoformat(),
                    "start_date": local_start_date.isoformat(),
                    "end_date": local_end_date.isoformat(),
                    "timezone": "UTC+08:00",
                    "utc_start": utc_start,
                    "utc_end": utc_end,
                },
            },
            "records": records,
        }

    def latest_catalogue_moves(self, exercise_ids: list[str]) -> dict[str, dict[str, Any]]:
        """返回指定题目的最近一次已回读目录移动，用于跨 Focus 恢复终态。"""
        normalized = sorted({str(item).strip() for item in exercise_ids if str(item).strip()})
        if not normalized:
            return {}
        placeholders = ",".join("?" for _ in normalized)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT exercise_id, stable_code, target_catalogue_id, target_path_json, moved_at
                FROM {self._MOVE_HISTORY_TABLE}
                WHERE exercise_id IN ({placeholders})
                ORDER BY moved_at DESC, id DESC
                """,
                normalized,
            ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for exercise_id, stable_code, target_catalogue_id, target_path_json, moved_at in rows:
            if exercise_id in latest:
                continue
            latest[exercise_id] = {
                "stable_code": stable_code or "",
                "target_catalogue_id": target_catalogue_id,
                "target_path": json.loads(target_path_json),
                "moved_at": moved_at,
            }
        return latest

    def close(self) -> None:
        with self._lock:
            self._connection.close()
