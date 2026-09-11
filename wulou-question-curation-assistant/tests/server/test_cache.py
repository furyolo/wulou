from __future__ import annotations

import sqlite3
import tempfile
import unittest
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
                original_target_path=["专题1：实数", "【大题】", "旧分类"],
                target_path=["专题1：实数", "【大题】", "分母有理化"],
            )

            override = cache.get_manual_override("exercise-1", "level4-a")
            self.assertIsNotNone(override)
            self.assertEqual(override["target_path"][-1], "分母有理化")
            self.assertEqual(
                cache._connection.execute("SELECT COUNT(*) FROM manual_classification_audit").fetchone()[0], 1
            )
            self.assertEqual(
                cache._connection.execute("SELECT COUNT(*) FROM classification_results").fetchone()[0], 0
            )
            self.assertEqual(cache.delete_by_exercise_ids(["exercise-1"]), 0)
            self.assertIsNotNone(cache.get_manual_override("exercise-1", "level4-a"))
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
            self.assertEqual(report["summary"]["topics"], ["专题10：三角形", "专题4：分式方程与不等式"])
            latest = next(item for item in report["records"] if item["stable_code"] == "CS2026MOVE001")
            self.assertEqual(latest["original_path"][0], "专题4：分式方程与不等式")
            self.assertEqual(latest["target_path"][0], "专题10：三角形")
            self.assertEqual(cache.delete_by_exercise_ids(["exercise-1", "exercise-2"]), 0)
            self.assertEqual(cache.catalogue_move_report()["summary"]["classified_count"], 2)
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


if __name__ == "__main__":
    unittest.main()
