from __future__ import annotations

import copy
import sys
import json
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook, load_workbook

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.directory_refactor import (  # noqa: E402
    DirectoryRefactorError,
    prepare_refactor_context,
    validate_refactor_plan,
)
from server.taxonomy import Taxonomy  # noqa: E402
from server.excel_sync import sha256, write_approved_refactor  # noqa: E402


class DirectoryRefactorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.taxonomy = Taxonomy.from_file(ROOT / "config" / "taxonomy.example.yaml")
        self.questions = [{"exercise_id": str(index)} for index in range(1, 7)]
        self.context = prepare_refactor_context({
            "focus": {
                "level": 3,
                "topic_id": "topic-01-real-numbers",
                "level2_id": "topic-01-large",
                "level3_id": "real-number-calculation",
            },
            "questions": self.questions,
        }, self.taxonomy)

    def outline_plan(self, supporting_ids: list[str]) -> dict:
        return {
            "level3": [{"key": "real-number-calculation", "title": "实数综合计算", "level4": [
                {"key": "rationalization", "title": "考法1：含分母有理化的实数计算", "basis": "分母有理化是计算的决定性条件", "supporting_exercise_ids": supporting_ids},
            ], "basis": "以实数运算为首要对象"}],
            "notes": [],
        }

    def test_level3_outline_uses_six_representative_questions_per_level4(self) -> None:
        plan = self.outline_plan([str(index) for index in range(1, 7)])
        result = validate_refactor_plan(self.context, plan)
        self.assertEqual(result["status"], "review")
        self.assertEqual(result["audit"]["level4_counts"][0]["count"], 6)
        self.assertNotIn("assignments", result)
        self.assertEqual(result["audit"]["classification_scope"], "directory_outline_only")

    def test_level3_outline_rejects_level4_below_numbered_parent_without_restructure(self) -> None:
        context = copy.deepcopy(self.context)
        context["selected_level3"][0]["knowledge_point_id"] = "ZCSQG20260915KP01"
        with self.assertRaisesRegex(DirectoryRefactorError, "承接四级目录重构"):
            validate_refactor_plan(context, self.outline_plan([str(index) for index in range(1, 7)]))

    def test_level3_outline_rejects_undersized_representative_set(self) -> None:
        plan = self.outline_plan([str(index) for index in range(1, 6)])
        with self.assertRaises(DirectoryRefactorError):
            validate_refactor_plan(self.context, plan)

    def test_level3_focus_cannot_split_into_multiple_level3_directories(self) -> None:
        plan = {"level3": [
            {"key": "first", "title": "实数综合计算", "basis": "计算", "level4": []},
            {"key": "second", "title": "实数应用", "basis": "应用", "level4": []},
        ], "notes": []}
        with self.assertRaises(DirectoryRefactorError):
            validate_refactor_plan(self.context, plan)

    def test_level3_focus_with_six_questions_requires_level4(self) -> None:
        plan = {"level3": [
            {"key": "real-number-calculation", "title": "实数综合计算", "basis": "计算", "level4": []},
        ], "notes": []}
        with self.assertRaises(DirectoryRefactorError):
            validate_refactor_plan(self.context, plan)

    def test_partial_collection_keeps_successful_questions_and_records_gap(self) -> None:
        context = prepare_refactor_context({
            "focus": {
                "level": 3,
                "topic_id": "topic-01-real-numbers",
                "level2_id": "topic-01-large",
                "level3_id": "real-number-calculation",
            },
            "questions": self.questions,
            "collection": {
                "discovered_question_count": 7,
                "collected_question_count": 6,
                "failed_exercise_ids": ["7"],
                "failed_page_urls": [],
            },
        }, self.taxonomy)
        self.assertEqual(context["collection"]["failed_exercise_ids"], ["7"])
        self.assertEqual(len(context["questions"]), 6)

    def test_stratified_sample_is_marked_as_incomplete_coverage(self) -> None:
        context = prepare_refactor_context({
            "focus": {
                "level": 3, "topic_id": "topic-01-real-numbers",
                "level2_id": "topic-01-large", "level3_id": "real-number-calculation",
            },
            "questions": self.questions,
            "collection": {
                "discovered_question_count": 20, "collected_question_count": 6,
                "failed_exercise_ids": [], "failed_page_urls": [],
                "sampling": {"mode": "stratified_page", "source_question_count": 20},
            },
        }, self.taxonomy)
        result = validate_refactor_plan(context, self.outline_plan([str(index) for index in range(1, 7)]))
        self.assertFalse(result["audit"]["coverage_complete"])
        self.assertEqual(result["collection"]["sampling"]["mode"], "stratified_page")

    def test_stratified_sample_accepts_three_evidence_questions_as_candidate(self) -> None:
        context = prepare_refactor_context({
            "focus": {
                "level": 3, "topic_id": "topic-01-real-numbers",
                "level2_id": "topic-01-large", "level3_id": "real-number-calculation",
            },
            "questions": self.questions,
            "collection": {
                "discovered_question_count": 20, "collected_question_count": 6,
                "failed_exercise_ids": [], "failed_page_urls": [],
                "sampling": {"mode": "stratified_page", "source_question_count": 20},
            },
        }, self.taxonomy)
        result = validate_refactor_plan(context, self.outline_plan(["1", "2", "3"]))
        evidence = result["audit"]["level4_counts"][0]
        self.assertEqual(evidence["count"], 3)
        self.assertEqual(evidence["evidence_tier"], "candidate")

    def test_stratified_sample_marks_four_evidence_questions_as_strong_candidate(self) -> None:
        context = prepare_refactor_context({
            "focus": {
                "level": 3, "topic_id": "topic-01-real-numbers",
                "level2_id": "topic-01-large", "level3_id": "real-number-calculation",
            },
            "questions": self.questions,
            "collection": {
                "discovered_question_count": 20, "collected_question_count": 6,
                "failed_exercise_ids": [], "failed_page_urls": [],
                "sampling": {"mode": "stratified_page", "source_question_count": 20},
            },
        }, self.taxonomy)
        result = validate_refactor_plan(context, self.outline_plan(["1", "2", "3", "4"]))
        self.assertEqual(result["audit"]["level4_counts"][0]["evidence_tier"], "strong_candidate")

    def test_stratified_sample_rejects_fewer_than_three_evidence_questions(self) -> None:
        context = prepare_refactor_context({
            "focus": {
                "level": 3, "topic_id": "topic-01-real-numbers",
                "level2_id": "topic-01-large", "level3_id": "real-number-calculation",
            },
            "questions": self.questions,
            "collection": {
                "discovered_question_count": 20, "collected_question_count": 6,
                "failed_exercise_ids": [], "failed_page_urls": [],
                "sampling": {"mode": "stratified_page", "source_question_count": 20},
            },
        }, self.taxonomy)
        with self.assertRaises(DirectoryRefactorError):
            validate_refactor_plan(context, self.outline_plan(["1", "2"]))

    def test_outline_references_the_whole_topic_directory_tree(self) -> None:
        reference = self.context["reference_directory_tree"]
        self.assertGreaterEqual(len(reference), 1)
        self.assertIn("topic-01-large", [level2["id"] for level2 in reference])
        self.assertTrue(all("level3" in level2 for level2 in reference))

    def test_excel_refactor_preserves_values_outside_replaced_range(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            baseline = folder / "baseline.xlsx"
            output = folder / "output.xlsx"
            plan_path = folder / "plan.json"
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "目录"
            sheet.cell(1, 1).value = "前缀"
            sheet.cell(2, 3).value = "旧三级"
            sheet.cell(3, 4).value = "旧四级"
            sheet.cell(4, 1).value = "后缀"
            workbook.save(baseline)
            plan_path.write_text(json.dumps({
                "status": "approved", "baseline_sha256": sha256(baseline), "sheet": "目录",
                "replace_start_row": 2, "replace_end_row": 3,
                "rows": [
                    {"level": 3, "title": "新三级", "question_count": 6},
                    {"level": 4, "title": "考法1：新四级", "question_count": 6},
                ],
            }, ensure_ascii=False), encoding="utf-8")
            write_approved_refactor(baseline, output, plan_path)
            result = load_workbook(output, data_only=False)["目录"]
            self.assertEqual(result.cell(1, 1).value, "前缀")
            self.assertEqual(result.cell(2, 3).value, "新三级")
            self.assertEqual(result.cell(3, 4).value, "考法1：新四级")
            self.assertEqual(result.cell(4, 1).value, "后缀")


if __name__ == "__main__":
    unittest.main()
