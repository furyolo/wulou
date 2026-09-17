from __future__ import annotations

import http.client
import json
from copy import deepcopy
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.main import CurationServer, MAX_INTERACTIVE_CLASSIFICATION_QUESTIONS, ServiceState, public_cloud_error_message
from server.providers.openai_responses import CloudProviderError
from server.taxonomy import Taxonomy


class HttpServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.tempdir.name)
        settings_path = self.temp_path / "settings.yaml"
        settings_path.write_text(yaml.safe_dump({
            "host": "127.0.0.1",
            "port": 0,
            "taxonomy_path": str(ROOT / "config" / "taxonomy.example.yaml"),
            "rules_path": str(ROOT / "config" / "classification-rules.yaml"),
            "cache_path": "cache.sqlite3",
        }, allow_unicode=True), encoding="utf-8")
        self.state = ServiceState(settings_path)
        self.server = CurationServer(("127.0.0.1", 0), self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.state.close()
        self.thread.join(timeout=2)
        self.tempdir.cleanup()

    def request(self, method: str, path: str, body: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        headers = {"Content-Type": "application/json"}
        raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        connection.request(method, path, body=raw_body, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    def test_health_is_available_without_token(self) -> None:
        status, payload = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["port"], self.server.server_port)

    def test_protocol_diagnostics_are_not_exposed_to_browser_messages(self) -> None:
        error = CloudProviderError(
            "云端 Claude Messages 未返回可解析的结构化结果"
            "（stop_reason=max_tokens; content=[text(0)]）"
        )
        message = public_cloud_error_message(error)
        self.assertNotIn("stop_reason", message)
        self.assertNotIn("content=[", message)
        self.assertIn("请稍后重试", message)

    def test_taxonomy_is_available_on_loopback_without_token(self) -> None:
        status, payload = self.request("GET", "/api/v1/taxonomy")
        self.assertEqual(status, 200)
        self.assertIn("topics", payload)

    def test_classification_is_cached(self) -> None:
        body = {
            "exercise_id": "2529221",
            "question_press": "计算并化简含有分母有理化的根式",
            "scope": {"topic_id": "topic-01-real-numbers", "level2_id": "topic-01-large"},
        }
        first_status, first = self.request("POST", "/api/v1/classify", body)
        second_status, second = self.request("POST", "/api/v1/classify", body)
        self.assertEqual(first_status, 200)
        self.assertFalse(first["cache_hit"])
        self.assertEqual(first["model_input_snapshot"]["question"]["text"], body["question_press"])
        self.assertEqual(second_status, 200)
        self.assertTrue(second["cache_hit"])
        self.assertEqual(second["target"]["level4_id"], "real-number-calculation-rationalization")

        deleted_status, deleted = self.request("POST", "/api/v1/cache/classifications/delete", {
            "exercise_ids": [body["exercise_id"]],
        })
        self.assertEqual(deleted_status, 200)
        self.assertEqual(deleted["deleted"], 1)
        refreshed_status, refreshed = self.request("POST", "/api/v1/classify", body)
        self.assertEqual(refreshed_status, 200)
        self.assertFalse(refreshed["cache_hit"])

    def test_cached_classifications_can_be_recovered_after_a_lost_browser_callback(self) -> None:
        body = {
            "exercise_id": "cache-recovery-1",
            "question_press": "计算并化简含有分母有理化的根式",
            "current_catalogue_id": "level4-a",
            "scope": {"topic_id": "topic-01-real-numbers", "level2_id": "topic-01-large"},
        }
        classified_status, _ = self.request("POST", "/api/v1/classify", body)
        lookup_status, lookup = self.request(
            "POST", "/api/v1/cache/classifications/lookup", {"questions": [body]}
        )
        self.assertEqual(classified_status, 200)
        self.assertEqual(lookup_status, 200)
        self.assertEqual(lookup["missing_exercise_ids"], [])
        self.assertEqual(lookup["results"][0]["exercise_id"], body["exercise_id"])
        self.assertTrue(lookup["results"][0]["cache_hit"])

    def test_cache_recovery_ignores_dynamic_card_text(self) -> None:
        body = {
            "exercise_id": "cache-dynamic-source-1",
            "question_press": "计算并化简含有分母有理化的根式",
            "answer_press": "答案文本",
            "question_image_url": "https://example.test/question.png",
            "current_catalogue_id": "level4-a",
            "source": "已贴知识点分组：旧页面状态",
            "scope": {"topic_id": "topic-01-real-numbers", "level2_id": "topic-01-large"},
        }
        self.request("POST", "/api/v1/classify", body)
        refreshed = {
            **body,
            "source": "已贴知识点分组：刷新后的页面状态",
            "scope": {"topic_id": "topic-99", "level2_id": "large-99"},
        }
        status, lookup = self.request(
            "POST", "/api/v1/cache/classifications/lookup", {"questions": [refreshed]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(lookup["missing_exercise_ids"], [])
        self.assertTrue(lookup["results"][0]["cache_hit"])

    def test_manual_classification_overrides_model_cache_in_the_same_directory_context(self) -> None:
        # 目标必须是示例目录里真实存在的落点：目录被改名或撤销后人工结论会失效，
        # 这条测的是"仍然成立时它必须压过模型缓存"。
        target = self.state.taxonomy.global_target(
            "real-number-calculation", "real-number-calculation-rationalization"
        )
        self.assertIsNotNone(target)
        assert target is not None
        target_path = target.published_path
        body = {
            "exercise_id": "manual-override-1",
            "stable_code": "CS2026MANUAL001",
            "current_catalogue_id": "level4-a",
        }
        saved_status, saved = self.request("POST", "/api/v1/manual-classifications", {
            **body,
            "original_target_path": ["专题一 实数", "【大题】", "实数的应用"],
            "target_path": target_path,
        })
        self.assertEqual(saved_status, 201)
        self.assertEqual(saved["source_catalogue_id"], "level4-a")

        lookup_status, lookup = self.request(
            "POST", "/api/v1/cache/classifications/lookup", {"questions": [body]}
        )
        self.assertEqual(lookup_status, 200)
        self.assertEqual(lookup["missing_exercise_ids"], [])
        result = lookup["results"][0]
        self.assertEqual(result["target"]["path"], target_path)
        self.assertEqual(result["manual_override"]["source"], "manual")

        # 题目移到人工指定的目标叶子后，恢复缓存时目录 ID 已变；
        # 人工决定仍必须压过旧的模型复核缓存。
        moved_lookup_status, moved_lookup = self.request(
            "POST", "/api/v1/cache/classifications/lookup", {"questions": [{
                **body,
                "current_catalogue_id": "level4-target",
            }]}
        )
        self.assertEqual(moved_lookup_status, 200)
        moved_result = moved_lookup["results"][0]
        self.assertEqual(moved_result["target"]["path"], target_path)
        self.assertEqual(moved_result["manual_override"]["source"], "manual")

    def test_manual_classification_is_not_reused_after_its_target_directory_disappears(self) -> None:
        """原目标目录被撤销时才不复用；不是版本号一动就整批丢弃。"""
        body = {
            "exercise_id": "manual-reclassify-on-taxonomy-change-1",
            "stable_code": "CS2026MANUAL002",
            "current_catalogue_id": "level4-a",
            "question_press": "计算并化简含有分母有理化的根式",
            "scope": {"topic_id": "topic-01-real-numbers", "level2_id": "topic-01-large"},
        }
        target = self.state.taxonomy.global_target("real-number-application", None)
        self.assertIsNotNone(target)
        assert target is not None
        saved_status, saved = self.request("POST", "/api/v1/manual-classifications", {
            **body,
            "original_target_path": [],
            "target_path": target.published_path,
        })
        self.assertEqual(saved_status, 201)
        old_taxonomy_version = saved["taxonomy_version"]

        # 换版本的同时把该三级目录撤掉：这才是让旧结论失效的真正原因。
        updated_taxonomy = deepcopy(self.state.taxonomy.raw)
        updated_taxonomy["taxonomy_version"] = f"{old_taxonomy_version}-updated"
        level2 = updated_taxonomy["topics"][0]["level2"][0]
        level2["level3"] = [
            item for item in level2["level3"] if item["id"] != "real-number-application"
        ]
        self.state.taxonomy = Taxonomy(updated_taxonomy)

        lookup_status, lookup = self.request(
            "POST", "/api/v1/cache/classifications/lookup", {"questions": [body]}
        )
        self.assertEqual(lookup_status, 200)
        self.assertEqual(lookup["results"], [])
        self.assertEqual(lookup["missing_exercise_ids"], [body["exercise_id"]])

        classified_status, classified = self.request("POST", "/api/v1/classify", body)
        self.assertEqual(classified_status, 200)
        self.assertFalse(classified["cache_hit"])
        self.assertEqual(classified["taxonomy_version"], self.state.taxonomy.version)
        self.assertNotIn("manual_override", classified)

    def test_catalogue_move_history_reports_latest_confirmed_move(self) -> None:
        first = {
            "exercise_id": "move-history-1", "stable_code": "CS2026MOVE001",
            "source_catalogue_id": "old-leaf", "target_catalogue_id": "middle-leaf",
            "original_path": ["专题1：实数", "【大题】", "旧分类"],
            "target_path": ["专题4：分式方程与不等式", "【大题】", "解不等式"],
        }
        second = {
            **first,
            "source_catalogue_id": "middle-leaf", "target_catalogue_id": "new-leaf",
            "original_path": first["target_path"],
            "target_path": ["专题10：三角形", "【大题】", "全等三角形", "考法2"],
        }
        self.assertEqual(self.request("POST", "/api/v1/history/catalogue-moves", first)[0], 201)
        self.assertEqual(self.request("POST", "/api/v1/history/catalogue-moves", second)[0], 201)
        status, report = self.request("GET", "/api/v1/history/catalogue-moves")
        self.assertEqual(status, 200)
        self.assertEqual(report["summary"]["classified_count"], 1)
        self.assertEqual(report["summary"]["topics"], ["专题4：分式方程与不等式"])
        self.assertEqual(report["records"][0]["stable_code"], "CS2026MOVE001")
        self.assertEqual(report["records"][0]["original_path"], first["target_path"])
        self.assertEqual(report["records"][0]["target_path"], second["target_path"])

    def test_catalogue_move_history_allows_unknown_original_path_but_not_missing_target(self) -> None:
        body = {
            "exercise_id": "move-history-unknown-source", "stable_code": "CS2026MOVE003",
            "source_catalogue_id": "outside-page-tree", "target_catalogue_id": "level3-leaf",
            "original_path": [],
            "target_path": ["专题4：分式方程与不等式", "【大题】", "解不等式"],
        }
        status, saved = self.request("POST", "/api/v1/history/catalogue-moves", body)
        self.assertEqual(status, 201)
        self.assertEqual(saved["exercise_id"], body["exercise_id"])

    def test_catalogue_move_history_can_be_filtered_by_utc8_date(self) -> None:
        body = {
            "exercise_id": "move-history-filter", "stable_code": "CS2026DATE001",
            "source_catalogue_id": "old-leaf", "target_catalogue_id": "new-leaf",
            "original_path": ["专题1：实数", "【大题】", "旧分类"],
            "target_path": ["专题4：分式方程与不等式", "【大题】", "解不等式"],
        }
        self.assertEqual(self.request("POST", "/api/v1/history/catalogue-moves", body)[0], 201)
        self.state.cache._connection.execute(
            "UPDATE catalogue_move_history SET moved_at = ? WHERE exercise_id = ?",
            ("2026-09-11 16:00:00", body["exercise_id"]),
        )
        self.state.cache._connection.commit()
        status, report = self.request("GET", "/api/v1/history/catalogue-moves?date=2026-09-12")
        self.assertEqual(status, 200)
        self.assertEqual(report["summary"]["period"]["label"], "2026-09-12 工作成果")
        self.assertEqual(report["records"][0]["stable_code"], "CS2026DATE001")
        range_status, ranged = self.request(
            "GET", "/api/v1/history/catalogue-moves?start_date=2026-09-11&end_date=2026-09-12"
        )
        self.assertEqual(range_status, 200)
        self.assertEqual(ranged["summary"]["period"]["label"], "2026-09-11 至 2026-09-12 工作成果")
        self.assertEqual(ranged["summary"]["period"]["start_date"], "2026-09-11")
        self.assertEqual(ranged["summary"]["period"]["end_date"], "2026-09-12")
        invalid_status, invalid = self.request("GET", "/api/v1/history/catalogue-moves?date=2026/09/12")
        self.assertEqual(invalid_status, 400)
        self.assertIn("YYYY-MM-DD", invalid["message"])
        invalid_range_status, invalid_range = self.request(
            "GET", "/api/v1/history/catalogue-moves?start_date=2026-09-12&end_date=2026-09-11"
        )
        self.assertEqual(invalid_range_status, 400)
        self.assertIn("截止日期", invalid_range["message"])

    def test_cloud_settings_save_key_without_returning_it(self) -> None:
        status, payload = self.request("POST", "/api/v1/settings/cloud", {
            "protocol": "chat_completions",
            "model": "test-cloud-model", "routing_model": "test-routing-model",
            "reasoning_effort": "high", "routing_reasoning_effort": "medium",
            "max_concurrent_requests": 5,
            "base_url": "https://api.example.test/v1", "api_key": "test-secret-key-123",
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], "test-cloud-model")
        self.assertEqual(payload["protocol"], "chat_completions")
        self.assertEqual(payload["routing_model"], "test-routing-model")
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertEqual(payload["routing_reasoning_effort"], "medium")
        self.assertEqual(payload["max_concurrent_requests"], 5)
        self.assertTrue(payload["api_key_configured"])
        self.assertNotIn("api_key", payload)
        persisted = yaml.safe_load((self.temp_path / "settings.yaml").read_text(encoding="utf-8"))
        active_id = persisted["classifier"]["cloud_profiles"]["active_id"]
        active = next(item for item in persisted["classifier"]["cloud_profiles"]["profiles"] if item["id"] == active_id)
        self.assertEqual(active["api_key"], "test-secret-key-123")
        self.assertEqual(active["protocol"], "chat_completions")
        self.assertEqual(active["routing_model"], "test-routing-model")
        self.assertEqual(active["routing_reasoning_effort"], "medium")
        self.assertEqual(persisted["classifier"]["pipeline"]["max_concurrent_requests"], 5)

    def test_cloud_profile_keeps_generic_connection_compatibility_private(self) -> None:
        status, payload = self.request("POST", "/api/v1/settings/cloud", {
            "action": "save_profile", "name": "兼容网关", "model": "test-model",
            "protocol": "chat_completions", "base_url": "https://api.example.test",
            "request_compatibility": "go_http",
            "extra_headers": {"X-Workspace": "math-curation"},
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["request_compatibility"], "go_http")
        self.assertEqual(payload["custom_header_names"], ["X-Workspace"])
        self.assertNotIn("extra_headers", payload)
        persisted = yaml.safe_load((self.temp_path / "settings.yaml").read_text(encoding="utf-8"))
        active = persisted["classifier"]["cloud_profiles"]["profiles"][0]
        self.assertEqual(active["request_compatibility"], "go_http")
        self.assertEqual(active["extra_headers"], {"X-Workspace": "math-curation"})

        status, rejected = self.request("POST", "/api/v1/settings/cloud", {
            "action": "save_profile", "profile_id": active["id"], "name": "兼容网关", "model": "test-model",
            "extra_headers": {"Authorization": "must-not-overwrite"},
        })
        self.assertEqual(status, 400)
        self.assertIn("不能覆盖", rejected["message"])

    def test_cloud_connection_test_uses_draft_without_persisting_it(self) -> None:
        with patch("server.main.OpenAIChatCompletionsProvider") as provider_class:
            provider_class.return_value.test_connection.return_value = {
                "protocol": "anthropic_messages", "model": "claude-test", "message": "连接成功",
            }
            status, payload = self.request("POST", "/api/v1/settings/cloud/test", {
                "protocol": "anthropic_messages", "model": "claude-test",
                "base_url": "https://api.anthropic.com/v1", "api_key": "test-secret-key-123",
            })
        self.assertEqual(status, 200)
        self.assertEqual(payload["protocol"], "anthropic_messages")
        settings = provider_class.call_args.args[0]
        self.assertEqual(settings["api_key"], "test-secret-key-123")
        self.assertEqual(settings["protocol"], "anthropic_messages")
        self.assertFalse((self.temp_path / "settings.yaml").read_text(encoding="utf-8").find("claude-test") >= 0)

    def test_cloud_connection_rejects_chatgpt_internal_backend_endpoint(self) -> None:
        status, payload = self.request("POST", "/api/v1/settings/cloud/test", {
            "protocol": "responses", "model": "gpt-test",
            "base_url": "https://chatgpt.com/backend-api/codex", "api_key": "test-secret-key-123",
        })
        self.assertEqual(status, 400)
        self.assertIn("登录态内部接口", payload["message"])

    def test_cloud_connection_test_can_be_started_and_polled(self) -> None:
        with patch("server.main.OpenAIChatCompletionsProvider") as provider_class:
            provider_class.return_value.test_connection.return_value = {
                "protocol": "responses", "model": "gpt-test", "message": "基础连通成功",
            }
            status, started = self.request("POST", "/api/v1/settings/cloud/test/start", {
                "protocol": "responses", "model": "gpt-test",
                "base_url": "https://api.example.test/v1", "api_key": "test-secret-key-123",
            })
            self.assertEqual(status, 202)
            for _ in range(20):
                status, snapshot = self.request("GET", f"/api/v1/settings/cloud/test/{started['test_id']}")
                if snapshot["status"] != "running":
                    break
                time.sleep(0.01)
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["status"], "succeeded")
        self.assertEqual(snapshot["result"]["model"], "gpt-test")

    def test_cloud_profiles_switch_independently_and_share_processing_speed(self) -> None:
        _, first = self.request("POST", "/api/v1/settings/cloud", {
            "action": "save_profile", "name": "GPT", "model": "gpt-model",
            "routing_model": "gpt-route", "reasoning_effort": "high", "routing_reasoning_effort": "medium",
            "base_url": "https://gpt.example.test/v1", "api_key": "gpt-secret-key-123",
            "pipeline": {"max_concurrent_requests": 2},
        })
        self.assertEqual(first["active_profile_name"], "GPT")
        gpt_id = first["active_profile_id"]
        status, created = self.request("POST", "/api/v1/settings/cloud", {"action": "create_profile", "name": "DeepSeek"})
        self.assertEqual(status, 200)
        deepseek_id = created["active_profile_id"]
        self.assertNotEqual(gpt_id, deepseek_id)
        status, saved = self.request("POST", "/api/v1/settings/cloud", {
            "action": "save_profile", "profile_id": deepseek_id, "name": "DeepSeek", "model": "deepseek-model",
            "routing_model": "", "reasoning_effort": "medium", "routing_reasoning_effort": "low",
            "base_url": "https://deepseek.example.test/v1", "api_key": "deepseek-secret-key-123",
            "pipeline": {"max_concurrent_requests": 4},
        })
        self.assertEqual(saved["model"], "deepseek-model")
        self.assertEqual(saved["max_concurrent_requests"], 4)
        status, switched = self.request("POST", "/api/v1/settings/cloud", {"action": "select_profile", "profile_id": gpt_id})
        self.assertEqual(status, 200)
        self.assertEqual(switched["active_profile_name"], "GPT")
        self.assertEqual(switched["model"], "gpt-model")
        self.assertEqual(switched["max_concurrent_requests"], 4)
        self.assertNotIn("api_key", switched)

    def test_cloud_profiles_can_be_reordered_without_changing_active_profile(self) -> None:
        _, first = self.request("POST", "/api/v1/settings/cloud", {
            "action": "save_profile", "name": "第一", "model": "first-model",
        })
        first_id = first["active_profile_id"]
        _, second = self.request("POST", "/api/v1/settings/cloud", {
            "action": "create_profile", "name": "第二",
        })
        second_id = second["active_profile_id"]
        _, third = self.request("POST", "/api/v1/settings/cloud", {
            "action": "create_profile", "name": "第三",
        })
        third_id = third["active_profile_id"]
        status, reordered = self.request("POST", "/api/v1/settings/cloud", {
            "action": "reorder_profiles", "profile_ids": [third_id, first_id, second_id],
        })
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in reordered["profiles"]], [third_id, first_id, second_id])
        self.assertEqual(reordered["active_profile_id"], third_id)
        persisted = yaml.safe_load((self.temp_path / "settings.yaml").read_text(encoding="utf-8"))
        self.assertEqual(
            [item["id"] for item in persisted["classifier"]["cloud_profiles"]["profiles"]],
            [third_id, first_id, second_id],
        )

    def test_cloud_profile_name_can_be_saved_without_resaving_other_settings(self) -> None:
        _, saved = self.request("POST", "/api/v1/settings/cloud", {
            "action": "save_profile", "name": "旧名称", "model": "test-model",
            "protocol": "chat_completions", "routing_model": "test-route",
            "reasoning_effort": "high", "routing_reasoning_effort": "medium",
            "base_url": "https://api.example.test", "api_key": "test-secret-key-123",
            "pipeline": {"max_concurrent_requests": 4},
        })
        status, renamed = self.request("POST", "/api/v1/settings/cloud", {
            "action": "rename_profile", "profile_id": saved["active_profile_id"], "name": "新名称",
        })
        self.assertEqual(status, 200)
        self.assertEqual(renamed["active_profile_name"], "新名称")
        self.assertEqual(renamed["model"], "test-model")
        self.assertEqual(renamed["protocol"], "chat_completions")
        self.assertEqual(renamed["routing_model"], "test-route")
        self.assertEqual(renamed["max_concurrent_requests"], 4)
        persisted = yaml.safe_load((self.temp_path / "settings.yaml").read_text(encoding="utf-8"))
        active = persisted["classifier"]["cloud_profiles"]["profiles"][0]
        self.assertEqual(active["name"], "新名称")
        self.assertEqual(active["api_key"], "test-secret-key-123")

    def test_interactive_job_returns_accepted_and_progress_can_be_polled(self) -> None:
        body = {
            "questions": [{
                "exercise_id": "job-2529221",
                "question_press": "计算并化简含有分母有理化的根式",
                "scope": {"topic_id": "topic-01-real-numbers", "level2_id": "topic-01-large"},
            }],
        }
        status, submitted = self.request("POST", "/api/v1/classification-jobs", body)
        self.assertEqual(status, 202)
        for _ in range(20):
            status, snapshot = self.request("GET", f"/api/v1/classification-jobs/{submitted['job_id']}")
            if snapshot["status"] == "completed":
                break
            time.sleep(0.01)
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["completed"], 1)
        self.assertEqual(snapshot["results"][0]["exercise_id"], "job-2529221")

    def test_interactive_job_accepts_focus_sized_payload_and_keeps_a_protection_limit(self) -> None:
        questions = [{"exercise_id": f"focus-{index}", "question_press": "分母有理化"} for index in range(101)]
        submitted = self.state.create_classification_job(questions)
        self.assertEqual(submitted["total"], 101)
        for _ in range(100):
            status = self.state.classification_jobs.snapshot(submitted["job_id"])["status"]
            if status == "completed":
                break
            time.sleep(0.01)
        self.assertEqual(status, "completed")
        with self.assertRaisesRegex(ValueError, "最多处理 1000 道题"):
            self.state.create_classification_job([
                {"exercise_id": f"oversized-{index}"}
                for index in range(MAX_INTERACTIVE_CLASSIFICATION_QUESTIONS + 1)
            ])

    def test_concurrent_jobs_share_one_service_level_llm_limit(self) -> None:
        target = self.state.taxonomy.all_targets()[0]
        active = 0
        peak = 0
        lock = threading.Lock()

        class StubCloud:
            configured = True
            provider_name = "stub"
            model = "fast-model"
            reasoning_effort = "medium"

            @staticmethod
            def _result(question):
                return {
                    "exercise_id": str(question["exercise_id"]), "status": "suggested",
                    "target_level3_id": target.level3_id, "target_level4_id": target.level4_id,
                    "confidence": 0.96, "reason": "唯一命中", "review_reasons": [],
                    "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None},
                }

            def _call(self, callback):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    time.sleep(0.02)
                    return callback()
                finally:
                    with lock:
                        active -= 1

            def route_fast_batch(self, questions, _taxonomy, _rules):
                return self._call(lambda: [{
                    "exercise_id": str(question["exercise_id"]), "status": "routed",
                    "required_knowledge_points": ["实数运算"], "latest_topic_id": target.topic_id,
                    "confidence": 0.96, "reason": "路由完成", "review_reasons": [],
                } for question in questions])

            def classify_topic_batch(self, questions, _topic_id, _taxonomy, _rules, routings):
                return self._call(lambda: [
                    {**self._result(question), "routing": routings[str(question["exercise_id"])]}
                    for question in questions
                ])

        self.state.settings["classifier"] = {"cloud": {"max_concurrent_requests": 1}}
        self.state.cloud = StubCloud()
        first = self.state.create_classification_job([
            {"exercise_id": f"first-{index}", "question_press": "分母有理化"} for index in range(10)
        ])
        second = self.state.create_classification_job([
            {"exercise_id": f"second-{index}", "question_press": "分母有理化"} for index in range(10)
        ])
        for _ in range(100):
            first_status = self.state.classification_jobs.snapshot(first["job_id"])["status"]
            second_status = self.state.classification_jobs.snapshot(second["job_id"])["status"]
            if first_status == second_status == "completed":
                break
            time.sleep(0.01)
        self.assertEqual(first_status, "completed")
        self.assertEqual(second_status, "completed")
        self.assertEqual(peak, 1)

    def test_interactive_cloud_job_routes_then_classifies_with_one_topic_catalog(self) -> None:
        target = self.state.taxonomy.all_targets()[0]

        class StubCloud:
            configured = True
            provider_name = "stub"
            model = "fast-model"
            reasoning_effort = "medium"

            def __init__(self) -> None:
                self.calls: list[str] = []

            def route_fast_batch(self, questions, taxonomy, rules):
                self.calls.append("routing")
                return [{
                    "exercise_id": str(question["exercise_id"]), "status": "routed",
                    "required_knowledge_points": ["实数运算"], "latest_topic_id": target.topic_id,
                    "confidence": 0.96, "reason": "路由完成", "review_reasons": [],
                } for question in questions]

            def classify_topic_batch(self, questions, topic_id, taxonomy, rules, routings):
                self.calls.append(f"topic:{topic_id}")
                self.assertEqual(topic_id, target.topic_id)
                return [{
                    "exercise_id": str(question["exercise_id"]), "status": "suggested",
                    "target_level3_id": target.level3_id, "target_level4_id": target.level4_id,
                    "confidence": 0.96, "reason": "唯一命中", "review_reasons": [],
                    "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None},
                    "routing": routings[str(question["exercise_id"])],
                } for question in questions]

        cloud = StubCloud()
        # 供内部桩断言使用，避免闭包中依赖 unittest 的隐式绑定。
        cloud.assertEqual = self.assertEqual
        self.state.cloud = cloud
        self.state.settings["classifier"] = {"cloud": {}}
        body = {"questions": [{"exercise_id": "cloud-job-1", "question_press": "分母有理化"}]}
        status, submitted = self.request("POST", "/api/v1/classification-jobs", body)
        self.assertEqual(status, 202)
        for _ in range(20):
            _, snapshot = self.request("GET", f"/api/v1/classification-jobs/{submitted['job_id']}")
            if snapshot["status"] == "completed":
                break
            time.sleep(0.01)
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["results"][0]["target"]["level3_id"], target.level3_id)
        self.assertEqual(cloud.calls, ["routing", f"topic:{target.topic_id}"])

    def test_interactive_cloud_job_batches_final_directory_paths_for_throughput(self) -> None:
        target = self.state.taxonomy.all_targets()[0]

        class StubCloud:
            configured = True
            provider_name = "stub"
            model = "fast-model"
            reasoning_effort = "medium"

            def __init__(self) -> None:
                self.final_call_ids: list[list[str]] = []

            def route_fast_batch(self, questions, _taxonomy, _rules):
                return [{
                    "exercise_id": str(question["exercise_id"]), "status": "routed",
                    "required_knowledge_points": ["实数运算"], "latest_topic_id": target.topic_id,
                    "confidence": 0.96, "reason": "路由完成", "review_reasons": [],
                } for question in questions]

            def classify_topic_batch(self, questions, _topic_id, _taxonomy, _rules, routings):
                if len(questions) != 3:
                    raise AssertionError("同一专题的最终目录判定应复用微批")
                exercise_ids = [str(question["exercise_id"]) for question in questions]
                self.final_call_ids.append(exercise_ids)
                return [{
                    "exercise_id": exercise_id, "status": "suggested",
                    "target_level3_id": target.level3_id, "target_level4_id": target.level4_id,
                    "confidence": 0.96, "reason": "唯一命中", "review_reasons": [],
                    "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None},
                    "routing": routings[exercise_id],
                } for exercise_id in exercise_ids]

        cloud = StubCloud()
        self.state.settings["classifier"] = {"cloud": {"max_concurrent_requests": 1}}
        self.state.cloud = cloud
        questions = [{"exercise_id": f"independent-{index}", "question_press": "分母有理化"} for index in range(3)]
        _, submitted = self.request("POST", "/api/v1/classification-jobs", {"questions": questions})
        for _ in range(80):
            _, snapshot = self.request("GET", f"/api/v1/classification-jobs/{submitted['job_id']}")
            if snapshot["status"] == "completed":
                break
            time.sleep(0.01)
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(cloud.final_call_ids, [["independent-0", "independent-1", "independent-2"]])

    def test_interactive_cloud_job_recovers_from_unexpected_cloud_attribute_error(self) -> None:
        """上游兼容网关的格式缺陷只能影响当前分批，不能击穿整个作业。"""

        class BrokenCloud:
            configured = True
            provider_name = "stub"
            model = "fast-model"
            reasoning_effort = "medium"

            def route_fast_batch(self, _questions, _taxonomy, _rules):
                raise AttributeError("gateway response has no output_text")

            def classify_topic_batch(self, *_args):
                raise AssertionError("路由失败的题目不应进入专题内分类")

        self.state.cloud = BrokenCloud()
        self.state.settings["classifier"] = {"cloud": {}}
        _, submitted = self.request("POST", "/api/v1/classification-jobs", {
            "questions": [{"exercise_id": "broken-cloud-1", "question_press": "分母有理化"}],
        })
        for _ in range(20):
            _, snapshot = self.request("GET", f"/api/v1/classification-jobs/{submitted['job_id']}")
            if snapshot["status"] == "completed":
                break
            time.sleep(0.01)
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["failed_exercise_ids"], ["broken-cloud-1"])
        self.assertEqual(snapshot["results"][0]["status"], "review")
        self.assertEqual(
            snapshot["results"][0]["reason"],
            "云端模型本次未返回可用的分类结果，请稍后重试；若持续出现，请切换兼容的模型方案。",
        )

    def test_second_stage_reroutes_to_corrected_topic_large_question_directory(self) -> None:
        self.state.taxonomy = Taxonomy({
            "taxonomy_version": "reroute-test-v1",
            "topics": [
                {"id": "topic-algebra", "title": "专题2：代数式", "order": 2, "level2": [
                    {"id": "algebra-large", "title": "【大题】", "level3": [
                        {"id": "algebra-fraction", "title": "分式化简", "level4": []},
                    ]},
                ]},
                {"id": "topic-trigonometry", "title": "专题12：锐角三角函数", "order": 12, "level2": [
                    {"id": "trigonometry-large", "title": "【大题】", "level3": [
                        {"id": "trigonometry-solve", "title": "锐角三角函数求值", "level4": [
                            {"id": "trigonometry-special-angle", "title": "考法1：特殊角三角函数值"},
                        ]},
                    ]},
                ]},
            ],
        })
        target = self.state.taxonomy.global_target("trigonometry-solve", "trigonometry-special-angle")
        self.assertIsNotNone(target)

        class StubCloud:
            configured = True
            provider_name = "stub"
            model = "fast-model"
            reasoning_effort = "medium"

            def __init__(self) -> None:
                self.calls: list[str] = []

            def route_fast_batch(self, questions, _taxonomy, _rules):
                self.calls.append("routing:topic-algebra")
                return [{
                    "exercise_id": str(question["exercise_id"]), "status": "routed",
                    "required_knowledge_points": ["分式"], "latest_topic_id": "topic-algebra",
                    "confidence": 0.8, "reason": "初始路由", "review_reasons": [],
                } for question in questions]

            def classify_topic_batch(self, questions, topic_id, _taxonomy, _rules, routings):
                self.calls.append(f"classify:{topic_id}")
                if topic_id == "topic-algebra":
                    return [{
                        "exercise_id": str(question["exercise_id"]), "status": "review",
                        "target_level3_id": None, "target_level4_id": None,
                        "confidence": 0.0, "reason": "遗漏锐角三角函数",
                        "review_reasons": ["topic_routing_incomplete"],
                        "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None},
                        "routing": routings[str(question["exercise_id"])],
                        "self_check": {"passed": False, "violations": ["遗漏更晚专题"], "reason": "需改路由", "confidence": 0.98, "reroute_topic_id": "topic-trigonometry"},
                    } for question in questions]
                self.assertEqual(topic_id, "topic-trigonometry")
                if not all(routings[str(question["exercise_id"])]["rerouted_from_topic_id"] == "topic-algebra" for question in questions):
                    raise AssertionError("重路由后的专题轨迹缺失")
                return [{
                    "exercise_id": str(question["exercise_id"]), "status": "suggested",
                    "target_level3_id": target.level3_id, "target_level4_id": target.level4_id,
                    "confidence": 0.96, "reason": "特殊角三角函数值",
                    "review_reasons": [],
                    "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None},
                    "routing": routings[str(question["exercise_id"])],
                    "self_check": {"passed": True, "violations": [], "reason": "路由与目录一致", "confidence": 0.96, "reroute_topic_id": None},
                } for question in questions]

        cloud = StubCloud()
        cloud.assertEqual = self.assertEqual
        self.state.cloud = cloud
        self.state.settings["classifier"] = {"cloud": {"max_concurrent_requests": 1}}
        _, submitted = self.request("POST", "/api/v1/classification-jobs", {"questions": [{
            "exercise_id": "reroute-1", "question_press": "计算 tan60° 的值",
            "scope": {"topic_id": "topic-algebra", "level2_id": "algebra-large"},
        }]})
        for _ in range(50):
            _, snapshot = self.request("GET", f"/api/v1/classification-jobs/{submitted['job_id']}")
            if snapshot["status"] == "completed":
                break
            time.sleep(0.01)
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(cloud.calls, ["routing:topic-algebra", "classify:topic-algebra", "classify:topic-trigonometry"])
        self.assertEqual(snapshot["results"][0]["target"]["topic_id"], "topic-trigonometry")
        self.assertEqual(snapshot["results"][0]["target"]["level2_title"], "【大题】")
        self.assertEqual(snapshot["results"][0]["routing"]["rerouted_from_topic_id"], "topic-algebra")

    def test_interactive_cloud_job_starts_topic_classification_before_all_routing_batches_finish(self) -> None:
        target = self.state.taxonomy.all_targets()[0]
        events: list[str] = []

        class StubCloud:
            configured = True
            provider_name = "stub"
            model = "fast-model"
            reasoning_effort = "medium"

            def route_fast_batch(self, questions, taxonomy, rules):
                if any(question["exercise_id"] == "pipe-0" for question in questions):
                    time.sleep(0.08)
                    events.append("routing-long-finished")
                else:
                    events.append("routing-short-finished")
                return [{
                    "exercise_id": str(question["exercise_id"]), "status": "routed",
                    "required_knowledge_points": ["实数运算"], "latest_topic_id": target.topic_id,
                    "confidence": 0.96, "reason": "路由完成", "review_reasons": [],
                } for question in questions]

            def classify_topic_batch(self, questions, topic_id, taxonomy, rules, routings):
                events.append("classification-started")
                return [{
                    "exercise_id": str(question["exercise_id"]), "status": "suggested",
                    "target_level3_id": target.level3_id, "target_level4_id": target.level4_id,
                    "confidence": 0.96, "reason": "唯一命中", "review_reasons": [],
                    "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None},
                    "routing": routings[str(question["exercise_id"])],
                } for question in questions]

        self.state.settings.setdefault("classifier", {}).setdefault("cloud", {})["max_concurrent_requests"] = 2
        self.state.cloud = StubCloud()
        questions = [{"exercise_id": f"pipe-{index}", "question_press": "分母有理化"} for index in range(11)]
        _, submitted = self.request("POST", "/api/v1/classification-jobs", {"questions": questions})
        for _ in range(100):
            _, snapshot = self.request("GET", f"/api/v1/classification-jobs/{submitted['job_id']}")
            if snapshot["status"] == "completed":
                break
            time.sleep(0.01)
        self.assertEqual(snapshot["status"], "completed")
        self.assertLess(events.index("classification-started"), events.index("routing-long-finished"))


if __name__ == "__main__":
    unittest.main()
