from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.model_input import build_model_input_snapshot


class ModelInputSnapshotTests(unittest.TestCase):
    def test_snapshot_cleans_markup_redacts_identifiers_and_keeps_math_alternatives(self) -> None:
        snapshot = build_model_input_snapshot({
            "question_press": "<p>计算&nbsp; \\sqrt{2}</p> 联系 13800138000 或 a@example.com",
            "question_latex": r"\sqrt{2}",
            "answer_press": "<b>答案</b>：2",
            "source": "来源 13800138000",
            "question_image_url": "https://example.test/question.png?token=secret",
        })
        self.assertEqual(snapshot["question"]["text"], "计算 \\sqrt{2} 联系 [已隐藏手机号] 或 [已隐藏邮箱]")
        self.assertEqual(snapshot["question"]["latex"], r"\sqrt{2}")
        self.assertEqual(snapshot["answer"]["text"], "答案 ：2")
        self.assertTrue(snapshot["question_image_available"])
        self.assertEqual(snapshot["site_context"], "来源 [已隐藏手机号]")
        self.assertNotIn("token", str(snapshot))

    def test_snapshot_warns_when_typesetting_or_latex_is_incomplete(self) -> None:
        snapshot = build_model_input_snapshot({
            "question_press": "\ue001根式",
            "answer_press": "答案",
            "question_latex": r"\frac{1{2}",
        })
        self.assertIn("question_unrecognized_typesetting", snapshot["warnings"])
        self.assertIn("question_unbalanced_latex", snapshot["warnings"])

    def test_snapshot_converts_known_fangzheng_math_codes(self) -> None:
        snapshot = build_model_input_snapshot({
            "question_press": "计算: 〖JB(|〗〖KF(〗2〖KF)〗-2〖JB)|〗+〖JB((〗π-1〖JB))〗0-〖JB((〗〖SX(〗1〖〗2〖SX)〗〖JB))〗-1.",
            "answer_press": "答案：1-√2",
        })
        text = snapshot["question"]["text"]
        self.assertIn("|√(2)-2|", text)
        self.assertIn("(π-1)^(0)", text)
        self.assertIn("((1)/(2))^(-1)", text)
        self.assertNotIn("question_unrecognized_typesetting", snapshot["warnings"])

    def test_snapshot_removes_inline_private_use_layout_separators(self) -> None:
        snapshot = build_model_input_snapshot({
            "question_press": "计算:-1^(2\ue5e6026)+√(9)-3tan\ue5e645°+((1)/(2))^(-1).",
            "answer_press": "解: 原式=-1+3-3×1+2=1.",
        })
        self.assertEqual(snapshot["question"]["text"], "计算:-1^(2026)+√(9)-3tan45°+((1)/(2))^(-1).")
        self.assertNotIn("question_unrecognized_typesetting", snapshot["warnings"])

    def test_snapshot_keeps_answer_with_parts_missing_from_question_for_llm_review(self) -> None:
        snapshot = build_model_input_snapshot({
            "question_press": "计算：|√2-2|+(π-1)^0-(1/2)^(-1)",
            "answer_press": "(1) 原式=1-√2。(2) 化简 x²/(x-2)。",
        })
        self.assertEqual(snapshot["answer"]["text"], "(1) 原式=1-√2。(2) 化简 x²/(x-2)。")
        self.assertTrue(snapshot["answer"]["used_for_classification"])
        self.assertTrue(snapshot["answer"]["part_numbering_mismatch"])
        self.assertIsNone(snapshot["answer"]["omitted_reason"])
        self.assertIn("(2)", snapshot["answer"]["captured_text"])
        self.assertIn("answer_part_numbering_mismatch", snapshot["warnings"])

    def test_formula_parentheses_are_not_mistaken_for_question_parts(self) -> None:
        snapshot = build_model_input_snapshot({
            "question_press": "计算: 〖JB(|〗〖KF(〗2〖KF)〗-2〖JB)|〗+〖JB((〗π-1〖JB))〗0-〖JB((〗〖SX(〗1〖〗2〖SX)〗〖JB))〗-1.",
            "answer_press": "解: (1)原式=1-√(2). (2)原式=(x^(2))/((x-2)(x+2)).",
        })
        self.assertEqual(snapshot["question"]["text"], "计算: |√(2)-2|+(π-1)^(0)-((1)/(2))^(-1).")
        self.assertEqual(snapshot["answer"]["text"], "解: (1)原式=1-√(2). (2)原式=(x^(2))/((x-2)(x+2)).")
        self.assertTrue(snapshot["answer"]["used_for_classification"])
        self.assertIn("answer_part_numbering_mismatch", snapshot["warnings"])

    def test_matching_question_parts_keep_the_answer(self) -> None:
        snapshot = build_model_input_snapshot({
            "question_press": "（1）计算√4；（2）化简√8。",
            "answer_press": "解：（1）2；（2）2√2。",
        })
        self.assertTrue(snapshot["answer"]["used_for_classification"])
        self.assertEqual(snapshot["answer"]["text"], "解：（1）2；（2）2√2。")
        self.assertFalse(snapshot["answer"]["part_numbering_mismatch"])
        self.assertNotIn("answer_part_numbering_mismatch", snapshot["warnings"])


if __name__ == "__main__":
    unittest.main()
