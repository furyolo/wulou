from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.cache import ResultCache  # noqa: E402
from server.main import CurationServer, ServiceState  # noqa: E402
from server.skill_classifications import (  # noqa: E402
    SkillImportError,
    build_import_plan,
    parse_import_text,
)
from server.taxonomy import Taxonomy  # noqa: E402


TAXONOMY_RAW = {
    "taxonomy_version": "excel-v4-test00000000",
    "topics": [{
        "id": "topic-1",
        "title": "专题1：实数",
        "order": 1,
        "level2": [{
            "id": "l2-1",
            "title": "【大题】",
            "level3": [
                {"id": "l3-leaf", "title": "实数的应用", "knowledge_point_id": "ZCSQG20260101SF01", "level4": []},
                {
                    "id": "l3-parent",
                    "title": "实数综合计算",
                    "knowledge_point_id": None,
                    "level4": [
                        {"id": "l4-a", "title": "考法1：不含分母有理化", "knowledge_point_id": "ZCSQG20260101SF02"},
                        {"id": "l4-b", "title": "考法2：含分母有理化", "knowledge_point_id": "ZCSQG20260101SF03"},
                    ],
                },
                {
                    # 数据里不该出现，但一旦出现必须拦住：父三级有编号时不得再细分四级。
                    "id": "l3-numbered",
                    "title": "已编号的三级",
                    "knowledge_point_id": "ZCSQG20260101SF09",
                    "level4": [{"id": "l4-c", "title": "考法1：不该存在", "knowledge_point_id": "ZCSQG20260101SF10"}],
                },
            ],
        }],
    }],
}


class SkillImportParsingTests(unittest.TestCase):
    def test_accepts_json_object_json_array_and_jsonl(self) -> None:
        rows = [{"exercise_id": "1", "knowledge_point_id": "ZCSQG20260101SF02"}]
        object_items, version = parse_import_text(json.dumps(
            {"taxonomy_version": "v9", "items": rows}, ensure_ascii=False
        ))
        self.assertEqual(object_items, rows)
        self.assertEqual(version, "v9")
        array_items, _ = parse_import_text(json.dumps(rows, ensure_ascii=False))
        self.assertEqual(array_items, rows)
        jsonl_items, _ = parse_import_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
        )
        self.assertEqual(jsonl_items, rows)

    def test_rejects_empty_or_unparsable_text(self) -> None:
        with self.assertRaises(SkillImportError):
            parse_import_text("   ")
        with self.assertRaises(SkillImportError):
            parse_import_text("[{'exercise_id': '1'}]")


class SkillImportPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.taxonomy = Taxonomy(TAXONOMY_RAW)

    def test_resolves_level4_and_level3_leaf_codes_into_title_paths(self) -> None:
        plan = build_import_plan({"items": [
            {"exercise_id": "11", "knowledge_point_id": "ZCSQG20260101SF03"},
            {"exercise_id": "12", "knowledge_point_id": "ZCSQG20260101SF01"},
        ]}, self.taxonomy)
        self.assertEqual(plan["resolved"], 2)
        self.assertEqual(plan["failed"], [])
        self.assertEqual(plan["rows"][0]["target_path"], ["专题1：实数", "【大题】", "实数综合计算", "考法2：含分母有理化"])
        self.assertEqual(plan["rows"][1]["target_path"], ["专题1：实数", "【大题】", "实数的应用"])
        self.assertTrue(all(row["taxonomy_version"] == "excel-v4-test00000000" for row in plan["rows"]))
        self.assertEqual(plan["rows"][0]["source_catalogue_id"], "__uncategorized__")

    def test_unknown_code_fails_only_its_own_item(self) -> None:
        plan = build_import_plan({"items": [
            {"exercise_id": "11", "knowledge_point_id": "ZCSQG20260101SF03"},
            {"exercise_id": "12", "knowledge_point_id": "不存在的编号"},
            {"knowledge_point_id": "ZCSQG20260101SF02"},
        ]}, self.taxonomy)
        self.assertEqual(plan["resolved"], 1)
        self.assertEqual(
            [item["code"] for item in plan["failed"]],
            ["unresolved_knowledge_point_id", "invalid_exercise_id"],
        )

    def test_rejects_duplicate_exercise_ids_inside_one_file(self) -> None:
        plan = build_import_plan({"items": [
            {"exercise_id": "11", "knowledge_point_id": "ZCSQG20260101SF03"},
            {"exercise_id": "11", "knowledge_point_id": "ZCSQG20260101SF02"},
        ]}, self.taxonomy)
        self.assertEqual(plan["resolved"], 1)
        self.assertEqual(plan["failed"][0]["code"], "duplicate_exercise_id")

    def test_rejects_level4_target_under_numbered_level3(self) -> None:
        """编号兼容性：父三级已有编号时，只能粗分到三级，不能落四级。"""
        plan = build_import_plan({"items": [
            {"exercise_id": "11", "knowledge_point_id": "ZCSQG20260101SF09"},
        ]}, self.taxonomy)
        self.assertEqual(plan["resolved"], 0)
        self.assertIn("不能再细分四级目录", plan["failed"][0]["message"])

    def test_warns_when_file_targets_another_taxonomy_version(self) -> None:
        plan = build_import_plan(
            {"taxonomy_version": "excel-v4-other", "items": [
                {"exercise_id": "11", "knowledge_point_id": "ZCSQG20260101SF03"},
            ]},
            self.taxonomy,
        )
        self.assertEqual(plan["resolved"], 1)
        self.assertTrue(any("excel-v4-other" in note for note in plan["warnings"]))


