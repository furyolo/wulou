from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.batch_jobs import BatchStore
from server.providers.openai_responses import CloudProviderError, OpenAIChatCompletionsProvider
from server.taxonomy import Taxonomy


class CloudAndBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.taxonomy = Taxonomy.from_file(ROOT / "config" / "taxonomy.example.yaml")
        self.question = {"exercise_id": "2529221", "question_press": "分母有理化", "scope": {"topic_id": "topic-01-real-numbers", "level2_id": "topic-01-large"}}
        self.provider = OpenAIChatCompletionsProvider({"model": "test-model", "api_key_env": "MISSING_TEST_KEY"})

    def test_batch_jsonl_has_one_independent_request_per_question(self) -> None:
        lines = self.provider.create_batch_jsonl([self.question, {**self.question, "exercise_id": "2529222"}], self.taxonomy, {"rules": {}}).splitlines()
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["url"], "/v1/responses")
        self.assertTrue(first["custom_id"].startswith("exercise-2529221-"))
        self.assertEqual(first["body"]["model"], "test-model")
        self.assertIn("input", first["body"])
        self.assertEqual(first["body"]["text"]["format"]["type"], "json_schema")
        prompt = json.loads(first["body"]["input"][0]["content"][0]["text"])
        self.assertIsNone(prompt["topic_routing"])
        self.assertIn("网页目录只是弱提示", first["body"]["instructions"])

    def test_responses_request_supports_none_and_max_reasoning_effort(self) -> None:
        for effort in ("none", "max"):
            provider = OpenAIChatCompletionsProvider({"model": "test-model", "reasoning_effort": effort})
            request = provider.build_routing_request(self.question, self.taxonomy, {"rules": {}})
            self.assertEqual(request["reasoning"]["effort"], effort)

    def test_realtime_routing_sees_all_topics_and_skill_protocol(self) -> None:
        request = self.provider.build_routing_request(
            self.question,
            self.taxonomy,
            {"rule_version": "skill-v1", "decision_protocol": ["按分阶段规则选择最终归属专题"]},
        )
        prompt = json.loads(request["input"][0]["content"][0]["text"])
        self.assertEqual(prompt["policy"]["decision_protocol"], ["按分阶段规则选择最终归属专题"])
        self.assertEqual(len(prompt["all_topics_in_order"]), len(self.taxonomy.raw["topics"]))
        self.assertEqual(prompt["question"]["page_scope_hint"], "")

    def test_all_llm_phases_apply_standard_math_notation_convention(self) -> None:
        target = self.taxonomy.all_targets()[0]
        routing = {"latest_topic_id": target.topic_id, "required_knowledge_points": ["实数运算"]}
        decision = {"target_level3_id": target.level3_id, "target_level4_id": target.level4_id}
        requests = [
            self.provider.build_request(self.question, self.taxonomy.candidates(target.topic_id, None), {"rules": {}}),
            self.provider.build_routing_request(self.question, self.taxonomy, {"rules": {}}),
            self.provider.build_batch_routing_request([self.question], self.taxonomy, {"rules": {}}),
            self.provider.build_topic_batch_request([self.question], target.topic_id, self.taxonomy, {"rules": {}}, {self.question["exercise_id"]: routing}),
            self.provider.build_audit_request(self.question, routing, decision, target, self.taxonomy, {"rules": {}}),
            self.provider.build_batch_audit_request([(self.question, routing, decision, target)], self.taxonomy, {"rules": {}}),
            self.provider.build_fast_batch_request([self.question], self.taxonomy, {"rules": {}}),
        ]
        instruction_texts = []
        for request in requests:
            static_prompt = json.loads(request["input"][0]["content"][0]["text"])
            instruction_texts.append(" ".join(static_prompt.get("instructions") or static_prompt.get("checks") or []))
        self.assertTrue(all("乘方优先于一元正负号" in text for text in instruction_texts))

    def test_provider_keeps_numbering_mismatched_answer_for_semantic_review(self) -> None:
        question = {
            "exercise_id": "2036200",
            "question_press": "计算：|√2-2|+(π-1)^0-(1/2)^(-1)",
            "answer_press": "(1) 原式=1-√2。(2) 化简 x²/(x-2)。",
        }
        request = self.provider.build_routing_request(question, self.taxonomy, {"rules": {}})
        prompt = json.loads(request["input"][0]["content"][0]["text"])
        self.assertEqual(prompt["question"]["answer"], "(1) 原式=1-√2。(2) 化简 x²/(x-2)。")
        self.assertIsNone(prompt["question"]["answer_omitted_reason"])
        self.assertTrue(prompt["question"]["answer_part_numbering_mismatch"])
        self.assertIn("answer_part_numbering_mismatch", prompt["question"]["input_warnings"])
        self.assertTrue(any("数学连续性" in item for item in prompt["instructions"]))

    def test_fast_batch_builds_one_request_for_multiple_questions(self) -> None:
        questions = [self.question, {**self.question, "exercise_id": "2529222"}]
        request = self.provider.build_fast_batch_request(questions, self.taxonomy, {"rule_version": "skill-v1"})
        prompt = json.loads(request["input"][0]["content"][0]["text"])
        result_schema = request["text"]["format"]["schema"]["properties"]["results"]
        item_properties = result_schema["items"]["properties"]
        self.assertEqual(len(prompt["questions"]), 2)
        self.assertEqual(len(prompt["directory_catalog"]), len(self.taxonomy.raw["topics"]))
        self.assertEqual(
            item_properties["target_level3_id"]["enum"],
            sorted({target.level3_id for target in self.taxonomy.all_targets()}) + [None],
        )
        self.assertEqual(
            item_properties["target_level4_id"]["enum"],
            sorted({target.level4_id for target in self.taxonomy.all_targets() if target.level4_id is not None}) + [None],
        )
        self.assertEqual(
            item_properties["latest_topic_id"]["enum"],
            [topic["id"] for topic in self.taxonomy.topic_catalog()] + [None],
        )
        self.assertEqual(result_schema["minItems"], 2)
        self.assertEqual(result_schema["maxItems"], 2)

    def test_batch_routing_keeps_static_catalog_before_dynamic_questions(self) -> None:
        questions = [self.question, {**self.question, "exercise_id": "2529222"}]
        request = self.provider.build_batch_routing_request(questions, self.taxonomy, {"rule_version": "skill-v1"})
        static_prompt = json.loads(request["input"][0]["content"][0]["text"])
        dynamic_prompt = json.loads(request["input"][1]["content"][0]["text"])
        self.assertEqual(request["text"]["format"]["name"], "math_batch_topic_routing")
        self.assertIn("all_topics_in_order", static_prompt)
        self.assertNotIn("questions", static_prompt)
        self.assertEqual(len(dynamic_prompt["questions"]), 2)

    def test_topic_batch_sends_routed_directory_and_global_self_check_catalog(self) -> None:
        target = self.taxonomy.all_targets()[0]
        routing = {
            "status": "routed", "latest_topic_id": target.topic_id,
            "required_knowledge_points": ["实数运算"], "confidence": 0.96,
            "reason": "路由完成", "review_reasons": [],
        }
        request = self.provider.build_topic_batch_request([self.question], target.topic_id, self.taxonomy, {"rule_version": "skill-v1"}, {self.question["exercise_id"]: routing})
        static_prompt = json.loads(request["input"][0]["content"][0]["text"])
        item_properties = request["text"]["format"]["schema"]["properties"]["results"]["items"]["properties"]
        self.assertEqual(request["text"]["format"]["name"], "math_topic_batch_classification")
        self.assertEqual([item["id"] for item in static_prompt["directory_catalog"]], [target.topic_id])
        self.assertEqual(len(static_prompt["all_topics_in_order"]), len(self.taxonomy.raw["topics"]))
        self.assertIn("先独立复核", static_prompt["instructions"][0])
        self.assertIn("self_check_passed", item_properties)
        candidates = self.taxonomy.candidates(target.topic_id, None)
        self.assertEqual(
            item_properties["target_level3_id"]["enum"],
            sorted({candidate.level3_id for candidate in candidates}) + [None],
        )
        self.assertEqual(
            item_properties["target_level4_id"]["enum"],
            sorted({candidate.level4_id for candidate in candidates if candidate.level4_id is not None}) + [None],
        )

    def test_batch_audit_uses_all_topics_and_does_not_rely_on_hardcoded_signals(self) -> None:
        target = self.taxonomy.all_targets()[0]
        routing = {"latest_topic_id": target.topic_id, "required_knowledge_points": ["实数运算"]}
        decision = {"target_level3_id": target.level3_id, "target_level4_id": target.level4_id}
        request = self.provider.build_batch_audit_request(
            [(self.question, routing, decision, target)], self.taxonomy, {"rule_version": "skill-v1"}
        )
        static_prompt = json.loads(request["input"][0]["content"][0]["text"])
        dynamic_prompt = json.loads(request["input"][1]["content"][0]["text"])
        self.assertEqual(request["text"]["format"]["name"], "math_batch_classification_audit")
        self.assertEqual(len(static_prompt["all_topics_in_order"]), len(self.taxonomy.raw["topics"]))
        self.assertIn("不得依赖任何硬编码知识点特判", static_prompt["task"])
        self.assertEqual(dynamic_prompt["items"][0]["proposed_target"]["level3_id"], target.level3_id)

    def test_fast_batch_maps_each_result_to_skill_validation_shape(self) -> None:
        target = self.taxonomy.all_targets()[0]
        questions = [self.question, {**self.question, "exercise_id": "2529222"}]

        class StubFastProvider(OpenAIChatCompletionsProvider):
            def _request(self, method: str, path: str, payload=None, extra_headers=None, raw=False, timeout_seconds=None):
                rows = []
                for question in questions:
                    rows.append({
                        "exercise_id": question["exercise_id"], "status": "suggested",
                        "required_knowledge_points": ["实数运算"], "latest_topic_id": target.topic_id,
                        "primary_object": "实数式", "main_question": "计算", "decisive_condition": "常规运算",
                        "target_level3_id": target.level3_id, "target_level4_id": target.level4_id,
                        "confidence": 0.93, "reason": "唯一命中", "review_reasons": [],
                        "audit_passed": True, "audit_violations": [],
                        "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None},
                    })
                content = json.dumps({"results": rows}, ensure_ascii=False)
                return {"status": "completed", "output_text": content}

        provider = StubFastProvider({"model": "test-model", "api_key": "test-key-123"})
        decisions = provider.classify_fast_batch(questions, self.taxonomy, {"rule_version": "skill-v1"})
        self.assertEqual([item["exercise_id"] for item in decisions], ["2529221", "2529222"])
        self.assertEqual(decisions[0]["routing"]["latest_topic_id"], target.topic_id)
        self.assertTrue(decisions[0]["audit"]["passed"])

    def test_topic_batch_maps_routing_back_to_each_result(self) -> None:
        target = self.taxonomy.all_targets()[0]
        routing = {
            "status": "routed", "latest_topic_id": target.topic_id,
            "required_knowledge_points": ["实数运算"], "confidence": 0.96,
            "reason": "路由完成", "review_reasons": [],
        }

        class StubTopicProvider(OpenAIChatCompletionsProvider):
            def _request(self, method: str, path: str, payload=None, extra_headers=None, raw=False, timeout_seconds=None):
                content = json.dumps({"results": [{
                    "exercise_id": "2529221", "status": "suggested",
                    "target_level3_id": target.level3_id, "target_level4_id": target.level4_id,
                    "confidence": 0.95, "reason": "唯一命中", "review_reasons": [],
                    "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None},
                    "self_check_passed": True, "self_check_violations": [],
                    "self_check_reason": "路由与目录一致", "self_check_confidence": 0.95,
                }]}, ensure_ascii=False)
                return {"status": "completed", "output_text": content}

        decision = StubTopicProvider({"model": "test-model", "api_key": "test-key-123"}).classify_topic_batch(
            [self.question], target.topic_id, self.taxonomy, {"rule_version": "skill-v1"}, {self.question["exercise_id"]: routing}
        )[0]
        self.assertEqual(decision["routing"]["latest_topic_id"], target.topic_id)
        self.assertTrue(decision["self_check"]["passed"])
        self.assertIsNone(decision["audit"])

    def test_realtime_classification_runs_route_classify_and_audit(self) -> None:
        target = self.taxonomy.all_targets()[0]

        class StubProvider(OpenAIChatCompletionsProvider):
            def __init__(self, responses: list[dict[str, object]]) -> None:
                super().__init__({"model": "test-model", "api_key": "test-key-123"})
                self.responses = responses
                self.request_names: list[str] = []

            def _request(self, method: str, path: str, payload=None, extra_headers=None, raw=False):
                request_name = payload["text"]["format"]["name"]
                self.request_names.append(request_name)
                value = self.responses.pop(0)
                content = json.dumps({"results": [value]} if request_name == "math_topic_batch_classification" else value, ensure_ascii=False)
                return {"status": "completed", "output_text": content}

        provider = StubProvider([
            {
                "status": "routed", "required_knowledge_points": ["分母有理化"],
                "latest_topic_id": target.topic_id, "primary_object": "实数式", "main_question": "计算",
                "decisive_condition": "分母有理化", "evidence": ["分母有理化"], "confidence": 0.97,
                "reason": "最终归属专题为实数运算", "review_reasons": [],
            },
            {
                "exercise_id": self.question["exercise_id"], "status": "suggested", "target_level3_id": target.level3_id,
                "target_level4_id": target.level4_id, "confidence": 0.96, "reason": "唯一命中",
                "review_reasons": [],
                "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None},
                "self_check_passed": True, "self_check_violations": [],
                "self_check_reason": "路由与目录一致", "self_check_confidence": 0.96,
            },
            {"passed": True, "violations": [], "reason": "未发现冲突", "confidence": 0.95},
        ])
        decision = provider.classify(self.question, self.taxonomy, {"rule_version": "skill-v1"}, audit_mode="always")
        self.assertEqual(provider.request_names, [
            "math_topic_routing", "math_topic_batch_classification", "math_classification_audit",
        ])
        self.assertEqual(decision["routing"]["latest_topic_id"], target.topic_id)
        self.assertTrue(decision["audit"]["passed"])
        self.assertEqual(decision["confidence"], 0.95)

    def test_disabled_audit_stops_after_routing_and_directory_classification(self) -> None:
        target = self.taxonomy.all_targets()[0]
        decision = {
            "self_check": {"passed": False, "violations": ["需复核"]},
        }
        self.assertFalse(self.provider._requires_independent_audit(
            self.question, decision, target, {"rules": {}}, "disabled"
        ))
        disabled = self.provider._disabled_audit()
        self.assertEqual(disabled["mode"], "disabled")
        self.assertIsNone(disabled["passed"])

    def test_non_object_model_json_is_reported_as_cloud_error(self) -> None:
        response = {"status": "completed", "output_text": "[]"}
        with self.assertRaisesRegex(CloudProviderError, "JSON 对象"):
            self.provider._decode(response)

    def test_audit_failure_keeps_candidate_as_review(self) -> None:
        target = self.taxonomy.all_targets()[0]

        class AuditFailureProvider(OpenAIChatCompletionsProvider):
            def __init__(self) -> None:
                super().__init__({"model": "test-model", "api_key": "test-key-123"})
                self.call_count = 0

            def _request(self, method: str, path: str, payload=None, extra_headers=None, raw=False):
                self.call_count += 1
                if self.call_count == 1:
                    value = {
                        "status": "routed", "required_knowledge_points": ["实数运算"],
                        "latest_topic_id": target.topic_id, "primary_object": "实数式", "main_question": "计算",
                        "decisive_condition": "常规运算", "evidence": ["计算"], "confidence": 0.96,
                        "reason": "路由完成", "review_reasons": [],
                    }
                elif self.call_count == 2:
                    value = {
                        "exercise_id": "2529221", "status": "suggested", "target_level3_id": target.level3_id,
                        "target_level4_id": target.level4_id, "confidence": 0.95, "reason": "唯一命中",
                        "review_reasons": [],
                        "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None},
                        "self_check_passed": True, "self_check_violations": [],
                        "self_check_reason": "路由与目录一致", "self_check_confidence": 0.95,
                    }
                else:
                    raise CloudProviderError("审核超时")
                request_name = payload["text"]["format"]["name"]
                content = {"results": [value]} if request_name == "math_topic_batch_classification" else value
                return {"status": "completed", "output_text": json.dumps(content, ensure_ascii=False)}

        decision = AuditFailureProvider().classify(
            self.question, self.taxonomy, {"rule_version": "skill-v1"}, audit_mode="always"
        )
        self.assertEqual(decision["status"], "review")
        self.assertEqual(decision["target_level3_id"], target.level3_id)
        self.assertIn("audit_request_failed", decision["review_reasons"])

    def test_batch_store_keeps_local_input_without_submission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BatchStore(Path(directory))
            try:
                job = store.create("{\"x\":1}\n", [self.question])
                self.assertEqual(store.get(job["job_id"])["status"], "exported")
                self.assertEqual(store.input_bytes(job["job_id"]), b'{"x":1}\n')
            finally:
                store.close()


if __name__ == "__main__": unittest.main()
