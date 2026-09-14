"""OpenAI Responses 与 Batch API 适配器。

不依赖 SDK，便于接入兼容 ``/v1/responses`` 的云端网关。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from ..taxonomy import Target
    from ..model_input import build_model_input_snapshot
except ImportError:  # 支持直接运行 server/main.py。
    from taxonomy import Target
    from model_input import build_model_input_snapshot


class CloudProviderError(RuntimeError):
    """云端模型请求无法安全完成。"""


class OpenAIChatCompletionsProvider:
    """以 Responses 协议请求云端分类模型；保留旧类名以兼容本机导入。"""

    provider_name = "openai_responses"

    def __init__(self, settings: dict[str, Any]) -> None:
        self.model = str(settings.get("model", "")).strip()
        self.api_key_env = str(settings.get("api_key_env", "OPENAI_API_KEY")).strip()
        self.api_key = str(settings.get("api_key", "")).strip()
        self.base_url = str(settings.get("base_url", "https://api.openai.com/v1")).rstrip("/")
        self.reasoning_effort = str(settings.get("reasoning_effort", "high")).strip().lower()
        # 目录方案是粗粒度结构设计：先并发提取短题目特征，再一次性综合，不能沿用逐题精分的高推理配置。
        self.directory_reasoning_effort = str(settings.get("directory_reasoning_effort", "low")).strip().lower()
        self.directory_batch_size = int(settings.get("directory_batch_size", 60))
        self.directory_concurrency = int(settings.get("directory_concurrency", 5))
        self.directory_retry_attempts = int(settings.get("directory_retry_attempts", 3))
        # 目录方案的单个模型请求正常应在一分钟左右完成。比通用分类的 600 秒
        # 更短的超时可避免网关无响应时让整个方案长期停在“推理中”。
        self.directory_request_timeout_seconds = int(settings.get("directory_request_timeout_seconds", 90))
        # 这是网络失联保护，不是页面分类的业务时限。兼容旧配置的 180 秒也提升到 600 秒，
        # 防止上游已经完成但本机先断开并重复计费。
        self.timeout_seconds = max(600, int(settings.get("timeout_seconds", 600)))
        if not self.model:
            raise ValueError("cloud.model 不能为空")
        if self.reasoning_effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("cloud.reasoning_effort 必须是 none、low、medium、high、xhigh 或 max")
        if self.directory_reasoning_effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("cloud.directory_reasoning_effort 必须是 none、low、medium、high、xhigh 或 max")
        if not 20 <= self.directory_batch_size <= 100:
            raise ValueError("cloud.directory_batch_size 必须是 20 到 100")
        if not 1 <= self.directory_concurrency <= 5:
            raise ValueError("cloud.directory_concurrency 必须是 1 到 5")
        if not 1 <= self.directory_retry_attempts <= 5:
            raise ValueError("cloud.directory_retry_attempts 必须是 1 到 5")
        if not 30 <= self.directory_request_timeout_seconds <= 180:
            raise ValueError("cloud.directory_request_timeout_seconds 必须是 30 到 180")

    @property
    def configured(self) -> bool:
        return bool(self.api_key or os.environ.get(self.api_key_env))

    @staticmethod
    def _policy(rules: dict[str, Any]) -> dict[str, Any]:
        """只发送可执行分类规则，避免把本机 Skill 路径当作规则内容。"""
        return {
            "rule_version": str(rules.get("rule_version", "")),
            "rules": rules.get("rules") or {},
            "decision_protocol": rules.get("decision_protocol") or [],
        }

    @staticmethod
    def _notation_instruction() -> str:
        """统一数学表达式可判定性，避免把标准优先级误报为括号歧义。"""
        return (
            "按通行的中学数学运算优先级解释题干和答案：乘方优先于一元正负号，"
            "未写括号的正负号不自动并入幂底。可由标准优先级唯一确定的表达式不得仅因未额外加括号而返回 review；"
            "只有存在两个均符合题干、且不能由答案或上下文排除的解释时，才可判为表达式歧义。"
        )

    @staticmethod
    def _question(question: dict[str, Any]) -> dict[str, Any]:
        snapshot = build_model_input_snapshot(question)
        question_input = snapshot["question"]
        answer_input = snapshot["answer"]
        text = str(question_input["text"] or "")
        answer = str(answer_input["text"] or "")
        source = str(snapshot["site_context"] or "")
        return {
            "exercise_id": str(question["exercise_id"]),
            "text": text or "（题干文本缺失）",
            "supplemental_text": question_input["supplemental_text"],
            "question_latex": question_input["latex"],
            "answer": answer,
            "answer_supplemental_text": answer_input["supplemental_text"],
            "answer_latex": answer_input["latex"],
            "answer_omitted_reason": answer_input.get("omitted_reason"),
            "answer_part_numbering_mismatch": bool(answer_input.get("part_numbering_mismatch")),
            "input_warnings": snapshot["warnings"],
            "page_scope_hint": snapshot["page_scope_hint"],
            # 页面卡片文本通常重复题干，只保留少量网站标签作为弱证据。
            "site_context": source[:240],
            # Chat Completions 的普通文本消息不会读取 URL 指向的图片；有文字题干时不传无效 URL。
            "question_image_url": str(question.get("question_image_url", ""))[:512] if not text else "",
            "answer_image_url": str(question.get("answer_image_url", ""))[:512] if not answer else "",
        }

    @staticmethod
    def model_input_snapshot(question: dict[str, Any]) -> dict[str, Any]:
        """返回可落库的脱敏输入快照，供复核时核对模型实际读取的文本。"""
        return build_model_input_snapshot(question)

    @staticmethod
    def _candidate_rows(candidates: list[Target]) -> list[dict[str, Any]]:
        rows = []
        for target in candidates:
            item = asdict(target)
            item.pop("include_keywords", None)
            item.pop("exclude_keywords", None)
            rows.append(item)
        return rows

    @staticmethod
    def _candidate_id_schema(candidates: list[Target], attribute: str) -> dict[str, Any]:
        """将目录目标限制为当前候选集合，禁止模型生成不存在的 ID。"""
        valid_ids = sorted(
            {
                value
                for target in candidates
                if (value := getattr(target, attribute)) is not None
            }
        )
        return {"type": ["string", "null"], "enum": [*valid_ids, None]}

    @staticmethod
    def _string_list(value: Any, fallback: list[str] | None = None) -> list[str]:
        if value is None:
            return list(fallback or [])
        if not isinstance(value, list):
            raise CloudProviderError("云端模型返回的原因列表格式无效")
        return [str(item) for item in value if item]

    @staticmethod
    def _confidence(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CloudProviderError("云端模型返回的置信度格式无效")
        return max(0.0, min(1.0, float(value)))

    @staticmethod
    def _boolean(value: Any, field: str) -> bool:
        if not isinstance(value, bool):
            raise CloudProviderError(f"云端模型返回的 {field} 格式无效")
        return value

    def build_request(
        self,
        question: dict[str, Any],
        candidates: list[Target],
        rules: dict[str, Any],
        routing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        prompt = {
            "task": "将一道中考数学大题归入给定目录。只能选择候选目录，不能臆造已生效目录。",
            "policy": self._policy(rules),
            "question": self._question(question),
            "topic_routing": routing,
            "candidates": self._candidate_rows(candidates),
            "instructions": [
                "topic_routing 存在时，其 latest_topic_id 已按全局专题确定；本步只在该专题内选择下级目录。",
                "topic_routing 不存在时，必须先在内部按相同规则完成全局专题判断。",
                "信息不足、候选不唯一或需要图片时，status 必须为 review。",
                "site_context 中的已贴知识点分组可作辅助证据；题干和答案与其冲突时，以题干和答案为准。",
                "实际题干是分类主依据，答案用于补充验证。若答案编号与题干不一致，须判断答案的算式、变量、条件和结论是否与题干连续；只有明确属于独立题目时才忽略对应答案段落。",
                "target_level3_id 必须来自 candidates；没有四级目录时 target_level4_id 为 null。",
                "仅当命中的三级目录实际提供四级子目录时，才要求唯一匹配四级；三级没有四级子目录时，三级就是末级，不得因此返回 review。",
                "input_warnings 表示排版可能不完整；只有缺失部分影响分类时才 review，不得凭此臆造答案含有另一道题。",
                "不得只因编号结构不一致就丢弃答案或返回 review；题干公式在图片中缺失但答案可还原关键关系时，应使用该答案完成判断。",
                "proposal 只可提出候选，不会创建目录；单题不应形成新目录。",
                self._notation_instruction(),
            ],
        }
        schema = {
            "type": "object", "additionalProperties": False,
            "required": ["status", "target_level3_id", "target_level4_id", "confidence", "reason", "review_reasons", "proposal"],
            "properties": {
                "status": {"type": "string", "enum": ["suggested", "review"]},
                "target_level3_id": self._candidate_id_schema(candidates, "level3_id"),
                "target_level4_id": self._candidate_id_schema(candidates, "level4_id"),
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
                "review_reasons": {"type": "array", "items": {"type": "string"}},
                "proposal": {
                    "type": "object", "additionalProperties": False,
                    "required": ["kind", "title", "cluster_key", "reason"],
                    "properties": {
                        "kind": {"type": "string", "enum": ["none", "new_level3", "new_level4"]},
                        "title": {"type": ["string", "null"]},
                        "cluster_key": {"type": ["string", "null"]},
                        "reason": {"type": ["string", "null"]},
                    },
                },
            },
        }
        return self._responses_request("math_question_classification", [prompt], schema)

    def build_routing_request(self, question: dict[str, Any], taxonomy: Any, rules: dict[str, Any]) -> dict[str, Any]:
        """构造不受网页目录限制的全局专题路由请求。"""
        topics = taxonomy.topic_catalog()
        topic_ids = [item["id"] for item in topics]
        prompt = {
            "task": "列出完整解题所需知识点，并按专题阶段规则选择最终归属专题。",
            "policy": self._policy(rules),
            "question": self._question(question),
            "all_topics_in_order": topics,
            "instructions": [
                "先分析，再选专题；不得从 page_scope_hint 直接抄专题。专题10之前的基础或前置知识专题按最晚必备知识点路由；从专题10“三角形”起及后续专题的【大题】按最终解题核心突破口路由。",
                "all_topics_in_order 是完整候选范围。",
                "含 sin、cos、tan、cot 或特殊角三角函数值时，必须把三角函数列为必备知识点。",
                "题干、公式或答案不足时返回 review，不得猜测。",
                "实际题干是专题路由主依据；答案编号与题干不一致时，须检查算式、变量、条件和结论的数学连续性。答案可还原题干缺失公式时应使用；仅明确独立的答案段落才忽略。",
                self._notation_instruction(),
            ],
        }
        schema = {
            "type": "object", "additionalProperties": False,
            "required": ["status", "required_knowledge_points", "latest_topic_id", "primary_object", "main_question", "decisive_condition", "evidence", "confidence", "reason", "review_reasons"],
            "properties": {
                "status": {"type": "string", "enum": ["routed", "review"]},
                "required_knowledge_points": {"type": "array", "items": {"type": "string"}},
                "latest_topic_id": {"type": ["string", "null"], "enum": topic_ids + [None]},
                "primary_object": {"type": "string"},
                "main_question": {"type": "string"},
                "decisive_condition": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
                "review_reasons": {"type": "array", "items": {"type": "string"}},
            },
        }
        return self._structured_request("math_topic_routing", prompt, schema)

    def build_batch_routing_request(
        self, questions: list[dict[str, Any]], taxonomy: Any, rules: dict[str, Any]
    ) -> dict[str, Any]:
        """第一阶段确定最终归属专题，固定目录内容位于动态题目前以便命中提示词缓存。"""
        if not questions or len(questions) > 10:
            raise ValueError("专题路由每次必须包含 1-10 道题")
        topics = taxonomy.topic_catalog()
        topic_ids = [item["id"] for item in topics]
        static_context = {
            "task": "批量确定中考数学题的最终归属专题。逐题独立执行 Skill 的分阶段路由规则。",
            "policy": self._policy(rules),
            "all_topics_in_order": topics,
            "instructions": [
                "列出完整解题不可缺少的知识点。专题10之前的基础或前置知识专题选择目录顺序中最晚的必备专题；从专题10“三角形”起及后续专题的【大题】选择最终解题的核心突破口所属专题，不能因扇形面积、旋转、坐标或代数运算等辅助步骤改变归属。",
                "网页目录只是弱提示；不得从 page_scope_hint 直接抄专题。",
                "含 sin、cos、tan、cot 或特殊角三角函数值时，必须把三角函数列为必备知识点。",
                "题干、公式或答案不足时返回 review，不得猜测；reason 仅保留短句。",
                "input_warnings 仅用于判断信息是否足够；不得把排版警告误写成题干、答案存在另一道题。",
                self._notation_instruction(),
            ],
        }
        dynamic_context = {"questions": [self._question(question) for question in questions]}
        item_schema = {
            "type": "object", "additionalProperties": False,
            "required": ["exercise_id", "status", "required_knowledge_points", "latest_topic_id", "confidence", "reason", "review_reasons"],
            "properties": {
                "exercise_id": {"type": "string"},
                "status": {"type": "string", "enum": ["routed", "review"]},
                "required_knowledge_points": {"type": "array", "items": {"type": "string"}},
                "latest_topic_id": {"type": ["string", "null"], "enum": topic_ids + [None]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
                "review_reasons": {"type": "array", "items": {"type": "string"}},
            },
        }
        schema = {
            "type": "object", "additionalProperties": False, "required": ["results"],
            "properties": {"results": {"type": "array", "minItems": len(questions), "maxItems": len(questions), "items": item_schema}},
        }
        return self._staged_structured_request("math_batch_topic_routing", static_context, dynamic_context, schema)

    def route_fast_batch(
        self, questions: list[dict[str, Any]], taxonomy: Any, rules: dict[str, Any]
    ) -> list[dict[str, Any]]:
        payload = self._decode(self._request("POST", "/responses", self.build_batch_routing_request(questions, taxonomy, rules)))
        return self._batch_rows(payload, questions, "专题路由")

    def build_topic_batch_request(
        self,
        questions: list[dict[str, Any]],
        topic_id: str,
        taxonomy: Any,
        rules: dict[str, Any],
        routings: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """第二阶段只发送路由专题的目录，避免每个分块重复携带全量目录。"""
        if not questions or len(questions) > 10:
            raise ValueError("专题内分类每次必须包含 1-10 道题")
        is_large_question = all(taxonomy.is_large_question_scope(question) for question in questions)
        level2_id = taxonomy.large_question_level2_id(topic_id) if is_large_question else None
        if is_large_question and not level2_id:
            raise ValueError(f"专题 {topic_id} 缺少唯一的【大题】二级目录")
        catalog = taxonomy.classification_catalog_for_topic(topic_id, level2_id)
        candidates = taxonomy.candidates(topic_id, level2_id)
        if len(catalog) != 1:
            raise ValueError("专题路由没有对应的可分类目录")
        static_context = {
            "task": "复核专题路由后，将题目归入已确定专题的现有目录。只能选择给定目录，不能臆造已生效目录。",
            "policy": self._policy(rules),
            "all_topics_in_order": taxonomy.topic_catalog(),
            "directory_catalog": catalog,
            "instructions": [
                "先独立复核 topic_routing 是否符合分阶段规则：专题10之前检查是否遗漏更晚的必备专题；专题10及后续的【大题】检查是否把辅助步骤误作核心考点。若发现应改到其他专题，必须设置 self_check_passed=false、status=review、reroute_topic_id=修正专题，并将目录目标留空；服务端会加载修正专题的详细目录后重新分类。",
                "自检通过后，只在已路由专题内按首要数学对象确定三级、按主问或决定性条件确定四级。",
                "directory_catalog 仅是当前已路由专题的详细目录，不代表完整专题目录；不得据此声称系统缺少其他专题目录。",
                "当前题属于【大题】时，directory_catalog 已限定为该专题的【大题】二级目录；只能在其三级、四级目录中选择。" if is_large_question else "当前题不限定为【大题】；按 directory_catalog 选择可用目录。",
                "显式符号与目录的含/不含语义冲突、候选不唯一或信息不足时必须返回 review。",
                "仅当命中的三级目录有四级子目录时才匹配四级；没有四级子目录时三级即末级，target_level4_id 必须为 null。",
                "input_warnings 仅提示输入风险；答案编号与题干不一致时，须依据数学连续性判断是否属于同一道题，不得直接丢弃答案。",
                "新目录只能作为候选，单道题不得创建新目录；reason 仅保留短句。",
                self._notation_instruction(),
            ],
        }
        dynamic_context = {
            "questions": [self._question(question) for question in questions],
            "topic_routing": [
                {
                    "exercise_id": str(question["exercise_id"]),
                    "latest_topic_id": routings[str(question["exercise_id"])].get("latest_topic_id"),
                    "required_knowledge_points": routings[str(question["exercise_id"])].get("required_knowledge_points") or [],
                }
                for question in questions
            ],
        }
        proposal_schema = {
            "type": "object", "additionalProperties": False,
            "required": ["kind", "title", "cluster_key", "reason"],
            "properties": {
                "kind": {"type": "string", "enum": ["none", "new_level3", "new_level4"]},
                "title": {"type": ["string", "null"]},
                "cluster_key": {"type": ["string", "null"]},
                "reason": {"type": ["string", "null"]},
            },
        }
        item_schema = {
            "type": "object", "additionalProperties": False,
            "required": ["exercise_id", "status", "target_level3_id", "target_level4_id", "confidence", "reason", "review_reasons", "proposal", "self_check_passed", "self_check_violations", "self_check_reason", "self_check_confidence", "reroute_topic_id"],
            "properties": {
                "exercise_id": {"type": "string"},
                "status": {"type": "string", "enum": ["suggested", "review"]},
                "target_level3_id": self._candidate_id_schema(candidates, "level3_id"),
                "target_level4_id": self._candidate_id_schema(candidates, "level4_id"),
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
                "review_reasons": {"type": "array", "items": {"type": "string"}},
                "proposal": proposal_schema,
                "self_check_passed": {"type": "boolean"},
                "self_check_violations": {"type": "array", "items": {"type": "string"}},
                "self_check_reason": {"type": "string"},
                "self_check_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reroute_topic_id": {"type": ["string", "null"], "enum": [item["id"] for item in taxonomy.topic_catalog()] + [None]},
            },
        }
        schema = {
            "type": "object", "additionalProperties": False, "required": ["results"],
            "properties": {"results": {"type": "array", "minItems": len(questions), "maxItems": len(questions), "items": item_schema}},
        }
        return self._staged_structured_request("math_topic_batch_classification", static_context, dynamic_context, schema)

    def classify_topic_batch(
        self,
        questions: list[dict[str, Any]],
        topic_id: str,
        taxonomy: Any,
        rules: dict[str, Any],
        routings: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        request = self.build_topic_batch_request(questions, topic_id, taxonomy, rules, routings)
        rows = self._batch_rows(self._decode(self._request("POST", "/responses", request)), questions, "专题内分类")
        for row in rows:
            row["routing"] = routings[str(row["exercise_id"])]
            row["self_check"] = {
                "passed": self._boolean(row.pop("self_check_passed"), "self_check_passed"),
                "violations": self._string_list(row.pop("self_check_violations")),
                "reason": str(row.pop("self_check_reason")),
                "confidence": self._confidence(row.pop("self_check_confidence")),
                "reroute_topic_id": row.pop("reroute_topic_id"),
            }
        return rows

    def build_fast_batch_request(
        self, questions: list[dict[str, Any]], taxonomy: Any, rules: dict[str, Any]
    ) -> dict[str, Any]:
        """构造整页快速模式请求；每道题仍需独立给出知识分析和自审结果。"""
        candidates = taxonomy.all_targets()
        topic_ids = [item["id"] for item in taxonomy.topic_catalog()]
        prompt = {
            "task": "批量分类中考数学题。逐题独立执行完整 Skill 决策协议并返回结果，不得让相邻题目互相影响。",
            "policy": self._policy(rules),
            "directory_catalog": taxonomy.classification_catalog(),
            "instructions": [
                "每题先列出完整必备知识点，再按分阶段规则选专题：专题10之前按最晚必备知识点；从专题10“三角形”起及后续专题的【大题】按最终解题核心突破口，辅助的扇形面积、旋转、坐标或代数运算不得改变归属。",
                "page_scope_hint 和 site_context 只作弱提示；与实际题目冲突时必须忽略。",
                "在选定专题内，按首要数学对象确定三级，按主问或决定性条件确定四级。",
                "逐题检查显式符号与目录的含/不含语义；无法唯一命中时返回 review。",
                "仅对存在四级子目录的三级目录执行四级唯一匹配；无四级子目录时三级即末级，不得作为复核理由。",
                "input_warnings 表示排版风险；答案编号与题干不一致时，须依据数学连续性判断是否属于同一道题，不得直接丢弃答案。",
                "实际题干是分类主依据；题干公式缺失但答案能还原关键关系时，可使用对应答案完成分类；答案明确属于独立题目时才忽略该段。",
                "只返回 JSON；知识点和说明使用短语，reason 限一短句，不输出推理过程。",
                self._notation_instruction(),
                "results 必须恰好包含每个 exercise_id 一次。",
            ],
            # 目录和规则在前、动态题目在后，使支持精确前缀缓存的兼容网关可以复用固定上下文。
            "questions": [self._question(question) for question in questions],
        }
        item_schema = {
            "type": "object", "additionalProperties": False,
            "required": [
                "exercise_id", "status", "required_knowledge_points", "latest_topic_id",
                "primary_object", "main_question", "decisive_condition",
                "target_level3_id", "target_level4_id", "confidence", "reason", "review_reasons",
                "proposal",
            ],
            "properties": {
                "exercise_id": {"type": "string"},
                "status": {"type": "string", "enum": ["suggested", "review"]},
                "required_knowledge_points": {"type": "array", "items": {"type": "string"}},
                "latest_topic_id": {"type": ["string", "null"], "enum": [*topic_ids, None]},
                "primary_object": {"type": "string"},
                "main_question": {"type": "string"},
                "decisive_condition": {"type": "string"},
                "target_level3_id": self._candidate_id_schema(candidates, "level3_id"),
                "target_level4_id": self._candidate_id_schema(candidates, "level4_id"),
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
                "review_reasons": {"type": "array", "items": {"type": "string"}},
                "proposal": {
                    "type": "object", "additionalProperties": False,
                    "required": ["kind", "title", "cluster_key", "reason"],
                    "properties": {
                        "kind": {"type": "string", "enum": ["none", "new_level3", "new_level4"]},
                        "title": {"type": ["string", "null"]},
                        "cluster_key": {"type": ["string", "null"]},
                        "reason": {"type": ["string", "null"]},
                    },
                },
            },
        }
        schema = {
            "type": "object", "additionalProperties": False, "required": ["results"],
            "properties": {
                "results": {"type": "array", "minItems": len(questions), "maxItems": len(questions), "items": item_schema},
            },
        }
        return self._structured_request("math_fast_batch_classification", prompt, schema)

    def classify_fast_batch(
        self, questions: list[dict[str, Any]], taxonomy: Any, rules: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if not questions or len(questions) > 10:
            raise ValueError("快速批量分类每次必须包含 1-10 道题")
        request = self.build_fast_batch_request(questions, taxonomy, rules)
        payload = self._decode(self._request("POST", "/responses", request))
        rows = payload.get("results")
        if not isinstance(rows, list):
            raise CloudProviderError("云端模型未返回批量 results 数组")
        by_id: dict[str, dict[str, Any]] = {}
        expected_ids = {str(question["exercise_id"]) for question in questions}
        for row in rows:
            if not isinstance(row, dict):
                raise CloudProviderError("云端模型批量结果包含非对象项")
            exercise_id = str(row.get("exercise_id", ""))
            if exercise_id not in expected_ids or exercise_id in by_id:
                raise CloudProviderError("云端模型批量结果的题目 ID 缺失、重复或越界")
            decision = dict(row)
            decision["routing"] = {
                "latest_topic_id": row.get("latest_topic_id"),
                "required_knowledge_points": row.get("required_knowledge_points") or [],
                "primary_object": row.get("primary_object"),
                "main_question": row.get("main_question"),
                "decisive_condition": row.get("decisive_condition"),
                "confidence": row.get("confidence"),
                "reason": row.get("reason"),
                "review_reasons": row.get("review_reasons") or [],
            }
            by_id[exercise_id] = decision
        if set(by_id) != expected_ids:
            raise CloudProviderError("云端模型没有返回全部题目的分类结果")
        return [by_id[str(question["exercise_id"])] for question in questions]

    def propose_directory_refactor(self, context: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
        """按一个已选中的二级或三级目录提出待审核的粗粒度目录方案。"""
        selected = context["selected_level3"]
        focus = context["focus"]
        level3_keys = [str(item["id"]) for item in selected]
        sampling = context["collection"].get("sampling") or {}
        is_sampled = sampling.get("mode") == "stratified_page"
        support_minimum = (
            context["sampled_level4_candidate_min_count"]
            if is_sampled else context["minimum_level4_question_count"]
        )
        static_context = {
            "task": "为一个中考数学目录 Focus 设计待人工审核的三级、四级目录优化方案。不得执行写入或逐题分类。",
            "policy": self._policy(rules),
            "focus": focus,
            "old_directory_tree": selected,
            "reference_directory_tree": context["reference_directory_tree"],
            "collection": context["collection"],
            "minimum_level4_question_count": context["minimum_level4_question_count"],
            "instructions": [
                "三级目录必须按题干首要数学对象或情境划分；四级目录按最终主问或决定性条件划分。",
                "目录标题必须剥离单题故事背景；四级标题以“考法N：”开头，且每个四级目录至少有给定最小题量。",
                "每个三级、四级目录均须提供 basis，简洁说明其数学分类边界、纳入条件或与相邻目录的区分；不得引用具体题号或故事背景。该文字可写入 Excel N 列作为分类依据。",
                "这是目录骨架优化，不是逐题归类任务。不得输出全部题目的分类结果，也不得要求每道题落入某个四级目录。",
                "每个新建或保留的四级目录必须提供 supporting_exercise_ids：它们只是符合该目录的题号，用于核验该目录的样本或全量题量，不是数学证明题；这些题号在不同四级目录间不得重复。其余题目无需归属。",
                "Focus 为三级时，level3 只能保留一个，并使用给定的 key；不得移动到其他三级或新建三级。",
                "旧目录和同专题其他三级、四级目录只作参考，不能照抄无题量支持的目录。",
                "collection 记录本轮未能读取的题目或分页；只能基于 questions 中已成功读取的题目提出方案，并在 notes 中说明覆盖缺口，不得臆造缺失题目内容。",
                "二级 Focus 无法形成满足题量的四级分类时，不要捏造目录；可保留三级末级目录。三级 Focus 且题量达到门槛时必须给出四级分类。",
                self._notation_instruction(),
            ],
        }
        if is_sampled:
            # 抽样只改变候选证据门槛，不把数百道完整题干塞入一次请求。
            # 先用低强度并发提炼短数学特征，再由原目录分类强度完成最终归纳。
            static_context["instructions"].append(
                "本轮是分页分层抽样，只产出待审核候选，不得直接写入 Excel。样本中有 3 道不同匹配题才可提出四级目录；应尽量列出最多 6 道实际匹配题。样本中 4 至 6 道为强候选，恰好 3 道为普通候选；少于 3 道必须在 notes 标明证据不足，不得猜测。最终写入前仍须全量核验实际题量不少于 6 道。"
            )
            request_effort = self.reasoning_effort
        else:
            request_effort = self.directory_reasoning_effort
        # 无论全量还是抽样，都先并发提取短特征，避免单次请求过大、无进度且易卡住。
        question_signals, signal_failed_ids = self._directory_question_signals(context["questions"])
        if signal_failed_ids:
            context["collection"]["failed_signal_exercise_ids"] = signal_failed_ids
            static_context["instructions"].append(
                "部分题目的特征提取请求失败，failed_signal_exercise_ids 中的题只能视为覆盖缺口；不得根据缺失题目猜测目录。"
            )
        if not question_signals:
            raise CloudProviderError("所有抽样题目的数学特征提取均失败，无法生成目录方案")
        dynamic_context = {
            "question_signals": question_signals,
            "question_count": len(context["questions"]),
            "sampling": sampling,
        }
        exercise_ids = [str(question["exercise_id"]) for question in context["questions"]]
        level4_schema = {
            "type": "object", "additionalProperties": False, "required": ["key", "title", "basis", "supporting_exercise_ids"],
            "properties": {
                "key": {"type": "string"}, "title": {"type": "string"}, "basis": {"type": "string"},
                "supporting_exercise_ids": {
                    "type": "array", "minItems": support_minimum,
                    "maxItems": context["minimum_level4_question_count"],
                    # 部分兼容网关的 Structured Outputs 不支持 uniqueItems；
                    # 返回后仍由 directory_refactor 严格校验同目录和跨目录的题号去重。
                    "items": {"type": "string", "enum": exercise_ids},
                },
            },
        }
        level3_schema = {
            "type": "object", "additionalProperties": False, "required": ["key", "title", "basis", "level4"],
            "properties": {
                "key": {"type": "string", "enum": level3_keys} if focus["level"] == 3 else {"type": "string"},
                "title": {"type": "string"}, "basis": {"type": "string"},
                "level4": {"type": "array", "items": level4_schema},
            },
        }
        schema = {
            "type": "object", "additionalProperties": False, "required": ["level3", "notes"],
            "properties": {
                "level3": {"type": "array", "minItems": 1, "items": level3_schema},
                "notes": {"type": "array", "items": {"type": "string"}},
            },
        }
        request = self._staged_structured_request(
            "math_directory_refactor", static_context, dynamic_context, schema,
            reasoning_effort=request_effort,
        )
        return self._decode(self._request_with_retry("POST", "/responses", request))

    @staticmethod
    def _clip_directory_text(value: Any, limit: int) -> str:
        """保留题干头尾，避免长材料题吞掉目录方案的上下文窗口。"""
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        separator = " … "
        head = max(1, (limit - len(separator)) * 2 // 3)
        tail = max(1, limit - len(separator) - head)
        return f"{text[:head]}{separator}{text[-tail:]}"

    def _directory_question(self, question: dict[str, Any]) -> dict[str, Any]:
        full = self._question(question)
        return {
            "exercise_id": full["exercise_id"],
            "text": self._clip_directory_text(full["text"], 900),
            "question_latex": self._clip_directory_text(full["question_latex"], 400),
            "answer": self._clip_directory_text(full["answer"], 300),
            "answer_latex": self._clip_directory_text(full["answer_latex"], 240),
            "page_scope_hint": self._clip_directory_text(full["page_scope_hint"], 120),
        }

    def _directory_question_signals(self, questions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        """分批并发提取题目数学特征；这是目录归纳的中间摘要，不是逐题分类结果。"""
        batches = [questions[index:index + self.directory_batch_size] for index in range(0, len(questions), self.directory_batch_size)]

        def request_batch(index: int, batch: list[dict[str, Any]]) -> tuple[int, list[dict[str, Any]]]:
            exercise_ids = [str(question["exercise_id"]) for question in batch]
            schema = {
                "type": "object", "additionalProperties": False, "required": ["signals"],
                "properties": {"signals": {"type": "array", "minItems": len(batch), "maxItems": len(batch), "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["exercise_id", "primary_object", "main_question", "decisive_condition"],
                    "properties": {
                        "exercise_id": {"type": "string", "enum": exercise_ids},
                        "primary_object": {"type": "string"}, "main_question": {"type": "string"},
                        "decisive_condition": {"type": "string"},
                    },
                }}},
            }
            prompt = {
                "task": "仅提取每道中考数学题的短数学特征，供后续粗粒度目录设计使用；不是目录分类。",
                "instructions": [
                    "每道题必须返回一次，字段均用极简数学术语。",
                    "primary_object 写首要对象或情境；main_question 写最终主问；decisive_condition 写决定性条件或方法。",
                    "剥离具体故事背景，不给出三级、四级目录名称，不解释推理。",
                ],
                "questions": [self._directory_question(question) for question in batch],
            }
            request = self._structured_request("math_directory_question_signals", prompt, schema, reasoning_effort=self.directory_reasoning_effort)
            payload = self._decode(self._request_with_retry("POST", "/responses", request))
            rows = payload.get("signals") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise CloudProviderError("目录题目特征未返回 signals 数组")
            by_id = {str(row.get("exercise_id", "")): row for row in rows if isinstance(row, dict)}
            if len(by_id) != len(batch) or set(by_id) != set(exercise_ids):
                raise CloudProviderError("目录题目特征没有覆盖当前批次的全部题目")
            return index, [by_id[exercise_id] for exercise_id in exercise_ids]

        results: list[list[dict[str, Any]] | None] = [None] * len(batches)
        failed_exercise_ids: list[str] = []
        with ThreadPoolExecutor(max_workers=min(self.directory_concurrency, len(batches))) as executor:
            futures = {
                executor.submit(request_batch, index, batch): batch
                for index, batch in enumerate(batches)
            }
            for future in as_completed(futures):
                try:
                    index, rows = future.result()
                except CloudProviderError:
                    # 单个特征分批失败不能丢弃其他已完成分批；最终方案会带上覆盖缺口。
                    failed_exercise_ids.extend(str(question["exercise_id"]) for question in futures[future])
                    continue
                results[index] = rows
        return [row for batch in results if batch for row in batch], failed_exercise_ids

    def _request_with_retry(self, method: str, path: str, payload: Any) -> Any:
        """目录分析可安全重试瞬时模型或网关失败；参数错误不做无意义重试。"""
        last_error: CloudProviderError | None = None
        for attempt in range(self.directory_retry_attempts):
            try:
                return self._request(
                    method, path, payload,
                    timeout_seconds=self.directory_request_timeout_seconds,
                )
            except CloudProviderError as error:
                message = str(error)
                if "HTTP 4" in message and "HTTP 429" not in message:
                    raise
                last_error = error
                if attempt + 1 < self.directory_retry_attempts:
                    time.sleep(1.5 * (2 ** attempt))
        assert last_error is not None
        raise CloudProviderError(f"目录模型请求已重试 {self.directory_retry_attempts} 次仍失败：{last_error}") from last_error

    def _structured_request(
        self, name: str, prompt: dict[str, Any], schema: dict[str, Any], reasoning_effort: str | None = None
    ) -> dict[str, Any]:
        return self._responses_request(name, [prompt], schema, reasoning_effort=reasoning_effort)

    def _staged_structured_request(
        self, name: str, static_context: dict[str, Any], dynamic_context: dict[str, Any], schema: dict[str, Any],
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """把固定 Skill/目录和动态题目拆成连续消息，便于兼容网关复用共同前缀。"""
        return self._responses_request(name, [static_context, dynamic_context], schema, reasoning_effort=reasoning_effort)

    def _responses_request(
        self, name: str, contexts: list[dict[str, Any]], schema: dict[str, Any], reasoning_effort: str | None = None
    ) -> dict[str, Any]:
        """按 Responses API 生成请求；连续 input 消息让固定上下文位于动态题目前。"""
        request: dict[str, Any] = {
            "model": self.model,
            "instructions": "你是中考数学目录审核器。网页目录只是弱提示；实际题目和 Skill 决策协议优先。只返回符合 JSON Schema 的结果。",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": json.dumps(context, ensure_ascii=False, separators=(",", ":"))}]}
                for context in contexts
            ],
            "text": {"format": {"type": "json_schema", "name": name, "strict": True, "schema": schema}},
            "reasoning": {"effort": reasoning_effort or self.reasoning_effort},
        }
        return request

    @staticmethod
    def _batch_rows(payload: dict[str, Any], questions: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
        rows = payload.get("results")
        if not isinstance(rows, list):
            raise CloudProviderError(f"云端模型未返回{label} results 数组")
        expected_ids = [str(question["exercise_id"]) for question in questions]
        by_id: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise CloudProviderError(f"云端模型{label}结果包含非对象项")
            exercise_id = str(row.get("exercise_id", ""))
            if exercise_id not in expected_ids or exercise_id in by_id:
                raise CloudProviderError(f"云端模型{label}结果的题目 ID 缺失、重复或越界")
            by_id[exercise_id] = dict(row)
        if set(by_id) != set(expected_ids):
            raise CloudProviderError(f"云端模型没有返回全部{label}结果")
        return [by_id[exercise_id] for exercise_id in expected_ids]

    def classify(self, question: dict[str, Any], taxonomy: Any, rules: dict[str, Any]) -> dict[str, Any]:
        """实时精准分类：全局专题路由、专题内目录分类与第二阶段自检。"""
        routing = self._decode(self._request("POST", "/responses", self.build_routing_request(question, taxonomy, rules)))
        topic_id = routing.get("latest_topic_id")
        if routing.get("status") != "routed" or not taxonomy.topic(str(topic_id)):
            return {
                "status": "review", "target_level3_id": None, "target_level4_id": None,
                "confidence": 0.0, "reason": str(routing.get("reason") or "无法确定最终归属专题"),
                "review_reasons": self._string_list(routing.get("review_reasons")) or ["topic_routing_failed"],
                "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None},
                "routing": routing,
            }
        current_topic_id = str(topic_id)
        for reroute_count in range(2):
            decision = self.classify_topic_batch(
                [question], current_topic_id, taxonomy, rules, {str(question["exercise_id"]): routing}
            )[0]
            self_check = decision.get("self_check") if isinstance(decision.get("self_check"), dict) else {}
            corrected_topic_id = str(self_check.get("reroute_topic_id") or "").strip()
            if (
                decision.get("status") != "review"
                or self_check.get("passed") is not False
                or not corrected_topic_id
                or corrected_topic_id == current_topic_id
                or not taxonomy.topic(corrected_topic_id)
            ):
                break
            if taxonomy.is_large_question_scope(question) and not taxonomy.large_question_level2_id(corrected_topic_id):
                break
            routing = dict(routing)
            routing["initial_topic_id"] = routing.get("initial_topic_id") or current_topic_id
            routing["rerouted_from_topic_id"] = current_topic_id
            routing["latest_topic_id"] = corrected_topic_id
            routing["reroute_reason"] = str(self_check.get("reason") or decision.get("reason") or "二阶段目录自检要求修正专题")
            routing["reroute_count"] = reroute_count + 1
            current_topic_id = corrected_topic_id
        return decision

    def create_batch_jsonl(self, questions: list[dict[str, Any]], taxonomy: Any, rules: dict[str, Any]) -> str:
        lines: list[str] = []
        for question in questions:
            # Provider Batch 无法在同一个作业内串联三次依赖调用，因此给每题完整候选；
            # 导入后仍会经过相同的服务端语义冲突校验。
            candidates = taxonomy.all_targets()
            if not candidates:
                raise ValueError(f"题目 {question.get('exercise_id')} 没有可用目录")
            lines.append(json.dumps({
                "custom_id": f"exercise-{question['exercise_id']}-{uuid.uuid4().hex[:10]}",
                "method": "POST", "url": "/v1/responses", "body": self.build_request(question, candidates, rules),
            }, ensure_ascii=False, separators=(",", ":")))
        return "\n".join(lines) + ("\n" if lines else "")

    def submit_batch(self, jsonl_bytes: bytes) -> dict[str, Any]:
        boundary = f"----wulou{uuid.uuid4().hex}"
        chunks = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\nbatch\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"classification.jsonl\"\r\nContent-Type: application/jsonl\r\n\r\n".encode(),
            jsonl_bytes, f"\r\n--{boundary}--\r\n".encode(),
        ]
        file_info = self._request("POST", "/files", b"".join(chunks), {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        return self._request("POST", "/batches", {"input_file_id": file_info["id"], "endpoint": "/v1/responses", "completion_window": "24h"})

    def get_batch(self, provider_batch_id: str) -> dict[str, Any]:
        return self._request("GET", f"/batches/{provider_batch_id}")

    def get_file_content(self, file_id: str) -> bytes:
        return self._request("GET", f"/files/{file_id}/content", raw=True)

    def _request(
        self,
        method: str,
        path: str,
        payload: Any = None,
        extra_headers: dict[str, str] | None = None,
        raw: bool = False,
        timeout_seconds: int | None = None,
    ) -> Any:
        key = self.api_key or os.environ.get(self.api_key_env)
        if not key:
            raise CloudProviderError(f"未设置环境变量 {self.api_key_env}")
        body = None if payload is None else (payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        headers = {"Authorization": f"Bearer {key}"}
        if body is not None and not isinstance(payload, bytes): headers["Content-Type"] = "application/json"
        if extra_headers: headers.update(extra_headers)
        request = Request(self.base_url + path, data=body, method=method, headers=headers)
        try:
            with urlopen(request, timeout=timeout_seconds or self.timeout_seconds) as response:
                data = response.read()
        except HTTPError as error:
            # 只回显网关返回的错误摘要，不回显请求内容或密钥。
            detail = self._http_error_detail(error)
            suffix = f"：{detail}" if detail else ""
            raise CloudProviderError(f"云端模型返回 HTTP {error.code}{suffix}") from error
        except URLError as error:
            raise CloudProviderError("无法连接云端模型") from error
        except TimeoutError as error:
            limit = timeout_seconds or self.timeout_seconds
            raise CloudProviderError(f"云端模型在 {limit} 秒内未响应") from error
        if raw:
            return data
        try:
            decoded = json.loads(data.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise CloudProviderError("云端接口返回的响应不是有效 JSON") from error
        if not isinstance(decoded, dict):
            raise CloudProviderError("云端接口响应必须是 JSON 对象")
        return decoded

    @staticmethod
    def _decode(response: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise CloudProviderError("云端接口响应必须是 JSON 对象")
        if response.get("status") in {"failed", "cancelled", "incomplete"}:
            error = response.get("error") or response.get("incomplete_details") or {}
            detail = error.get("message") if isinstance(error, dict) else ""
            raise CloudProviderError(f"云端 Responses 请求未完成{f'：{detail}' if detail else ''}")
        content = response.get("output_text")
        if not isinstance(content, str):
            for item in response.get("output") or []:
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        content = part["text"]
                        break
                if isinstance(content, str):
                    break
        if isinstance(content, str):
            try:
                decoded = json.loads(content)
            except json.JSONDecodeError as error:
                raise CloudProviderError("云端模型返回的分类结果不是有效 JSON") from error
            if isinstance(decoded, dict):
                return decoded
            raise CloudProviderError("云端模型返回的分类结果必须是 JSON 对象")
        raise CloudProviderError("云端 Responses 未返回可解析的结构化结果")

    @staticmethod
    def _http_error_detail(error: HTTPError) -> str:
        """提取兼容网关的安全错误摘要，便于在面板中定位配置问题。"""
        try:
            raw = error.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                nested = parsed.get("error")
                if isinstance(nested, dict) and isinstance(nested.get("message"), str):
                    return nested["message"][:500]
                if isinstance(parsed.get("message"), str):
                    return parsed["message"][:500]
            return raw.strip().replace("\n", " ")[:500]
        except (OSError, UnicodeError, json.JSONDecodeError):
            return ""


# 兼容已保存的本地 Python 导入路径；实际请求协议为 Responses。
OpenAIResponsesProvider = OpenAIChatCompletionsProvider
