"""OpenAI Responses 与 Batch API 适配器。

不依赖 SDK，便于接入兼容 ``/v1/responses`` 的云端网关。
"""

from __future__ import annotations

import json
import os
import uuid
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
        # 这是网络失联保护，不是页面分类的业务时限。兼容旧配置的 180 秒也提升到 600 秒，
        # 防止上游已经完成但本机先断开并重复计费。
        self.timeout_seconds = max(600, int(settings.get("timeout_seconds", 600)))
        if not self.model:
            raise ValueError("cloud.model 不能为空")
        if self.reasoning_effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("cloud.reasoning_effort 必须是 none、low、medium、high、xhigh 或 max")

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
        catalog = taxonomy.classification_catalog_for_topic(topic_id)
        candidates = taxonomy.candidates(topic_id, None)
        if len(catalog) != 1:
            raise ValueError("专题路由没有对应的可分类目录")
        static_context = {
            "task": "复核专题路由后，将题目归入已确定专题的现有大题目录。只能选择给定目录，不能臆造已生效目录。",
            "policy": self._policy(rules),
            "all_topics_in_order": taxonomy.topic_catalog(),
            "directory_catalog": catalog,
            "instructions": [
                "先独立复核 topic_routing 是否符合分阶段规则：专题10之前检查是否遗漏更晚的必备专题；专题10及后续的【大题】检查是否把辅助步骤误作核心考点。发现任何疑点时 self_check_passed=false 并写明原因。",
                "自检通过后，只在已路由专题内按首要数学对象确定三级、按主问或决定性条件确定四级。",
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
            "required": ["exercise_id", "status", "target_level3_id", "target_level4_id", "confidence", "reason", "review_reasons", "proposal", "self_check_passed", "self_check_violations", "self_check_reason", "self_check_confidence"],
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
            }
            row["audit"] = None
        return rows

    def build_audit_request(
        self, question: dict[str, Any], routing: dict[str, Any], decision: dict[str, Any], target: Target,
        taxonomy: Any, rules: dict[str, Any]
    ) -> dict[str, Any]:
        """让独立一次模型调用从全量专题中寻找能推翻建议分类的证据。"""
        prompt = {
            "task": "独立审核建议分类是否严格符合 Skill；重点寻找错判分类阶段、错误核心考点或遗漏的前置知识。",
            "policy": self._policy(rules),
            "question": self._question(question),
            "all_topics_in_order": taxonomy.topic_catalog(),
            "topic_routing": routing,
            "proposed_decision": decision,
            "proposed_target": self._candidate_rows([target])[0],
            "checks": [
                "基础或前置知识专题是否遗漏目录顺序更靠后的必备专题；专题10及后续专题的【大题】是否错误地把辅助步骤当成核心考点",
                "三级是否按首要数学对象或情境划分",
                "四级是否按主问或决定性条件划分",
                "显式符号或运算是否与目录的含/不含语义冲突",
                "是否能够唯一命中",
                self._notation_instruction(),
            ],
        }
        schema = {
            "type": "object", "additionalProperties": False,
            "required": ["passed", "violations", "reason", "confidence"],
            "properties": {
                "passed": {"type": "boolean"},
                "violations": {"type": "array", "items": {"type": "string"}},
                "reason": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
        }
        return self._structured_request("math_classification_audit", prompt, schema)

    def build_batch_audit_request(
        self, items: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], Target]], taxonomy: Any, rules: dict[str, Any]
    ) -> dict[str, Any]:
        """批量独立审核路由和专题内分类，避免把数学语义写死为本地特判。"""
        if not items or len(items) > 10:
            raise ValueError("独立审核每次必须包含 1-10 道题")
        static_context = {
            "task": "独立审核中考数学题的专题与目录建议。逐题从完整专题目录和实际题干出发，不得依赖任何硬编码知识点特判。",
            "policy": self._policy(rules),
            "all_topics_in_order": taxonomy.topic_catalog(),
            "checks": [
                "完整列举并核对必备知识点，而非仅检查预设知识词",
                "专题10之前的基础或前置知识专题是否遗漏最晚必备知识点",
                "专题10及后续专题的【大题】是否把扇形面积、旋转、坐标或代数运算等辅助步骤误当成核心考点",
                "去掉候选核心知识点后，主结论的证明或求解主线是否失效",
                "三级是否按首要数学对象或情境划分，四级是否按主问或决定性条件划分，且是否唯一命中",
                self._notation_instruction(),
            ],
        }
        dynamic_context = {
            "items": [
                {
                    "exercise_id": str(question["exercise_id"]),
                    "question": self._question(question),
                    "topic_routing": routing,
                    "proposed_decision": decision,
                    "proposed_target": self._candidate_rows([target])[0],
                }
                for question, routing, decision, target in items
            ],
        }
        item_schema = {
            "type": "object", "additionalProperties": False,
            "required": ["exercise_id", "passed", "violations", "reason", "confidence"],
            "properties": {
                "exercise_id": {"type": "string"},
                "passed": {"type": "boolean"},
                "violations": {"type": "array", "items": {"type": "string"}},
                "reason": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
        }
        schema = {
            "type": "object", "additionalProperties": False, "required": ["results"],
            "properties": {"results": {"type": "array", "minItems": len(items), "maxItems": len(items), "items": item_schema}},
        }
        return self._staged_structured_request("math_batch_classification_audit", static_context, dynamic_context, schema)

    def audit_batch(
        self, items: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], Target]], taxonomy: Any, rules: dict[str, Any]
    ) -> list[dict[str, Any]]:
        request = self.build_batch_audit_request(items, taxonomy, rules)
        payload = self._decode(self._request("POST", "/responses", request))
        return self._batch_rows(payload, [item[0] for item in items], "独立审核")

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
                "audit_passed", "audit_violations", "proposal",
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
                "audit_passed": {"type": "boolean"},
                "audit_violations": {"type": "array", "items": {"type": "string"}},
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
            decision["audit"] = {
                "passed": row.get("audit_passed"),
                "violations": row.get("audit_violations") or [],
                "confidence": row.get("confidence"),
                "reason": row.get("reason"),
            }
            by_id[exercise_id] = decision
        if set(by_id) != expected_ids:
            raise CloudProviderError("云端模型没有返回全部题目的分类结果")
        return [by_id[str(question["exercise_id"])] for question in questions]

    def _structured_request(self, name: str, prompt: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        return self._responses_request(name, [prompt], schema)

    def _staged_structured_request(
        self, name: str, static_context: dict[str, Any], dynamic_context: dict[str, Any], schema: dict[str, Any]
    ) -> dict[str, Any]:
        """把固定 Skill/目录和动态题目拆成连续消息，便于兼容网关复用共同前缀。"""
        return self._responses_request(name, [static_context, dynamic_context], schema)

    def _responses_request(self, name: str, contexts: list[dict[str, Any]], schema: dict[str, Any]) -> dict[str, Any]:
        """按 Responses API 生成请求；连续 input 消息让固定上下文位于动态题目前。"""
        request: dict[str, Any] = {
            "model": self.model,
            "instructions": "你是中考数学目录审核器。网页目录只是弱提示；实际题目和 Skill 决策协议优先。只返回符合 JSON Schema 的结果。",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": json.dumps(context, ensure_ascii=False, separators=(",", ":"))}]}
                for context in contexts
            ],
            "text": {"format": {"type": "json_schema", "name": name, "strict": True, "schema": schema}},
            "reasoning": {"effort": self.reasoning_effort},
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

    def _requires_independent_audit(
        self, question: dict[str, Any], decision: dict[str, Any], target: Target, rules: dict[str, Any], audit_mode: str
    ) -> bool:
        """兼容单题接口的条件审核判断，与交互式作业保持相同的风险边界。"""
        if audit_mode == "always":
            return True
        self_check = decision.get("self_check") if isinstance(decision.get("self_check"), dict) else None
        if not self_check or self_check.get("passed") is not True:
            return True
        warnings = set(self._question(question).get("input_warnings") or [])
        if warnings - {"answer_text_missing"}:
            return True
        core_start = int((rules.get("rules") or {}).get("large_question_core_topic_start_order", 10))
        return target.topic_order < core_start or (
            target.topic_order >= core_start and str(target.level2_title) == "【大题】"
        )

    @staticmethod
    def _self_check_audit(decision: dict[str, Any]) -> dict[str, Any]:
        self_check = decision.get("self_check") if isinstance(decision.get("self_check"), dict) else {}
        return {
            "mode": "second_stage_self_check",
            "passed": self_check.get("passed") is True,
            "violations": list(self_check.get("violations") or []),
            "reason": str(self_check.get("reason") or "第二阶段自检通过，未触发独立审核"),
            "confidence": self_check.get("confidence"),
        }

    def classify(
        self, question: dict[str, Any], taxonomy: Any, rules: dict[str, Any], audit_mode: str = "conditional"
    ) -> dict[str, Any]:
        """实时精准分类：全局路由、专题内分类与自检，必要时独立审核。"""
        routing = self._decode(self._request("POST", "/responses", self.build_routing_request(question, taxonomy, rules)))
        topic_id = routing.get("latest_topic_id")
        if routing.get("status") != "routed" or not taxonomy.topic(str(topic_id)):
            return {
                "status": "review", "target_level3_id": None, "target_level4_id": None,
                "confidence": 0.0, "reason": str(routing.get("reason") or "无法确定最终归属专题"),
                "review_reasons": self._string_list(routing.get("review_reasons")) or ["topic_routing_failed"],
                "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None},
                "routing": routing, "audit": None,
            }
        decision = self.classify_topic_batch(
            [question], str(topic_id), taxonomy, rules, {str(question["exercise_id"]): routing}
        )[0]
        target = taxonomy.global_target(str(decision.get("target_level3_id")), decision.get("target_level4_id")) if decision.get("target_level3_id") else None
        if decision.get("status") != "suggested" or not target or target.topic_id != str(topic_id):
            decision["audit"] = None
            return decision
        if not self._requires_independent_audit(question, decision, target, rules, audit_mode):
            decision["audit"] = self._self_check_audit(decision)
            return decision
        try:
            audit = self._decode(self._request("POST", "/responses", self.build_audit_request(question, routing, decision, target, taxonomy, rules)))
        except CloudProviderError as error:
            # 前两阶段已有可检查的候选时，审核服务失败不应把整题变成 500 或丢失候选。
            decision["status"] = "review"
            decision["review_reasons"] = self._string_list(decision.get("review_reasons")) + ["audit_request_failed"]
            decision["reason"] = f"已生成候选，但独立审核未完成：{error}"
            decision["audit"] = None
            decision["confidence"] = min(
                self._confidence(decision.get("confidence")), self._confidence(routing.get("confidence"))
            )
            return decision
        decision["audit"] = audit
        audit_passed = self._boolean(audit.get("passed"), "passed")
        if not audit_passed:
            decision["status"] = "review"
            decision["review_reasons"] = self._string_list(decision.get("review_reasons")) + ["skill_audit_failed"] + self._string_list(audit.get("violations"))
            decision["reason"] = str(audit.get("reason") or decision.get("reason") or "Skill 二次审核未通过")
        decision["confidence"] = min(
            self._confidence(decision.get("confidence")),
            self._confidence(routing.get("confidence")),
            self._confidence(audit.get("confidence")),
        )
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
