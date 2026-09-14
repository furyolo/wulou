from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.directory_exports import write_directory_export  # noqa: E402


class DirectoryExportTests(unittest.TestCase):
    def test_export_contains_full_questions_and_manual_skill_handoff_without_excel_range(self) -> None:
        context = {
            "focus": {"level": 3, "topic_id": "topic", "level2_id": "l2", "level3_id": "l3"},
            "questions": [{"exercise_id": "1", "text": "题一"}, {"exercise_id": "2", "text": "题二"}],
            "collection": {"sampling": {"mode": "full", "source_question_count": 2}},
            "selected_level3": [{"id": "l3", "title": "三级"}],
            "reference_directory_tree": [{"id": "l2", "title": "二级", "level3": []}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            result = write_directory_export(
                Path(temporary), context=context, taxonomy_version="taxonomy-v1", rule_version="rules-v1"
            )
            rows = Path(result["questions_path"]).read_text(encoding="utf-8").splitlines()
            manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual([json.loads(row)["exercise_id"] for row in rows], ["1", "2"])
        self.assertEqual(manifest["question_count"], 2)
        self.assertRegex(manifest["created_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(manifest["manual_agent_handoff"]["skill"], "math-exam-directory-curation")
        self.assertNotIn("excel_scope", manifest)
