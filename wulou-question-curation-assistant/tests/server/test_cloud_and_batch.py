from __future__ import annotations

import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
import sys
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.batch_jobs import BatchStore
from server.providers.openai_responses import (
    CloudProviderError,
    OpenAIChatCompletionsProvider,
    _TransientCloudError,
)
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

    def test_chat_completions_request_and_response_are_adapted(self) -> None:
        provider = OpenAIChatCompletionsProvider({"model": "test-model", "protocol": "chat_completions"})
        request = provider.build_routing_request(self.question, self.taxonomy, {"rules": {}})
        self.assertEqual(provider._protocol_path(), "/chat/completions")
        self.assertNotIn("input", request)
        self.assertEqual(request["messages"][0]["role"], "system")
        self.assertEqual(request["response_format"], {"type": "json_object"})
        output_contract = json.loads(request["messages"][-1]["content"])
        self.assertIn("json_schema", output_contract)
        self.assertIn("latest_topic_id", output_contract["json_schema"]["properties"])
        self.assertEqual(provider._decode({"choices": [{"message": {"content": '{"status":"routed"}'}}]}), {"status": "routed"})
        line = json.loads(provider.create_batch_jsonl([self.question], self.taxonomy, {"rules": {}}))
        self.assertEqual(line["url"], "/v1/chat/completions")

    def test_claude_messages_request_and_json_result_are_adapted(self) -> None:
        provider = OpenAIChatCompletionsProvider({"model": "test-model", "protocol": "anthropic_messages"})
        request = provider.build_routing_request(self.question, self.taxonomy, {"rules": {}})
        self.assertEqual(provider._protocol_path(), "/messages")
        self.assertNotIn("response_format", request)
        self.assertNotIn("tools", request)
        claude_schema = request["output_config"]["format"]["schema"]
        self.assertEqual(request["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("minimum", json.dumps(claude_schema))
        self.assertIn("Must be greater than or equal to 0.", claude_schema["properties"]["confidence"]["description"])
        self.assertEqual(
            provider._decode({"content": [{"type": "text", "text": '{"status":"routed"}'}]}),
            {"status": "routed"},
        )
        with self.assertRaisesRegex(
            CloudProviderError,
            r"stop_reason=max_tokens; content=\[thinking\(-\),text\(0\)\]",
        ):
            provider._decode({"stop_reason": "max_tokens", "content": [
                {"type": "thinking", "thinking": "omitted"}, {"type": "text", "text": ""},
            ]})
        line = json.loads(provider.create_batch_jsonl([self.question], self.taxonomy, {"rules": {}}))
        self.assertTrue(line["custom_id"].startswith("exercise-2529221-"))
        self.assertIn("params", line)
        self.assertEqual(line["params"]["output_config"]["format"]["type"], "json_schema")

    def test_claude_schema_transform_preserves_canonical_constraints_locally(self) -> None:
        schema = {"type": "object", "properties": {"score": {
            "type": "number", "minimum": 0, "maximum": 1,
        }}}
        transformed = OpenAIChatCompletionsProvider._claude_output_schema(schema)
        self.assertEqual(schema["properties"]["score"]["minimum"], 0)
        self.assertNotIn("minimum", transformed["properties"]["score"])
        self.assertIn("Must be less than or equal to 1.", transformed["properties"]["score"]["description"])

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
                        "content": [{"type": "text", "text": '{"status":"suggested"}'}],
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

    def test_topic_batch_includes_excel_classification_basis_for_each_directory(self) -> None:
        taxonomy = Taxonomy({
            "taxonomy_version": "directory-basis-v1",
            "topics": [{
                "id": "topic-fraction", "title": "专题2：分式", "order": 2,
                "level2": [{"id": "fraction-large", "title": "【大题】", "level3": [{
                    "id": "fraction-simplification", "title": "分式的化简与求值",
                    "classification_basis": "主对象为含字母分母的分式。",
                    "level4": [{
                        "id": "fraction-range-domain", "title": "考法6：限定取值范围并保证分式有意义",
                        "classification_basis": "须由分母或约分前限制筛出可代入值；证据题目ID：CS2025EXAMPLE001。",
                    }, {
                        "id": "fraction-composite", "title": "考法4：分式加减乘除的复合化简",
                    }],
                }]}],
            }],
        })
        question = {**self.question, "scope": {"topic_id": "topic-fraction", "level2_id": "fraction-large"}}
        routing = {question["exercise_id"]: {"latest_topic_id": "topic-fraction", "required_knowledge_points": ["分式"]}}
        request = self.provider.build_topic_batch_request([question], "topic-fraction", taxonomy, {"rules": {}}, routing)
        prompt = json.loads(request["input"][0]["content"][0]["text"])
        level3 = prompt["directory_catalog"][0]["level2"][0]["level3"][0]
        self.assertEqual(level3["classification_basis"], "主对象为含字母分母的分式。")
        self.assertEqual(level3["level4"][0]["classification_basis"], "须由分母或约分前限制筛出可代入值")
        self.assertNotIn("CS2025EXAMPLE001", json.dumps(prompt, ensure_ascii=False))
        self.assertNotIn("classification_basis", level3["level4"][1])
        self.assertIn("不能只按标题猜测", " ".join(prompt["instructions"]))
        self.assertIn("未填写只表示", " ".join(prompt["instructions"]))

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


class _FakeResponse:
    """最小化的 urlopen 响应替身，只实现 ``_send_once`` 用到的读取接口。"""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


class CloudRequestRetryTests(unittest.TestCase):
    """瞬时故障重发：网关没受理的失败要重发，已经受理的读超时不许重发。"""

    def setUp(self) -> None:
        # 密钥检查发生在网络调用之前，这里给一个假密钥，好让用例走到传输层。
        self.provider = OpenAIChatCompletionsProvider({"model": "test-model", "api_key": "test-key"})

    def _scripted_sender(self, outcomes: list[Any]) -> tuple[Any, list[str]]:
        """按顺序重放结果或异常并记录每次尝试；用尽之后重复最后一项。"""
        calls: list[str] = []

        def send(method, path, payload=None, extra_headers=None, raw=False, timeout_seconds=None):  # type: ignore[no-untyped-def]
            calls.append(path)
            outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return send, calls

    def test_gateway_overload_is_retried_until_it_succeeds(self) -> None:
        send, calls = self._scripted_sender([_TransientCloudError("云端模型返回 HTTP 502"), {"results": []}])
        self.provider._send_once = send  # type: ignore[method-assign]
        with patch("server.providers.openai_responses.time.sleep") as sleep:
            self.assertEqual(self.provider._request("POST", "/responses", {"x": 1}), {"results": []})
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once()

    def test_exhausted_retries_report_the_attempt_count(self) -> None:
        send, calls = self._scripted_sender([_TransientCloudError("云端模型返回 HTTP 503")])
        self.provider._send_once = send  # type: ignore[method-assign]
        with patch("server.providers.openai_responses.time.sleep"):
            with self.assertRaises(CloudProviderError) as caught:
                self.provider._request("POST", "/responses", {"x": 1})
        self.assertEqual(len(calls), 3)
        self.assertIn("HTTP 503", str(caught.exception))
        self.assertIn("已尝试 3 次", str(caught.exception))

    def test_configured_attempt_count_is_respected(self) -> None:
        provider = OpenAIChatCompletionsProvider({"model": "test-model", "retry_attempts": 2})
        send, calls = self._scripted_sender([_TransientCloudError("无法连接云端模型")])
        provider._send_once = send  # type: ignore[method-assign]
        with patch("server.providers.openai_responses.time.sleep"):
            with self.assertRaises(CloudProviderError):
                provider._request("GET", "/models", None)
        self.assertEqual(len(calls), 2)

    def test_invalid_attempt_count_is_rejected_at_startup(self) -> None:
        for value in (0, 6, "many"):
            with self.assertRaises(ValueError):
                OpenAIChatCompletionsProvider({"model": "test-model", "retry_attempts": value})

    def test_read_timeout_is_never_retried(self) -> None:
        """请求已送达上游：重发会重复计费，还得让用户再等一个完整超时周期。"""
        calls: list[str] = []

        def fake_urlopen(request, timeout=None):  # type: ignore[no-untyped-def]
            calls.append(request.full_url)
            raise TimeoutError("timed out")

        with patch("server.providers.openai_responses.urlopen", fake_urlopen):
            with patch("server.providers.openai_responses.time.sleep") as sleep:
                with self.assertRaises(CloudProviderError) as caught:
                    self.provider._request("GET", "/models", None)
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()
        self.assertIn("未响应", str(caught.exception))

    def test_transport_failures_are_classified_for_retry(self) -> None:
        """网关过载、限流与连接失败可重发；鉴权、参数一类的 4xx 不重发。"""
        def status_error(code: int) -> HTTPError:
            return HTTPError("https://api.example.test/v1/responses", code, "boom", {}, BytesIO(b"{}"))

        with patch("server.providers.openai_responses.urlopen", side_effect=URLError("connection refused")):
            with self.assertRaises(_TransientCloudError):
                self.provider._send_once("POST", "/responses", {"x": 1}, None, False, None)
        for code in (408, 409, 425, 429, 500, 502, 503, 504):
            with patch("server.providers.openai_responses.urlopen", side_effect=status_error(code)):
                with self.assertRaises(_TransientCloudError):
                    self.provider._send_once("POST", "/responses", {"x": 1}, None, False, None)
        for code in (400, 401, 403, 404, 413, 422):
            with patch("server.providers.openai_responses.urlopen", side_effect=status_error(code)):
                with self.assertRaises(CloudProviderError) as caught:
                    self.provider._send_once("POST", "/responses", {"x": 1}, None, False, None)
                self.assertNotIsInstance(caught.exception, _TransientCloudError)

    def test_batch_creation_is_never_resent(self) -> None:
        """批任务没有幂等键：网关抖一下不能让同一批题目被提交两次。"""
        send, calls = self._scripted_sender([{"id": "file-1"}, _TransientCloudError("云端模型返回 HTTP 503")])
        self.provider._send_once = send  # type: ignore[method-assign]
        with patch("server.providers.openai_responses.time.sleep") as sleep:
            with self.assertRaises(CloudProviderError):
                self.provider.submit_batch(b'{"custom_id":"a","method":"POST","url":"/v1/responses","body":{}}\n')
        self.assertEqual(calls, ["/files", "/batches"])
        sleep.assert_not_called()

    def test_connection_test_rides_out_a_gateway_blip(self) -> None:
        """连接测试若因一次网关抖动就报失败，用户会误以为自己配置写错了。"""
        calls: list[str] = []

        def fake_urlopen(request, timeout=None):  # type: ignore[no-untyped-def]
            calls.append(request.full_url)
            if len(calls) == 1:
                raise URLError("connection reset")
            return _FakeResponse(b'{"data": [{"id": "test-model"}]}')

        with patch("server.providers.openai_responses.urlopen", fake_urlopen):
            with patch("server.providers.openai_responses.time.sleep"):
                result = self.provider.test_connection()
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["models"], ["test-model"])


if __name__ == "__main__": unittest.main()
