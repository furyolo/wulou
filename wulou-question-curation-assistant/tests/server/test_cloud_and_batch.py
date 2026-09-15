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


class _DirectoryRequestCaptured(Exception):
    """仅用于检查目录方案 Structured Output 请求，不发起真实网络调用。"""


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

    def test_chat_completions_request_and_response_are_adapted(self) -> None:
        provider = OpenAIChatCompletionsProvider({"model": "test-model", "protocol": "chat_completions"})
        request = provider.build_routing_request(self.question, self.taxonomy, {"rules": {}})
        self.assertEqual(provider._protocol_path(), "/chat/completions")
        self.assertNotIn("input", request)
        self.assertEqual(request["messages"][0]["role"], "system")
        self.assertEqual(request["response_format"]["type"], "json_schema")
        self.assertEqual(request["response_format"]["json_schema"]["name"], "math_topic_routing")
        self.assertEqual(provider._decode({"choices": [{"message": {"content": '{"status":"routed"}'}}]}), {"status": "routed"})
        line = json.loads(provider.create_batch_jsonl([self.question], self.taxonomy, {"rules": {}}))
        self.assertEqual(line["url"], "/v1/chat/completions")

    def test_claude_messages_request_and_tool_result_are_adapted(self) -> None:
        provider = OpenAIChatCompletionsProvider({"model": "test-model", "protocol": "anthropic_messages"})
        request = provider.build_routing_request(self.question, self.taxonomy, {"rules": {}})
        self.assertEqual(provider._protocol_path(), "/messages")
        self.assertNotIn("response_format", request)
        self.assertEqual(request["tools"][0]["name"], "submit_classification")
        self.assertEqual(request["tool_choice"], {"type": "tool", "name": "submit_classification"})
        self.assertEqual(
            provider._decode({"content": [{"type": "tool_use", "name": "submit_classification", "input": {"status": "routed"}}]}),
            {"status": "routed"},
        )
        line = json.loads(provider.create_batch_jsonl([self.question], self.taxonomy, {"rules": {}}))
        self.assertTrue(line["custom_id"].startswith("exercise-2529221-"))
        self.assertIn("params", line)
        self.assertEqual(line["params"]["tool_choice"], {"type": "tool", "name": "submit_classification"})

    def test_claude_message_batch_submission_polling_and_result_parsing(self) -> None:
        provider = OpenAIChatCompletionsProvider({"model": "test-model", "protocol": "anthropic_messages", "base_url": "https://api.anthropic.com/v1"})
        request_line = provider.create_batch_jsonl([self.question], self.taxonomy, {"rules": {}}).encode("utf-8")
        calls: list[tuple[str, str, object, dict[str, object]]] = []

        def request(method, path, payload=None, **kwargs):  # type: ignore[no-untyped-def]
            calls.append((method, path, payload, kwargs))
            if method == "POST":
                return {"id": "msgbatch_123", "processing_status": "in_progress"}
            if path == "/messages/batches/msgbatch_123":
                return {"id": "msgbatch_123", "processing_status": "ended", "results_url": "/v1/messages/batches/msgbatch_123/results"}
            if path == "/messages/batches/msgbatch_123/results":
                return (json.dumps({
                    "custom_id": "exercise-2529221-batch", "result": {"type": "succeeded", "message": {
                        "content": [{"type": "tool_use", "name": "submit_classification", "input": {"status": "suggested"}}],
                    }},
                }) + "\n").encode("utf-8")
            raise AssertionError(f"unexpected request: {method} {path}")

        provider._request = request  # type: ignore[method-assign]
        submitted = provider.submit_batch(request_line)
        self.assertEqual(submitted["id"], "msgbatch_123")
        self.assertEqual(submitted["status"], "in_progress")
        self.assertEqual(calls[0][1], "/messages/batches")
        self.assertIn("requests", calls[0][2])
        completed = provider.get_batch("msgbatch_123")
        self.assertEqual(completed["status"], "completed")
        raw = provider.get_batch_result_content(completed)
        record = json.loads(raw.decode("utf-8"))
        self.assertEqual(provider.batch_result_decision(record), ("exercise-2529221-batch", {"status": "suggested"}))
        self.assertEqual(provider.batch_result_decision({"custom_id": "exercise-2529221-error", "result": {"type": "errored"}}), ("exercise-2529221-error", None))

    def test_connection_treats_missing_model_catalog_as_reachable_without_generation(self) -> None:
        for protocol in ("responses", "chat_completions", "anthropic_messages"):
            provider = OpenAIChatCompletionsProvider({"model": "test-model", "protocol": protocol})
            calls: list[tuple[str, str]] = []

            def request(method, request_path, payload, **kwargs):  # type: ignore[no-untyped-def]
                calls.append((method, request_path))
                if method == "GET":
                    raise CloudProviderError("云端模型返回 HTTP 404")
                raise AssertionError("快速连接测试不应触发模型生成")

            provider._request = request  # type: ignore[method-assign]
            result = provider.test_connection()
            self.assertEqual(result["protocol"], protocol)
            self.assertEqual(result["check"], "endpoint_reachable")
            self.assertEqual(calls, [("GET", "/models")])

    def test_connection_prefers_non_generating_model_lookup(self) -> None:
        provider = OpenAIChatCompletionsProvider({"allow_empty_model": True})
        calls: list[tuple[str, str]] = []

        def request(method, path, payload=None, **kwargs):  # type: ignore[no-untyped-def]
            calls.append((method, path))
            return {"data": [{"id": "test-model"}, {"id": "test-model"}, {"id": "other-model"}]}

        provider._request = request  # type: ignore[method-assign]
        result = provider.test_connection()
        self.assertEqual(result["check"], "model_catalog")
        self.assertEqual(result["models"], ["test-model", "other-model"])
        self.assertEqual(calls, [("GET", "/models")])

    def test_base_url_adds_v1_once(self) -> None:
        root_provider = OpenAIChatCompletionsProvider({"model": "test-model", "base_url": "https://api.example.test"})
        existing_version_provider = OpenAIChatCompletionsProvider({"model": "test-model", "base_url": "https://api.example.test/v1/"})
        self.assertEqual(root_provider.base_url, "https://api.example.test/v1")
        self.assertEqual(existing_version_provider.base_url, "https://api.example.test/v1")
        self.assertEqual(root_provider._request_url("/models"), "https://api.example.test/v1/models")
        self.assertEqual(root_provider._request_url("/v1/responses"), "https://api.example.test/v1/responses")

    def test_request_compatibility_is_configured_per_profile(self) -> None:
        provider = OpenAIChatCompletionsProvider({
            "model": "test-model", "request_compatibility": "go_http",
            "extra_headers": {"X-Workspace": "math-curation"},
        })
        self.assertEqual(provider._compatibility_headers(), {
            "User-Agent": "Go-http-client/1.1", "X-Workspace": "math-curation",
        })
        self.assertEqual(
            OpenAIChatCompletionsProvider({"model": "test-model"})._compatibility_headers(),
            {},
        )

    def test_directory_signal_requests_use_low_reasoning_and_compact_question_text(self) -> None:
        provider = OpenAIChatCompletionsProvider({
            "model": "test-model", "directory_reasoning_effort": "low", "directory_batch_size": 60,
        })
        request = provider._structured_request("directory-signals", {"questions": []}, {"type": "object"}, reasoning_effort="low")
        self.assertEqual(request["reasoning"]["effort"], "low")
        compact = provider._directory_question({
            "exercise_id": "1", "question_press": "甲" * 2_000, "answer_press": "乙" * 1_000,
        })
        self.assertLessEqual(len(compact["text"]), 900)
        self.assertLessEqual(len(compact["answer"]), 300)

    def test_directory_refactor_schema_leaves_unique_items_to_server_validation(self) -> None:
        class CapturingProvider(OpenAIChatCompletionsProvider):
            def _directory_question_signals(self, questions):  # type: ignore[no-untyped-def]
                return ([
                    {"exercise_id": str(question["exercise_id"]), "primary_object": "实数", "main_question": "计算", "decisive_condition": "运算"}
                    for question in questions
                ], [])

            def _request_with_retry(self, method, path, payload):  # type: ignore[no-untyped-def]
                self.captured_request = payload
                raise _DirectoryRequestCaptured()

        provider = CapturingProvider({"model": "test-model"})
        context = {
            "selected_level3": [{"id": "l3", "title": "实数计算"}],
            "focus": {"level": 3, "level3_id": "l3"},
            "reference_directory_tree": [],
            "collection": {"sampling": {"mode": "stratified_page", "source_question_count": 3}},
            "questions": [
                {"exercise_id": "1", "question_press": "题 1"},
                {"exercise_id": "2", "question_press": "题 2"},
                {"exercise_id": "3", "question_press": "题 3"},
            ],
            "minimum_level4_question_count": 6,
            "sampled_level4_candidate_min_count": 3,
            "sampled_level4_strong_candidate_min_count": 4,
        }
        with self.assertRaises(_DirectoryRequestCaptured):
            provider.propose_directory_refactor(context, {"rules": {}})
        schema = provider.captured_request["text"]["format"]["schema"]
        supporting_schema = schema["properties"]["level3"]["items"]["properties"]["level4"]["items"]["properties"]["supporting_exercise_ids"]
        self.assertNotIn("uniqueItems", supporting_schema)
        self.assertEqual(supporting_schema["minItems"], 3)
        self.assertEqual(supporting_schema["maxItems"], 6)

    def test_directory_signal_batch_failure_keeps_other_batches(self) -> None:
        class PartialSignalProvider(OpenAIChatCompletionsProvider):
            def _structured_request(self, name, prompt, schema, reasoning_effort=None):  # type: ignore[no-untyped-def]
                return {"exercise_ids": [str(question["exercise_id"]) for question in prompt["questions"]]}

            def _request_with_retry(self, method, path, payload):  # type: ignore[no-untyped-def]
                exercise_ids = payload["exercise_ids"]
                if "1" in exercise_ids:
                    raise CloudProviderError("临时网关失败")
                return {"signals": [
                    {"exercise_id": exercise_id, "primary_object": "实数", "main_question": "计算", "decisive_condition": "运算"}
                    for exercise_id in exercise_ids
                ]}

            def _decode(self, payload):  # type: ignore[no-untyped-def]
                return payload

        provider = PartialSignalProvider({"model": "test-model", "directory_batch_size": 20, "directory_concurrency": 2})
        questions = [{"exercise_id": str(index), "question_press": f"题 {index}"} for index in range(1, 22)]
        signals, failed_ids = provider._directory_question_signals(questions)
        self.assertEqual([item["exercise_id"] for item in signals], ["21"])
        self.assertEqual(failed_ids, [str(index) for index in range(1, 21)])

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
        requests = [
            self.provider.build_request(self.question, self.taxonomy.candidates(target.topic_id, None), {"rules": {}}),
            self.provider.build_routing_request(self.question, self.taxonomy, {"rules": {}}),
            self.provider.build_batch_routing_request([self.question], self.taxonomy, {"rules": {}}),
            self.provider.build_topic_batch_request([self.question], target.topic_id, self.taxonomy, {"rules": {}}, {self.question["exercise_id"]: routing}),
            self.provider.build_fast_batch_request([self.question], self.taxonomy, {"rules": {}}),
        ]
        instruction_texts = []
        for request in requests:
            static_prompt = json.loads(request["input"][0]["content"][0]["text"])
            instruction_texts.append(" ".join(static_prompt.get("instructions") or static_prompt.get("checks") or []))
        self.assertTrue(all("乘方优先于一元正负号" in text for text in instruction_texts))

    def test_all_topic_routing_phases_keep_compound_question_priority(self) -> None:
        target = self.taxonomy.all_targets()[0]
        routing = {"latest_topic_id": target.topic_id, "required_knowledge_points": ["整式", "分式"]}
        requests = [
            self.provider.build_routing_request(self.question, self.taxonomy, {"rules": {}}),
            self.provider.build_batch_routing_request([self.question], self.taxonomy, {"rules": {}}),
            self.provider.build_topic_batch_request(
                [self.question], target.topic_id, self.taxonomy, {"rules": {}},
                {self.question["exercise_id"]: routing},
            ),
            self.provider.build_fast_batch_request([self.question], self.taxonomy, {"rules": {}}),
        ]
        instruction_texts = []
        for request in requests:
            static_prompt = json.loads(request["input"][0]["content"][0]["text"])
            instruction_texts.append(" ".join(static_prompt["instructions"]))
        self.assertTrue(all("多个跨知识点小问" in text for text in instruction_texts))
        self.assertTrue(all("候选核心无法区分轻重" in text for text in instruction_texts))

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
        self.assertIn("reroute_topic_id", item_properties)
        self.assertEqual(item_properties["reroute_topic_id"]["enum"], [topic["id"] for topic in self.taxonomy.topic_catalog()] + [None])
        candidates = self.taxonomy.candidates(target.topic_id, None)
        self.assertEqual(
            item_properties["target_level3_id"]["enum"],
            sorted({candidate.level3_id for candidate in candidates}) + [None],
        )
        self.assertEqual(
            item_properties["target_level4_id"]["enum"],
            sorted({candidate.level4_id for candidate in candidates if candidate.level4_id is not None}) + [None],
        )

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
                        "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None},
                    })
                content = json.dumps({"results": rows}, ensure_ascii=False)
                return {"status": "completed", "output_text": content}

        provider = StubFastProvider({"model": "test-model", "api_key": "test-key-123"})
        decisions = provider.classify_fast_batch(questions, self.taxonomy, {"rule_version": "skill-v1"})
        self.assertEqual([item["exercise_id"] for item in decisions], ["2529221", "2529222"])
        self.assertEqual(decisions[0]["routing"]["latest_topic_id"], target.topic_id)

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
                    "self_check_reason": "路由与目录一致", "self_check_confidence": 0.95, "reroute_topic_id": None,
                }]}, ensure_ascii=False)
                return {"status": "completed", "output_text": content}

        decision = StubTopicProvider({"model": "test-model", "api_key": "test-key-123"}).classify_topic_batch(
            [self.question], target.topic_id, self.taxonomy, {"rule_version": "skill-v1"}, {self.question["exercise_id"]: routing}
        )[0]
        self.assertEqual(decision["routing"]["latest_topic_id"], target.topic_id)
        self.assertTrue(decision["self_check"]["passed"])
    def test_realtime_classification_runs_route_and_directory_classification(self) -> None:
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
                "self_check_reason": "路由与目录一致", "self_check_confidence": 0.96, "reroute_topic_id": None,
            },
        ])
        decision = provider.classify(self.question, self.taxonomy, {"rule_version": "skill-v1"})
        self.assertEqual(provider.request_names, [
            "math_topic_routing", "math_topic_batch_classification",
        ])
        self.assertEqual(decision["routing"]["latest_topic_id"], target.topic_id)
        self.assertEqual(decision["confidence"], 0.96)

    def test_non_object_model_json_is_reported_as_cloud_error(self) -> None:
        response = {"status": "completed", "output_text": "[]"}
        with self.assertRaisesRegex(CloudProviderError, "JSON 对象"):
            self.provider._decode(response)

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
