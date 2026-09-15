from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.classifier import classify, content_hash, normalize_text, validate_model_decision
from server.taxonomy import Taxonomy


class ClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.taxonomy = Taxonomy.from_file(ROOT / "config" / "taxonomy.example.yaml")
        self.rules = {"rule_version": "test-rules"}

    def test_normalizes_whitespace(self) -> None:
        self.assertEqual(normalize_text("  实数\u3000计算\n\n"), "实数 计算")

    def test_cache_content_hash_uses_question_content_not_dynamic_site_context(self) -> None:
        question = {
            "exercise_id": "1", "question_press": "题干", "answer_press": "答案",
            "question_image_url": "https://example.test/question.png",
            "source": "已贴知识点 A", "scope": {"topic_id": "topic-a", "level2_id": "large-a"},
        }
        self.assertEqual(content_hash(question), content_hash({**question, "source": "已贴知识点 B"}))
        self.assertEqual(content_hash(question), content_hash({**question, "scope": {"topic_id": "topic-b", "level2_id": "large-a"}}))
        self.assertNotEqual(content_hash(question), content_hash({**question, "question_press": "变更后的题干"}))
        self.assertNotEqual(content_hash(question), content_hash({**question, "answer_press": "变更后的答案"}))
        self.assertNotEqual(content_hash(question), content_hash({**question, "question_image_url": "https://example.test/changed.png"}))

    def test_unique_keyword_match_still_requires_cloud_skill_review(self) -> None:
        result = classify({
            "exercise_id": "2529221", "question_press": "计算并化简含有分母有理化的根式", "scope": {
                "topic_id": "topic-01-real-numbers", "level2_id": "topic-01-large"
            },
        }, self.taxonomy, self.rules)
        self.assertEqual(result["status"], "review")
        self.assertTrue(result["needs_review"])
        self.assertEqual(result["review_reasons"], ["cloud_model_required_for_skill_protocol"])
        self.assertEqual(result["target"]["level4_id"], "real-number-calculation-rationalization")

    def test_unmatched_question_becomes_proposal_candidate(self) -> None:
        result = classify({
            "exercise_id": "2529222", "question_press": "证明圆周角与圆心角的关系", "scope": {
                "topic_id": "topic-01-real-numbers", "level2_id": "topic-01-large"
            },
        }, self.taxonomy, self.rules)
        self.assertEqual(result["status"], "review")
        self.assertTrue(result["proposal_required"])

    def test_unknown_scope_is_not_silently_reclassified(self) -> None:
        result = classify({
            "exercise_id": "2529223", "question_press": "实际应用", "scope": {"topic_id": "missing"},
        }, self.taxonomy, self.rules)
        self.assertEqual(result["review_reasons"], ["scope_empty"])

    def test_model_result_cannot_escape_candidate_directory(self) -> None:
        result = validate_model_decision({"exercise_id": "1", "scope": {"topic_id": "topic-01-real-numbers", "level2_id": "topic-01-large"}}, {"status": "suggested", "target_level3_id": "missing", "target_level4_id": None, "confidence": 0.99, "reason": "x", "review_reasons": [], "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None}}, self.taxonomy, self.rules)
        self.assertEqual(result["status"], "review")
        self.assertEqual(result["review_reasons"], ["invalid_model_target"])

    def test_valid_model_result_keeps_target_but_requires_low_confidence_review(self) -> None:
        result = validate_model_decision({"exercise_id": "1", "scope": {"topic_id": "topic-01-real-numbers", "level2_id": "topic-01-large"}}, {"status": "suggested", "target_level3_id": "real-number-calculation", "target_level4_id": "real-number-calculation-rationalization", "confidence": 0.80, "reason": "分母有理化", "review_reasons": [], "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None}}, self.taxonomy, self.rules)
        self.assertEqual(result["classification_method"], "model")
        self.assertTrue(result["needs_review"])

    def test_model_confidence_outside_schema_range_is_rejected(self) -> None:
        result = validate_model_decision({"exercise_id": "1"}, {
            "status": "review", "target_level3_id": None, "target_level4_id": None,
            "confidence": 1.2, "reason": "x", "review_reasons": [],
            "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None},
        }, self.taxonomy, self.rules)
        self.assertEqual(result["review_reasons"], ["invalid_model_confidence"])

    def test_leaf_level3_does_not_require_a_level4_target(self) -> None:
        result = validate_model_decision({"exercise_id": "1"}, {
            "status": "review", "target_level3_id": "real-number-application", "target_level4_id": None,
            "confidence": 0.99, "reason": "实数应用", "review_reasons": ["现有目录未提供四级目录，无法完成四级唯一匹配。"],
            "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None},
        }, self.taxonomy, self.rules)
        self.assertEqual(result["status"], "suggested")
        self.assertFalse(result["needs_review"])
        self.assertEqual(result["target"]["level4_id"], None)

    def test_validator_does_not_apply_hardcoded_prerequisite_rule(self) -> None:
        taxonomy = Taxonomy({
            "taxonomy_version": "regression-1",
            "topics": [
                {
                    "id": "topic-real", "title": "专题1：实数", "order": 1,
                    "level2": [{"id": "real-large", "title": "【大题】", "level3": [{
                        "id": "real-calc", "title": "实数综合计算（不含三角比、不含分母有理化）",
                        "knowledge_point_id": "REAL-1", "level4": [],
                    }]}],
                },
                {
                    "id": "topic-trig", "title": "专题12：锐角三角函数", "order": 12,
                    "level2": [{"id": "trig-large", "title": "【大题】", "level3": [{
                        "id": "trig-calc", "title": "实数综合计算（含三角比）",
                        "knowledge_point_id": "TRIG-1", "level4": [],
                    }]}],
                },
            ],
        })
        rules = {"rule_version": "regression-rules"}
        question = {
            "exercise_id": "first-question",
            "question_press": r"(-1)^3+2\tan 60^\circ-\sqrt{12}+(\pi-2)^0",
            "scope": {"topic_id": "topic-real", "level2_id": "real-large"},
        }
        result = validate_model_decision(question, {
            "status": "suggested", "target_level3_id": "real-calc", "target_level4_id": None,
            "confidence": 0.99, "reason": "不含分母有理化", "review_reasons": [],
            "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None},
            "routing": {"latest_topic_id": "topic-real"},
        }, taxonomy, rules)
        self.assertEqual(result["status"], "suggested")
        self.assertEqual(result["target"]["topic_id"], "topic-real")

    def test_later_trigonometry_does_not_override_core_topic_routing(self) -> None:
        taxonomy = Taxonomy({
            "taxonomy_version": "regression-core-topic-1",
            "topics": [
                {
                    "id": "topic-triangle", "title": "专题10：三角形", "order": 10,
                    "level2": [{"id": "triangle-large", "title": "【大题】", "level3": [{
                        "id": "triangle-congruence", "title": "三角形全等", "knowledge_point_id": None, "level4": [],
                    }]}],
                },
                {
                    "id": "topic-trig", "title": "专题12：锐角三角函数", "order": 12,
                    "level2": [{"id": "trig-large", "title": "【大题】", "level3": [{
                        "id": "trig-calc", "title": "三角函数计算", "knowledge_point_id": None, "level4": [],
                    }]}],
                },
            ],
        })
        rules = {"rules": {"large_question_core_topic_start_order": 10}}
        result = validate_model_decision({
            "exercise_id": "core-question",
            "question_press": "证明两个三角形全等，并计算 sin60° 对应的线段长度。",
        }, {
            "status": "suggested", "target_level3_id": "triangle-congruence", "target_level4_id": None,
            "confidence": 0.99, "reason": "全等三角形是证明主线，sin60°仅用于中间计算", "review_reasons": [],
            "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None},
            "routing": {"latest_topic_id": "topic-triangle", "required_knowledge_points": ["三角形全等", "sin60°"]},
        }, taxonomy, rules)
        self.assertEqual(result["status"], "suggested")
        self.assertEqual(result["target"]["topic_id"], "topic-triangle")


if __name__ == "__main__":
    unittest.main()