class SkillCacheWriteTests(unittest.TestCase):
    """前端读的是人工修正表，所以这里直接验证它读到的东西。"""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.cache = ResultCache(Path(self.tempdir.name) / "cache.sqlite3")

    def tearDown(self) -> None:
        self.cache.close()
        self.tempdir.cleanup()

    def _rows(self, **overrides: object) -> list[dict]:
        row = {
            "exercise_id": "2374107",
            "source_catalogue_id": "__uncategorized__",
            "stable_code": "CS2025JLZKYHM326JKHERAIKKC012",
            "taxonomy_version": "excel-v4-test00000000",
            "original_target_path": [],
            "target_path": ["专题1：实数", "【大题】", "实数综合计算", "考法2：含分母有理化"],
        }
        row.update(overrides)
        return [row]

    def test_skill_row_is_readable_from_any_source_catalogue(self) -> None:
        self.cache.put_skill_classifications(self._rows())
        # 前端按题目当前目录查询；导入时并不知道该目录，靠同题候选兜底命中。
        overrides = self.cache.get_manual_overrides("2374107", "760474748")
        self.assertTrue(overrides)
        self.assertEqual(overrides[0]["source"], "skill")
        self.assertEqual(overrides[0]["target_path"][-1], "考法2：含分母有理化")

    def test_skill_row_is_still_readable_after_a_taxonomy_version_bump(self) -> None:
        """目录版本号变化不再直接作废记录，是否仍然成立由上层按目标路径解析判定。"""
        self.cache.put_skill_classifications(self._rows())
        overrides = self.cache.get_manual_overrides("2374107", "760474748")
        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0]["taxonomy_version"], "excel-v4-test00000000")

    def test_report_separates_inserts_from_overwrites(self) -> None:
        first = self.cache.put_skill_classifications(self._rows())
        second = self.cache.put_skill_classifications(self._rows(target_path=["专题1：实数", "【大题】", "实数的应用"]))
        self.assertEqual(first, {"inserted": 1, "updated": 0, "skipped_manual_decisions": 0})
        self.assertEqual(second, {"inserted": 0, "updated": 1, "skipped_manual_decisions": 0})

    def test_preserve_manual_decisions_keeps_human_choice(self) -> None:
        self.cache.put_manual_override(
            exercise_id="2374107", source_catalogue_id="__uncategorized__", stable_code="",
            taxonomy_version="excel-v4-test00000000", original_target_path=[],
            target_path=["专题1：实数", "【大题】", "实数的应用"],
        )
        report = self.cache.put_skill_classifications(
            self._rows(), preserve_manual_decisions=True
        )
        self.assertEqual(report["skipped_manual_decisions"], 1)
        overrides = self.cache.get_manual_overrides("2374107", "760474748")
        self.assertTrue(overrides)
        self.assertEqual(overrides[0]["source"], "manual")
        self.assertEqual(overrides[0]["target_path"][-1], "实数的应用")


