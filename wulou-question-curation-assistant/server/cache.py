"""仅保存可追溯的分类结果，不保存浏览器登录凭据。"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


class ResultCache:
    """按“题目 + 当前题湖目录叶子”保存每个分类语境的最新结果。"""

    _TABLE = "classification_results"
    _LEGACY_TABLE = "classification_results_legacy_v1"
    _MANUAL_TABLE = "manual_classification_overrides"
    _MANUAL_AUDIT_TABLE = "manual_classification_audit"
    _MOVE_HISTORY_TABLE = "catalogue_move_history"
    _TOPIC_NUMBER = re.compile(r"专题\s*(\d+)")

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
            # 人工修正与模型缓存分表保存。人工选择只对作出选择时的目录版本有效；
            # 目录更新后必须重新让模型基于新目录分类，不能把旧目录下的人工选择
            # 当作新目录中的最终结论。
            self._connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._MANUAL_TABLE} (
                    exercise_id TEXT NOT NULL,
                    source_catalogue_id TEXT NOT NULL,
                    stable_code TEXT,
                    source TEXT NOT NULL DEFAULT 'manual',
                    taxonomy_version TEXT NOT NULL,
                    original_target_path_json TEXT NOT NULL,
                    target_path_json TEXT NOT NULL,
                    accepted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (exercise_id, source_catalogue_id)
                )
                """
            )
            manual_columns = {
                str(row[1]) for row in self._connection.execute(f"PRAGMA table_info({self._MANUAL_TABLE})")
            }
            if "taxonomy_version" not in manual_columns:
                # 旧记录没有可验证的目录版本，保留供审计，但绝不在新目录中恢复。
                self._connection.execute(f"ALTER TABLE {self._MANUAL_TABLE} ADD COLUMN taxonomy_version TEXT")
            if "source" not in manual_columns:
                # 旧记录都产生于人工采纳；外部目录整理 Skill 导入的结果才标为 skill。
                self._connection.execute(
                    f"ALTER TABLE {self._MANUAL_TABLE} ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'"
                )
            self._connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._MANUAL_AUDIT_TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exercise_id TEXT NOT NULL,
                    source_catalogue_id TEXT NOT NULL,
                    stable_code TEXT,
                    source TEXT NOT NULL DEFAULT 'manual',
                    taxonomy_version TEXT,
                    original_target_path_json TEXT NOT NULL,
                    target_path_json TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            audit_columns = {
                str(row[1]) for row in self._connection.execute(f"PRAGMA table_info({self._MANUAL_AUDIT_TABLE})")
            }
            if "taxonomy_version" not in audit_columns:
                self._connection.execute(f"ALTER TABLE {self._MANUAL_AUDIT_TABLE} ADD COLUMN taxonomy_version TEXT")
            if "source" not in audit_columns:
                self._connection.execute(
                    f"ALTER TABLE {self._MANUAL_AUDIT_TABLE} ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'"
                )
            self._connection.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self._MANUAL_TABLE}_stable_code ON {self._MANUAL_TABLE}(stable_code)"
            )
            self._connection.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self._MANUAL_TABLE}_exercise_updated "
                f"ON {self._MANUAL_TABLE}(exercise_id, updated_at DESC)"
            )
            self._connection.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self._MANUAL_TABLE}_exercise_taxonomy_updated "
                f"ON {self._MANUAL_TABLE}(exercise_id, taxonomy_version, updated_at DESC)"
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

    def get_manual_overrides(self, exercise_id: str, source_catalogue_id: str) -> list[dict[str, Any]]:
        """按优先级返回人工修正候选：当前目录优先，其后是同题的其他决定（新的在前）。

        题目移动到人工指定的目标叶子后，当前目录 ID 会变成目标目录 ID，所以同题在
        别的目录下的记录仍要作为候选返回，不能因为目录 ID 变了就退回模型缓存。

        这里不再按 taxonomy_version 过滤：版本号是整表级的（整份工作簿的哈希），
        照它一刀切会让改动一个专题时、目标落在其他专题的人工结论被无谓作废。
        记录是否仍然成立改由上层按“目标路径能否在当前目录里解析”判定。
        """
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT source, stable_code, taxonomy_version, original_target_path_json, target_path_json, accepted_at, updated_at
                FROM {self._MANUAL_TABLE}
                WHERE exercise_id = ?
                ORDER BY (source_catalogue_id = ?) DESC, updated_at DESC, rowid DESC
                """,
                (exercise_id, source_catalogue_id),
            ).fetchall()
        return [
            {
                "source": str(row[0] or "manual"),
                "stable_code": row[1],
                "taxonomy_version": row[2],
                "original_target_path": json.loads(row[3]),
                "target_path": json.loads(row[4]),
                "accepted_at": row[5],
                "updated_at": row[6],
            }
            for row in rows
        ]

    def put_manual_override(
        self,
        *,
        exercise_id: str,
        source_catalogue_id: str,
        stable_code: str,
        taxonomy_version: str,
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
                    exercise_id, source_catalogue_id, stable_code, source, taxonomy_version,
                    original_target_path_json, target_path_json
                ) VALUES (?, ?, ?, 'manual', ?, ?, ?)
                ON CONFLICT(exercise_id, source_catalogue_id) DO UPDATE SET
                    stable_code = COALESCE(excluded.stable_code, stable_code),
                    source = excluded.source,
                    taxonomy_version = excluded.taxonomy_version,
                    original_target_path_json = excluded.original_target_path_json,
                    target_path_json = excluded.target_path_json,
                    accepted_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (exercise_id, source_catalogue_id, normalized_stable_code, taxonomy_version, original_json, target_json),
            )
            self._connection.execute(
                f"""
                INSERT INTO {self._MANUAL_AUDIT_TABLE} (
                    exercise_id, source_catalogue_id, stable_code, source, taxonomy_version,
                    original_target_path_json, target_path_json, event_type
                ) VALUES (?, ?, ?, 'manual', ?, ?, ?, 'accepted')
                """,
                (exercise_id, source_catalogue_id, normalized_stable_code, taxonomy_version, original_json, target_json),
            )
            self._connection.commit()
        # 刚写入的这条必然是当前目录、且 updated 最新，排在候选首位。
        overrides = self.get_manual_overrides(exercise_id, source_catalogue_id)
        if not overrides:
            raise RuntimeError("人工修正保存后无法读取")
        return overrides[0]

    def _existing_manual_rows(self, exercise_ids: list[str]) -> dict[tuple[str, str], dict[str, Any]]:
        """批量读取同键的现有归类记录，供导入报告如实区分新增与覆盖。

        一并取回目标路径：调用方要按「目标目录在当前目录里还解析不解析得到」决定
        要不要保下人工决策，这跟读取层用的是同一把尺子。
        """
        existing: dict[tuple[str, str], dict[str, Any]] = {}
        unique_ids = sorted({str(item).strip() for item in exercise_ids if str(item).strip()})
        # SQLite 的变量上限按编译参数而定，分块查询避免上千题导入时越界。
        for start in range(0, len(unique_ids), 400):
            chunk = unique_ids[start:start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = self._connection.execute(
                f"""
                SELECT exercise_id, source_catalogue_id, source, taxonomy_version, target_path_json
                FROM {self._MANUAL_TABLE}
                WHERE exercise_id IN ({placeholders})
                """,
                chunk,
            ).fetchall()
            for exercise_id, source_catalogue_id, source, taxonomy_version, target_path_json in rows:
                existing[(str(exercise_id), str(source_catalogue_id))] = {
                    "source": str(source or "manual"),
                    "taxonomy_version": str(taxonomy_version or ""),
                    "target_path": json.loads(target_path_json),
                }
        return existing

    def put_skill_classifications(
        self,
        rows: list[dict[str, Any]],
        *,
        keep_manual_decision: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, int]:
        """批量写入目录整理 Skill 的归类结果。

        结果与人工修正在同一张表保存，靠 ``source`` 区分来源，因此审计语义不失真。
        每条写入都追加审计事件，便于事后核对某次导入到底改动了哪些题。

        ``keep_manual_decision`` 决定要不要保下已有人工决策：只对 ``source='manual'``
        的现存同键记录求值，返回 True 才跳过（计入 ``skipped_manual_decisions``）。
        判定权交给调用方，是因为「旧结论还算不算数」要用当前目录来判断，而这是它
        才拿得到的东西——这样导入时的保留判据与读取层完全一致，不会出现「界面上
        还显示着人工结论，下次导入却把它覆盖了」。默认 None 表示不做保护：同键记录
        被批量结果直接覆盖，``source`` 改写成 ``skill``。
        """
        if not rows:
            return {"inserted": 0, "updated": 0, "skipped_manual_decisions": 0}
        with self._lock:
            previous = self._existing_manual_rows([str(row["exercise_id"]) for row in rows])
            inserted = updated = skipped = 0
            for row in rows:
                exercise_id = str(row["exercise_id"])
                source_catalogue_id = str(row["source_catalogue_id"])
                taxonomy_version = str(row["taxonomy_version"])
                known = previous.get((exercise_id, source_catalogue_id))
                if (
                    keep_manual_decision is not None
                    and known
                    and known["source"] == "manual"
                    and keep_manual_decision(known)
                ):
                    skipped += 1
                    continue
                stable_code = str(row.get("stable_code", "")).strip() or None
                original_json = json.dumps(
                    row.get("original_target_path") or [], ensure_ascii=False, separators=(",", ":")
                )
                target_json = json.dumps(
                    row.get("target_path") or [], ensure_ascii=False, separators=(",", ":")
                )
                self._connection.execute(
                    f"""
                    INSERT INTO {self._MANUAL_TABLE} (
                        exercise_id, source_catalogue_id, stable_code, source, taxonomy_version,
                        original_target_path_json, target_path_json
                    ) VALUES (?, ?, ?, 'skill', ?, ?, ?)
                    ON CONFLICT(exercise_id, source_catalogue_id) DO UPDATE SET
                        stable_code = COALESCE(excluded.stable_code, stable_code),
                        source = excluded.source,
                        taxonomy_version = excluded.taxonomy_version,
                        original_target_path_json = excluded.original_target_path_json,
                        target_path_json = excluded.target_path_json,
                        accepted_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (exercise_id, source_catalogue_id, stable_code, taxonomy_version, original_json, target_json),
                )
                self._connection.execute(
                    f"""
                    INSERT INTO {self._MANUAL_AUDIT_TABLE} (
                        exercise_id, source_catalogue_id, stable_code, source, taxonomy_version,
                        original_target_path_json, target_path_json, event_type
                    ) VALUES (?, ?, ?, 'skill', ?, ?, ?, 'skill_import')
                    """,
                    (exercise_id, source_catalogue_id, stable_code, taxonomy_version, original_json, target_json),
                )
                if known:
                    updated += 1
                else:
                    inserted += 1
            self._connection.commit()
        return {"inserted": inserted, "updated": updated, "skipped_manual_decisions": skipped}

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
        # 工作成果按题目移动前的目录语境汇总，不把现目录/目标目录计入涉及专题。
        # “专题10”必须排在“专题4”之后，不能采用普通字符串排序。
        def topic_sort_key(topic: str) -> tuple[int, int, str]:
            match = self._TOPIC_NUMBER.search(topic)
            return (0, int(match.group(1)), topic) if match else (1, 0, topic)

        topics = sorted(
            {record["original_path"][0] for record in records if record["original_path"]},
            key=topic_sort_key,
        )
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
