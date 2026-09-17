from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from server.cache import ResultCache


class ResultCacheTests(unittest.TestCase):
    def test_same_question_and_directory_overwrites_but_different_directory_persists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = ResultCache(Path(directory) / "cache.sqlite3")
            cache.put(
                "first", "exercise-1", "level4-a", {"value": "first"}, stable_code="CS2026TEST001"
            )
            cache.put("second", "exercise-1", "level4-a", {"value": "second"})
            cache.put("third", "exercise-1", "level4-b", {"value": "third"})

            self.assertIsNone(cache.get("first", "exercise-1", "level4-a"))
            self.assertEqual(cache.get("second", "exercise-1", "level4-a"), {"value": "second"})
            self.assertEqual(cache.get("third", "exercise-1", "level4-b"), {"value": "third"})
            count = cache._connection.execute("SELECT COUNT(*) FROM classification_results").fetchone()[0]
            self.assertEqual(count, 2)
            stable_code = cache._connection.execute(
                "SELECT stable_code FROM classification_results WHERE exercise_id = ? AND source_catalogue_id = ?",
                ("exercise-1", "level4-a"),
            ).fetchone()[0]
            self.assertEqual(stable_code, "CS2026TEST001")
            cache.close()

    def test_adds_stable_code_to_current_schema_without_losing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE classification_results (
                    exercise_id TEXT NOT NULL,
                    source_catalogue_id TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (exercise_id, source_catalogue_id)
                )
                """
            )
            connection.execute(
                "INSERT INTO classification_results (exercise_id, source_catalogue_id, cache_key, payload_json) VALUES (?, ?, ?, ?)",
                ("exercise-1", "level4-a", "key", "{}"),
            )
            connection.commit()
            connection.close()

            cache = ResultCache(path)
            columns = {
                str(row[1]) for row in cache._connection.execute("PRAGMA table_info(classification_results)")
            }
            self.assertIn("stable_code", columns)
            self.assertEqual(
                cache._connection.execute("SELECT COUNT(*) FROM classification_results").fetchone()[0], 1
            )
            cache.close()

    def test_manual_override_and_audit_are_persisted_separately_from_model_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = ResultCache(Path(directory) / "cache.sqlite3")
            cache.put_manual_override(
                exercise_id="exercise-1",
                source_catalogue_id="level4-a",
                stable_code="CS2026TEST001",
                taxonomy_version="taxonomy-v1",
                original_target_path=["专题1：实数", "【大题】", "旧分类"],
                target_path=["专题1：实数", "【大题】", "分母有理化"],
            )

            overrides = cache.get_manual_overrides("exercise-1", "level4-a")
            self.assertTrue(overrides)
            self.assertEqual(overrides[0]["target_path"][-1], "分母有理化")
            # 移到人工指定的目标叶子后，目录 ID 已变化，仍要恢复人工终态。
            moved_overrides = cache.get_manual_overrides("exercise-1", "level4-target")
            self.assertTrue(moved_overrides)
            self.assertEqual(moved_overrides[0]["target_path"][-1], "分母有理化")
            self.assertEqual(
                cache._connection.execute("SELECT COUNT(*) FROM manual_classification_audit").fetchone()[0], 1
            )
            self.assertEqual(
                cache._connection.execute("SELECT COUNT(*) FROM classification_results").fetchone()[0], 0
            )
            self.assertEqual(cache.delete_by_exercise_ids(["exercise-1"]), 0)
            # 目录版本号变化不再直接作废记录：读取一律返回候选，是否仍然成立改由
            # 上层按“目标路径能否在当前目录里解析”判定（见 ServiceState）。
            self.assertTrue(cache.get_manual_overrides("exercise-1", "level4-a"))
            cache.close()

    def test_legacy_manual_override_without_a_taxonomy_version_is_still_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE manual_classification_overrides (
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
            connection.execute(
                """
                INSERT INTO manual_classification_overrides (
                    exercise_id, source_catalogue_id, original_target_path_json, target_path_json
                ) VALUES (?, ?, ?, ?)
                """,
                ("exercise-1", "level4-a", '["旧目录"]', '["人工旧目录"]'),
            )
            connection.commit()
            connection.close()

            cache = ResultCache(path)

            self.assertEqual(
                cache._connection.execute("SELECT COUNT(*) FROM manual_classification_overrides").fetchone()[0], 1
            )
            # 旧表结构缺 source/taxonomy_version 列，仍要能读出来并默认成人工来源；
            # 它那条 ["人工旧目录"] 只有一级，会在上层解析时被判为失效而丢弃。
            legacy = cache.get_manual_overrides("exercise-1", "level4-a")
            self.assertEqual(len(legacy), 1)
            self.assertEqual(legacy[0]["source"], "manual")
            cache.close()

    def test_catalogue_move_report_keeps_only_latest_move_per_question(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = ResultCache(Path(directory) / "cache.sqlite3")
            cache.record_catalogue_move(
                exercise_id="exercise-1", stable_code="CS2026MOVE001",
                source_catalogue_id="old-leaf", target_catalogue_id="middle-leaf",
                original_path=["专题1：实数", "【大题】", "旧三级", "旧四级"],
                target_path=["专题4：分式方程与不等式", "【大题】", "解不等式", "考法1"],
            )
            cache.record_catalogue_move(
                exercise_id="exercise-1", stable_code="CS2026MOVE001",
                source_catalogue_id="middle-leaf", target_catalogue_id="new-leaf",
                original_path=["专题4：分式方程与不等式", "【大题】", "解不等式", "考法1"],
                target_path=["专题10：三角形", "【大题】", "全等三角形", "考法2"],
            )
            cache.record_catalogue_move(
                exercise_id="exercise-2", stable_code="CS2026MOVE002",
                source_catalogue_id="old-leaf-2", target_catalogue_id="new-leaf-2",
                original_path=["专题1：实数", "【大题】", "旧三级"],
                target_path=["专题4：分式方程与不等式", "【大题】", "解不等式"],
            )

            report = cache.catalogue_move_report()
            self.assertEqual(report["summary"]["classified_count"], 2)
            self.assertEqual(report["summary"]["topics"], ["专题1：实数", "专题4：分式方程与不等式"])
            latest = next(item for item in report["records"] if item["stable_code"] == "CS2026MOVE001")
            self.assertEqual(latest["original_path"][0], "专题4：分式方程与不等式")
            self.assertEqual(latest["target_path"][0], "专题10：三角形")
            self.assertEqual(cache.delete_by_exercise_ids(["exercise-1", "exercise-2"]), 0)
            self.assertEqual(cache.catalogue_move_report()["summary"]["classified_count"], 2)
            cache.close()

    def test_catalogue_move_report_sorts_source_topics_by_topic_number(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = ResultCache(Path(directory) / "cache.sqlite3")
            for number in (10, 4, 2):
                cache.record_catalogue_move(
                    exercise_id=f"exercise-{number}", stable_code=f"CS{number}",
                    source_catalogue_id=f"source-{number}", target_catalogue_id=f"target-{number}",
                    original_path=[f"专题{number}：原目录", "【大题】", "知识点"],
                    target_path=["专题99：现目录", "【大题】", "知识点"],
                )
            report = cache.catalogue_move_report()
            self.assertEqual(
                report["summary"]["topics"],
                ["专题2：原目录", "专题4：原目录", "专题10：原目录"],
            )
            cache.close()

    def test_catalogue_move_report_uses_utc8_today_as_a_half_open_utc_interval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = ResultCache(Path(directory) / "cache.sqlite3")

            def record(exercise_id: str, moved_at: str) -> None:
                cache.record_catalogue_move(
                    exercise_id=exercise_id, stable_code=f"CS2026{exercise_id}",
                    source_catalogue_id=f"old-{exercise_id}", target_catalogue_id=f"new-{exercise_id}",
                    original_path=["专题1：实数", "【大题】", "旧分类"],
                    target_path=["专题4：分式方程与不等式", "【大题】", "解不等式"],
                )
                cache._connection.execute(
                    "UPDATE catalogue_move_history SET moved_at = ? WHERE exercise_id = ?",
                    (moved_at, exercise_id),
                )
                cache._connection.commit()

            # UTC+8 的 2026-09-12 对应 SQLite UTC 的 [2026-09-11 16:00:00, 2026-09-12 16:00:00)。
            record("before", "2026-09-11 15:59:59")
            record("start", "2026-09-11 16:00:00")
            record("end", "2026-09-12 15:59:59")
            record("after", "2026-09-12 16:00:00")

            report = cache.catalogue_move_report(now=datetime(2026, 9, 12, 8, tzinfo=timezone.utc))
            self.assertEqual(report["summary"]["period"]["date"], "2026-09-12")
            self.assertEqual(report["summary"]["period"]["label"], "2026-09-12 今日")
            self.assertEqual(report["summary"]["period"]["utc_start"], "2026-09-11 16:00:00")
            self.assertEqual(report["summary"]["period"]["utc_end"], "2026-09-12 16:00:00")
            self.assertEqual({item["stable_code"] for item in report["records"]}, {"CS2026start", "CS2026end"})

            selected = cache.catalogue_move_report(selected_date="2026-09-11")
            self.assertEqual(selected["summary"]["period"]["label"], "2026-09-11 工作成果")
            self.assertEqual({item["stable_code"] for item in selected["records"]}, {"CS2026before"})
            ranged = cache.catalogue_move_report(start_date="2026-09-11", end_date="2026-09-12")
            self.assertEqual(ranged["summary"]["period"]["label"], "2026-09-11 至 2026-09-12 工作成果")
            self.assertEqual(ranged["summary"]["period"]["start_date"], "2026-09-11")
            self.assertEqual(ranged["summary"]["period"]["end_date"], "2026-09-12")
            self.assertEqual({item["stable_code"] for item in ranged["records"]}, {"CS2026before", "CS2026start", "CS2026end"})
            with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
                cache.catalogue_move_report(selected_date="2026/09/11")
            with self.assertRaisesRegex(ValueError, "截止日期"):
                cache.catalogue_move_report(start_date="2026-09-12", end_date="2026-09-11")
            cache.close()

    def test_legacy_cache_table_is_preserved_during_schema_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE classification_results (cache_key TEXT PRIMARY KEY, exercise_id TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            connection.execute(
                "INSERT INTO classification_results (cache_key, exercise_id, payload_json) VALUES ('legacy', 'exercise-1', '{}')"
            )
            connection.commit()
            connection.close()

            cache = ResultCache(path)
            tables = {
                row[0] for row in cache._connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            self.assertIn("classification_results", tables)
            self.assertIn("classification_results_legacy_v1", tables)
            self.assertEqual(cache._connection.execute("SELECT COUNT(*) FROM classification_results").fetchone()[0], 0)
            self.assertEqual(cache._connection.execute("SELECT COUNT(*) FROM classification_results_legacy_v1").fetchone()[0], 1)
            cache.close()

    def test_semantic_cache_can_be_recovered_after_the_question_moves_to_another_leaf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = ResultCache(Path(directory) / "cache.sqlite3")
            cache.put("semantic-key", "exercise-1", "catalogue-before", {"target": {"path": ["目录 A"]}})

            recovered = cache.get("semantic-key", "exercise-1", "catalogue-after")

            self.assertEqual(recovered, {"target": {"path": ["目录 A"]}})
            cache.close()

    def test_latest_catalogue_moves_returns_the_newest_record_for_each_question(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = ResultCache(Path(directory) / "cache.sqlite3")
            cache.record_catalogue_move(exercise_id="exercise-1", stable_code="CS-1", source_catalogue_id="a", target_catalogue_id="b", original_path=["原"], target_path=["目标一"])
            cache.record_catalogue_move(exercise_id="exercise-1", stable_code="CS-1", source_catalogue_id="b", target_catalogue_id="c", original_path=["目标一"], target_path=["目标二"])

            moves = cache.latest_catalogue_moves(["exercise-1", "missing"])

            self.assertEqual(moves["exercise-1"]["target_catalogue_id"], "c")
            self.assertEqual(moves["exercise-1"]["target_path"], ["目标二"])
            cache.close()


if __name__ == "__main__":
    unittest.main()