class ManualOverrideResolutionTests(unittest.TestCase):
    """目录变更后旧结论是否仍然成立：按目标路径能否解析，而不是按整表版本号。

    对应三条口径：同专题目录变动要重做、跨专题的人工结论不被牵连、
    整份工作簿换了版本号本身不等于全部作废。
    """

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.tempdir.name)
        self.taxonomy_path = self.temp_path / "taxonomy.yaml"
        self._write_taxonomy(self._raw_v1())
        settings_path = self.temp_path / "settings.yaml"
        settings_path.write_text(yaml.safe_dump({
            "host": "127.0.0.1",
            "port": 0,
            "taxonomy_path": str(self.taxonomy_path),
            "rules_path": str(ROOT / "config" / "classification-rules.yaml"),
            "cache_path": "cache.sqlite3",
        }, allow_unicode=True), encoding="utf-8")
        self.state = ServiceState(settings_path)

    def tearDown(self) -> None:
        self.state.close()
        self.tempdir.cleanup()

    @staticmethod
    def _raw_v1() -> dict:
        return {
            "taxonomy_version": "excel-v4-alpha",
            "topics": [
                {"id": "topic-1", "title": "专题1：实数", "order": 1, "level2": [{
                    "id": "l2-1", "title": "【大题】", "level3": [
                        {"id": "l3-a", "title": "实数的应用",
                         "knowledge_point_id": "ZCSQG20260101SF01", "level4": []},
                        {"id": "l3-d", "title": "实数与数轴",
                         "knowledge_point_id": "ZCSQG20260101SF04", "level4": []},
                    ]}]},
                {"id": "topic-2", "title": "专题2：代数式", "order": 2, "level2": [{
                    "id": "l2-2", "title": "【大题】", "level3": [
                        {"id": "l3-b", "title": "整式化简",
                         "knowledge_point_id": "ZCSQG20260101SF02", "level4": []},
                    ]}]},
            ],
        }

    @staticmethod
    def _raw_v2() -> dict:
        """专题1 大改：原三级被细分出四级、另加一个同级目录；专题2 原封不动。"""
        return {
            "taxonomy_version": "excel-v4-beta",
            "topics": [
                {"id": "topic-1", "title": "专题1：实数", "order": 1, "level2": [{
                    "id": "l2-1", "title": "【大题】", "level3": [
                        {"id": "l3-a", "title": "实数的应用", "knowledge_point_id": None, "level4": [
                            {"id": "l4-a1", "title": "考法1：直接开方",
                             "knowledge_point_id": "ZCSQG20260201SF01"},
                        ]},
                        {"id": "l3-c", "title": "二次根式估算",
                         "knowledge_point_id": "ZCSQG20260201SF02", "level4": []},
                        {"id": "l3-d", "title": "实数与数轴",
                         "knowledge_point_id": "ZCSQG20260101SF04", "level4": []},
                    ]}]},
                {"id": "topic-2", "title": "专题2：代数式", "order": 2, "level2": [{
                    "id": "l2-2", "title": "【大题】", "level3": [
                        {"id": "l3-b", "title": "整式化简",
                         "knowledge_point_id": "ZCSQG20260101SF02", "level4": []},
                    ]}]},
            ],
        }

    def _write_taxonomy(self, raw: dict) -> None:
        self.taxonomy_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    def _switch_taxonomy(self, raw: dict) -> None:
        self._write_taxonomy(raw)
        self.state.taxonomy = Taxonomy.from_file(self.taxonomy_path)

    def _adopt(self, code: str, exercise_id: str = "2374107") -> None:
        report = self.state.import_skill_classifications({
            "items": [{"exercise_id": exercise_id, "knowledge_point_id": code}],
        })
        self.assertEqual(report["written"], 1, report)

    def _suggested_path(self, exercise_id: str = "2374107") -> list[str] | None:
        result = self.state.cached_result({
            "exercise_id": exercise_id,
            "current_catalogue_id": "760474748",
            "stable_code": "CS2025JLZKYHM326JKHERAIKKC012",
        })
        return ((result or {}).get("target") or {}).get("path")

    def test_decision_targeting_an_untouched_topic_survives_a_version_bump(self) -> None:
        """跨专题的人工结论不该被本专题的目录改动牵连。"""
        self._adopt("ZCSQG20260101SF02")
        self.assertEqual(self._suggested_path(), ["专题2：代数式", "【大题】", "整式化简"])

        self._switch_taxonomy(self._raw_v2())
        self.assertEqual(self._suggested_path(), ["专题2：代数式", "【大题】", "整式化简"])

    def test_decision_on_a_subdivided_level3_is_dropped(self) -> None:
        """原结论落在三级，而该三级后来被细分出四级：旧结论不再成立。"""
        self._adopt("ZCSQG20260101SF01")
        self.assertEqual(self._suggested_path(), ["专题1：实数", "【大题】", "实数的应用"])

        self._switch_taxonomy(self._raw_v2())
        self.assertIsNone(self._suggested_path())

    def test_decision_survives_when_sibling_directories_are_added(self) -> None:
        """同专题内只是旁边多了目录、原目标目录本身没动：不无谓丢弃人工成果。"""
        self._adopt("ZCSQG20260101SF04")
        self.assertEqual(self._suggested_path(), ["专题1：实数", "【大题】", "实数与数轴"])

        self._switch_taxonomy(self._raw_v2())
        self.assertEqual(self._suggested_path(), ["专题1：实数", "【大题】", "实数与数轴"])


