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
            {"rule_version": "skill-v1", "decision_protocol": ["选择最晚必备专题"]},
        )
        prompt = json.loads(request["input"][0]["content"][0]["text"])
        self.assertEqual(prompt["policy"]["decision_protocol"], ["选择最晚必备专题"])
        self.assertEqual(len(prompt["all_topics_in_order"]), len(self.taxonomy.raw["topics"]))
        self.assertEqual(prompt["question"]["page_scope_hint"], "")

    def test_provider_excludes_suspected_extra_answer_parts(self) -> None:
        question = {
            "exercise_id": "2036200",
            "question_press": "计算：|√2-2|+(π-1)^0-(1/2)^(-1)",
            "answer_press": "(1) 原式=1-√2。(2) 化简 x²/(x-2)。",
        }
        request = self.provider.build_routing_request(question, self.taxonomy, {"rules": {}})
        prompt = json.loads(request["input"][0]["content"][0]["text"])
        self.assertEqual(prompt["question"]["answer"], "")
        self.assertEqual(prompt["question"]["answer_omitted_reason"], "suspected_extra_parts")
        self.assertIn("answer_suspected_extra_parts", prompt["question"]["input_warnings"])

    def test_fast_batch_builds_one_request_for_multiple_questions(self) -> None:
        questions = [self.question, {**self.question, "exercise_id": "2529222"}]
        request = self.provider.build_fast_batch_request(questions, self.taxonomy, {"rule_version": "skill-v1"})
        prompt = json.loads(request["input"][0]["content"][0]["text"])
        result_schema = request["text"]["format"]["schema"]["properties"]["results"]
        self.assertEqual(len(prompt["questions"]), 2)
        self.assertEqual(len(prompt["directory_catalog"]), len(self.taxonomy.raw["topics"]))
        self.assertNotIn("enum", result_schema["items"]["properties"]["target_level3_id"])
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

    def test_topic_batch_only_sends_routed_topic_catalog(self) -> None:
        target = self.taxonomy.all_targets()[0]
        routing = {
            "status": "routed", "latest_topic_id": target.topic_id,
            "required_knowledge_points": ["实数运算"], "confidence": 0.96,
            "reason": "路由完成", "review_reasons": [],
        }
        request = self.provider.build_topic_batch_request([self.question], target.topic_id, self.taxonomy, {"rule_version": "skill-v1"}, {self.question["exercise_id"]: routing})
        static_prompt = json.loads(request["input"][0]["content"][0]["text"])
        self.assertEqual(request["text"]["format"]["name"], "math_topic_batch_classification")
        self.assertEqual([item["id"] for item in static_prompt["directory_catalog"]], [target.topic_id])

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
                }]}, ensure_ascii=False)
                return {"status": "completed", "output_text": content}

        decision = StubTopicProvider({"model": "test-model", "api_key": "test-key-123"}).classify_topic_batch(
            [self.question], target.topic_id, self.taxonomy, {"rule_version": "skill-v1"}, {self.question["exercise_id"]: routing}
        )[0]
        self.assertEqual(decision["routing"]["latest_topic_id"], target.topic_id)
        self.assertIsNone(decision["audit"])

    def test_realtime_classification_runs_route_classify_and_audit(self) -> None:
        target = self.taxonomy.all_targets()[0]

        class StubProvider(OpenAIChatCompletionsProvider):
            def __init__(self, responses: list[dict[str, object]]) -> None:
                super().__init__({"model": "test-model", "api_key": "test-key-123"})
                self.responses = responses
                self.request_names: list[str] = []

            def _request(self, method: str, path: str, payload=None, extra_headers=None, raw=False):
                self.request_names.append(payload["text"]["format"]["name"])
                content = json.dumps(self.responses.pop(0), ensure_ascii=False)
                return {"status": "completed", "output_text": content}

        provider = StubProvider([
            {
                "status": "routed", "required_knowledge_points": ["分母有理化"],
                "latest_topic_id": target.topic_id, "primary_object": "实数式", "main_question": "计算",
                "decisive_condition": "分母有理化", "evidence": ["分母有理化"], "confidence": 0.97,
                "reason": "最晚必备专题为实数运算", "review_reasons": [],
            },
            {
                "status": "suggested", "target_level3_id": target.level3_id,
                "target_level4_id": target.level4_id, "confidence": 0.96, "reason": "唯一命中",
                "review_reasons": [],
                "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None},
            },
            {"passed": True, "violations": [], "reason": "未发现冲突", "confidence": 0.95},
        ])
        decision = provider.classify(self.question, self.taxonomy, {"rule_version": "skill-v1"})
        self.assertEqual(provider.request_names, [
            "math_topic_routing", "math_question_classification", "math_classification_audit",
        ])
        self.assertEqual(decision["routing"]["latest_topic_id"], target.topic_id)
        self.assertTrue(decision["audit"]["passed"])
        self.assertEqual(decision["confidence"], 0.95)

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
                        "status": "suggested", "target_level3_id": target.level3_id,
                        "target_level4_id": target.level4_id, "confidence": 0.95, "reason": "唯一命中",
                        "review_reasons": [],
                        "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None},
                    }
                else:
                    raise CloudProviderError("审核超时")
                return {"status": "completed", "output_text": json.dumps(value, ensure_ascii=False)}

        decision = AuditFailureProvider().classify(self.question, self.taxonomy, {"rule_version": "skill-v1"})
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