class PublishedPathResolutionTests(unittest.TestCase):
    """标题路径反查本身的行为边界。"""

    def setUp(self) -> None:
        self.taxonomy = Taxonomy(TAXONOMY_RAW)

    def test_resolves_leaf_and_four_level_paths(self) -> None:
        self.assertIsNotNone(self.taxonomy.resolve_published_path(["专题1：实数", "【大题】", "实数的应用"]))
        resolved = self.taxonomy.resolve_published_path(
            ["专题1：实数", "【大题】", "实数综合计算", "考法2：含分母有理化"]
        )
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.level4_id, "l4-b")

    def test_rejects_unknown_depth_and_unknown_titles(self) -> None:
        self.assertIsNone(self.taxonomy.resolve_published_path(["专题1：实数"]))
        self.assertIsNone(self.taxonomy.resolve_published_path(["专题1：实数", "【大题】", "已改名的三级"]))
        self.assertIsNone(self.taxonomy.resolve_published_path([]))

    def test_returns_none_when_the_same_path_matches_more_than_once(self) -> None:
        """同名路径不唯一时不擅自挑一个，宁可要求重新确认。"""
        ambiguous = Taxonomy({
            "taxonomy_version": "excel-v4-ambiguous",
            "topics": [
                {"id": "t1", "title": "专题1：实数", "order": 1, "level2": [
                    {"id": "l2-a", "title": "【大题】", "level3": [
                        {"id": "l3-x", "title": "同名的三级", "level4": []},
                    ]},
                    {"id": "l2-b", "title": "【大题】", "level3": [
                        {"id": "l3-y", "title": "同名的三级", "level4": []},
                    ]},
                ]},
            ],
        })
        self.assertIsNone(ambiguous.resolve_published_path(["专题1：实数", "【大题】", "同名的三级"]))


class SkillImportHttpTests(unittest.TestCase):
    """端到端：导入 → 前端那条缓存查询接口能直接读到建议。"""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.tempdir.name)
        taxonomy_path = self.temp_path / "taxonomy.yaml"
        taxonomy_path.write_text(yaml.safe_dump(TAXONOMY_RAW, allow_unicode=True), encoding="utf-8")
        settings_path = self.temp_path / "settings.yaml"
        settings_path.write_text(yaml.safe_dump({
            "host": "127.0.0.1",
            "port": 0,
            "taxonomy_path": str(taxonomy_path),
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
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        connection.request(method, path, body=raw_body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    def test_dry_run_reports_without_writing(self) -> None:
        status, payload = self.request("POST", "/api/v1/skill-classifications", {
            "dry_run": True,
            "items": [{"exercise_id": "11", "knowledge_point_id": "ZCSQG20260101SF02"}],
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["resolved"], 1)
        self.assertEqual(payload["written"], 0)
        _, lookup = self.request("POST", "/api/v1/cache/classifications/lookup", {
            "questions": [{"exercise_id": "11", "current_catalogue_id": "760474748"}],
        })
        self.assertEqual(lookup["results"], [])

    def test_imported_classification_is_visible_through_the_frontend_lookup(self) -> None:
        status, payload = self.request("POST", "/api/v1/skill-classifications", {
            "items": [{"exercise_id": "11", "knowledge_point_id": "ZCSQG20260101SF02"}],
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["written"], 1)
        self.assertEqual(payload["inserted"], 1)

        _, lookup = self.request("POST", "/api/v1/cache/classifications/lookup", {
            "questions": [{"exercise_id": "11", "current_catalogue_id": "760474748"}],
        })
        self.assertEqual(lookup["missing_exercise_ids"], [])
        result = lookup["results"][0]
        self.assertEqual(result["status"], "suggested")
        self.assertEqual(result["target"]["path"], ["专题1：实数", "【大题】", "实数综合计算", "考法1：不含分母有理化"])
        self.assertEqual(result["manual_override"]["source"], "skill")
        self.assertTrue(result["cache_hit"])

    def test_reimport_overwrites_and_reports_the_update(self) -> None:
        """默认覆盖：批量结果权威，但报告必须如实区分新增与覆盖。"""
        self.request("POST", "/api/v1/skill-classifications", {
            "items": [{"exercise_id": "11", "knowledge_point_id": "ZCSQG20260101SF02"}],
        })
        _, payload = self.request("POST", "/api/v1/skill-classifications", {
            "items": [{"exercise_id": "11", "knowledge_point_id": "ZCSQG20260101SF03"}],
        })
        self.assertEqual((payload["inserted"], payload["updated"], payload["written"]), (0, 1, 1))
        _, lookup = self.request("POST", "/api/v1/cache/classifications/lookup", {
            "questions": [{"exercise_id": "11", "current_catalogue_id": "760474748"}],
        })
        self.assertEqual(lookup["results"][0]["target"]["path"][-1], "考法2：含分母有理化")

    def test_removed_directory_refactor_endpoint_is_gone(self) -> None:
        status, _ = self.request("POST", "/api/v1/directory-refactors", {"focus": {}, "questions": []})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
