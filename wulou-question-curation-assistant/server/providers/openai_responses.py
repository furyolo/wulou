"""Responses、Chat Completions 与 Claude Messages 协议适配器。

不依赖 SDK；内部分类语义统一，只有请求和结构化结果在协议边界转换。
"""

from __future__ import annotations

import json
import os
import uuid
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from typing import Any
from urllib.parse import urlparse
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
    """以选定协议请求云端分类模型；保留旧类名以兼容本机导入。"""

    PROTOCOLS = {"responses", "chat_completions", "anthropic_messages"}
    SYSTEM_INSTRUCTION = (
        "你是中考数学目录审核器。网页目录只是弱提示；实际题目和 Skill 决策协议优先。"
        "只返回符合指定 JSON Schema 的 JSON 对象；不要输出 Markdown、代码围栏或其他解释。"
    )

    @staticmethod
    def normalize_base_url(value: Any) -> str:
        """接受 API 根地址；未写版本路径时统一补上 /v1。"""
        base_url = str(value or "https://api.openai.com").strip().rstrip("/")
        parsed = urlparse(base_url)
        segments = [segment for segment in parsed.path.split("/") if segment]
        # 兼容 /v1、/v1beta 等已有 API 版本路径，避免重复拼接。
        version = segments[-1].lower() if segments else ""
        if version != "v1" and not version.startswith("v1beta"):
            base_url = f"{base_url}/v1"
        return base_url

    def __init__(self, settings: dict[str, Any]) -> None:
        self.model = str(settings.get("model", "")).strip()
        self.allow_empty_model = bool(settings.get("allow_empty_model", False))
        self.api_key_env = str(settings.get("api_key_env", "OPENAI_API_KEY")).strip()
        self.api_key = str(settings.get("api_key", "")).strip()
        self.base_url = self.normalize_base_url(settings.get("base_url", "https://api.openai.com"))
        self.protocol = str(settings.get("protocol", "responses")).strip().lower()
        self.request_compatibility = str(settings.get("request_compatibility", "standard")).strip().lower()
        configured_headers = settings.get("extra_headers")
        self.extra_headers = {
            str(name): str(value) for name, value in configured_headers.items()
            if str(name).strip() and str(value).strip()
        } if isinstance(configured_headers, dict) else {}
        self.reasoning_effort = str(settings.get("reasoning_effort", "high")).strip().lower()
        # 这是网络失联保护，不是页面分类的业务时限。兼容旧配置的 180 秒也提升到 600 秒，
        # 防止上游已经完成但本机先断开并重复计费。
        self.timeout_seconds = max(600, int(settings.get("timeout_seconds", 600)))
        if not self.model and not self.allow_empty_model:
            raise ValueError("cloud.model 不能为空")
        if self.protocol not in self.PROTOCOLS:
            raise ValueError("cloud.protocol 必须是 responses、chat_completions 或 anthropic_messages")
        self.provider_name = self.protocol
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
    def _topic_routing_instruction() -> str:
        """统一多小问跨知识点题的专题路由优先级。"""
        return (
            "同一道含多个跨知识点小问的题必须作为整题路由，不能按小问拆分。先比较各知识点对整题主结论、"
            "核心难点和最困难非例行推理的作用；存在明确核心时归入该知识点所属专题。仅在候选核心无法区分轻重时，"
            "才从完整必备知识中选择目录顺序最晚的前置专题；更晚的常规计算、铺垫或辅助代入不得压过明确核心。"
            "专题10之前的非复合题或上述核心并列题按最晚必备知识点路由；从专题10“三角形”起及后续专题的【大题】"
            "仍按最终解题核心突破口路由。"
        )

    @staticmethod
    def _directory_boundary_instruction() -> str:
        """要求模型按工作簿定义的目录边界，而不是按标题联想。"""
        return (
            "directory_catalog 中每个 classification_basis 是工作簿定义的可判定纳入/排除边界。"
            "分类前必须逐项比对同级候选的分类依据，不能只按标题猜测；以题目的主问或决定性条件"
            "直接满足、且能排除其他同级候选的那一条依据确定末级目录。classification_basis 未填写只表示"
            "该目录没有额外文字边界，仍须结合目录名称、层级和题目信息继续分类，不得因此直接返回 review。"
            "若无法得到唯一匹配，才返回 review。"
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
        confidence = float(value)
        if not 0.0 <= confidence <= 1.0:
            raise CloudProviderError("云端模型返回的置信度必须在 0 到 1 之间")
        return confidence

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
                self._directory_boundary_instruction(),
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
        return self._protocol_request("math_question_classification", [prompt], schema)

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
                "先分析，再选专题；不得从 page_scope_hint 直接抄专题。",
                self._topic_routing_instruction(),
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
                "列出完整解题不可缺少的知识点。",
                self._topic_routing_instruction(),
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
        payload = self._decode(self._request("POST", self._protocol_path(), self.build_batch_routing_request(questions, taxonomy, rules)))
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
                "先独立复核 topic_routing 是否符合分阶段规则及同题多小问归属机制：检查是否把辅助或铺垫知识误作核心，或在核心并列时遗漏更晚的必备专题；专题10及后续的【大题】还要检查是否把辅助步骤误作核心考点。若发现应改到其他专题，必须设置 self_check_passed=false、status=review、reroute_topic_id=修正专题，并将目录目标留空；服务端会加载修正专题的详细目录后重新分类。",
                self._topic_routing_instruction(),
                "自检通过后，只在已路由专题内按首要数学对象确定三级、按主问或决定性条件确定四级。",
                "directory_catalog 仅是当前已路由专题的详细目录，不代表完整专题目录；不得据此声称系统缺少其他专题目录。",
                "当前题属于【大题】时，directory_catalog 已限定为该专题的【大题】二级目录；只能在其三级、四级目录中选择。" if is_large_question else "当前题不限定为【大题】；按 directory_catalog 选择可用目录。",
                "显式符号与目录的含/不含语义冲突、候选不唯一或信息不足时必须返回 review。",
                "仅当命中的三级目录有四级子目录时才匹配四级；没有四级子目录时三级即末级，target_level4_id 必须为 null。",
                "input_warnings 仅提示输入风险；答案编号与题干不一致时，须依据数学连续性判断是否属于同一道题，不得直接丢弃答案。",
                "新目录只能作为候选，单道题不得创建新目录；reason 仅保留短句。",
                self._directory_boundary_instruction(),
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
        rows = self._batch_rows(self._decode(self._request("POST", self._protocol_path(), request)), questions, "专题内分类")
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
                "每题先列出完整必备知识点，再按分阶段规则选专题。",
                self._topic_routing_instruction(),
                "page_scope_hint 和 site_context 只作弱提示；与实际题目冲突时必须忽略。",
                "在选定专题内，按首要数学对象确定三级，按主问或决定性条件确定四级。",
                "逐题检查显式符号与目录的含/不含语义；无法唯一命中时返回 review。",
                "仅对存在四级子目录的三级目录执行四级唯一匹配；无四级子目录时三级即末级，不得作为复核理由。",
                "input_warnings 表示排版风险；答案编号与题干不一致时，须依据数学连续性判断是否属于同一道题，不得直接丢弃答案。",
                "实际题干是分类主依据；题干公式缺失但答案能还原关键关系时，可使用对应答案完成分类；答案明确属于独立题目时才忽略该段。",
                "只返回 JSON；知识点和说明使用短语，reason 限一短句，不输出推理过程。",
                self._directory_boundary_instruction(),
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
        payload = self._decode(self._request("POST", self._protocol_path(), request))
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

    def _structured_request(
        self, name: str, prompt: dict[str, Any], schema: dict[str, Any], reasoning_effort: str | None = None
    ) -> dict[str, Any]:
        return self._protocol_request(name, [prompt], schema, reasoning_effort=reasoning_effort)

    def _staged_structured_request(
        self, name: str, static_context: dict[str, Any], dynamic_context: dict[str, Any], schema: dict[str, Any],
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """把固定 Skill/目录和动态题目拆成连续消息，便于兼容网关复用共同前缀。"""
        return self._protocol_request(name, [static_context, dynamic_context], schema, reasoning_effort=reasoning_effort)

    def _protocol_request(
        self, name: str, contexts: list[dict[str, Any]], schema: dict[str, Any], reasoning_effort: str | None = None
    ) -> dict[str, Any]:
        """将统一的分类请求转换为所选协议的结构化输出请求。"""
        encoded_contexts = [json.dumps(context, ensure_ascii=False, separators=(",", ":")) for context in contexts]
        effort = reasoning_effort or self.reasoning_effort
        if self.protocol == "responses":
            return {
                "model": self.model,
                "instructions": self.SYSTEM_INSTRUCTION,
                "input": [{"role": "user", "content": [{"type": "input_text", "text": context}]} for context in encoded_contexts],
                "text": {"format": {"type": "json_schema", "name": name, "strict": True, "schema": schema}},
                "reasoning": {"effort": effort},
            }
        if self.protocol == "chat_completions":
            # 部分 OpenAI 兼容网关只实现 json_object，不支持 json_schema。此时把
            # Schema 明确放入提示词，仍让模型知道完整的输出契约；本地继续解析 JSON。
            encoded_contexts.append(json.dumps({
                "required_output": "Return exactly one JSON object conforming to this JSON Schema.",
                "json_schema": schema,
            }, ensure_ascii=False, separators=(",", ":")))
            return {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self.SYSTEM_INSTRUCTION},
                    *[{"role": "user", "content": context} for context in encoded_contexts],
                ],
                "response_format": {"type": "json_object"},
                "reasoning_effort": effort,
            }
        # Claude 的 Structured Outputs 只接受 JSON Schema 子集。传输层移除
        # 不支持的约束，但本地保留并校验原始业务 schema，不能借此放宽规则。
        return {
            "model": self.model,
            "max_tokens": 4096,
            "system": self.SYSTEM_INSTRUCTION,
            "messages": [{"role": "user", "content": context} for context in encoded_contexts],
            "output_config": {"format": {"type": "json_schema", "schema": self._claude_output_schema(schema)}},
        }

    @staticmethod
    def _claude_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
        """编译 Claude 可接受的 schema，同时保留完整业务 schema 供本地验证。"""
        transformed = deepcopy(schema)
        unsupported_constraints = {
            "minimum": "Must be greater than or equal to {value}.",
            "maximum": "Must be less than or equal to {value}.",
            "multipleOf": "Must be a multiple of {value}.",
            "minLength": "Must contain at least {value} characters.",
            "maxLength": "Must contain at most {value} characters.",
            "maxItems": "Must contain at most {value} items.",
        }

        def transform(node: Any) -> None:
            if isinstance(node, list):
                for item in node:
                    transform(item)
                return
            if not isinstance(node, dict):
                return
            hints = []
            for key, template in unsupported_constraints.items():
                if key in node:
                    hints.append(template.format(value=node.pop(key)))
            # Claude 只支持值为 0 或 1 的 minItems；其余下放至本地验证。
            if "minItems" in node and node["minItems"] not in {0, 1}:
                hints.append(f"Must contain at least {node.pop('minItems')} items.")
            if hints:
                description = str(node.get("description", "")).strip()
                node["description"] = " ".join([*([description] if description else []), *hints])
            for value in node.values():
                transform(value)

        transform(transformed)
        return transformed

    def _protocol_path(self) -> str:
        return {
            "responses": "/responses",
            "chat_completions": "/chat/completions",
            "anthropic_messages": "/messages",
        }[self.protocol]

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
        routing = self._decode(self._request("POST", self._protocol_path(), self.build_routing_request(question, taxonomy, rules)))
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

    @staticmethod
    def _listed_models(payload: dict[str, Any]) -> list[str]:
        """从 OpenAI/Claude 及兼容网关的模型目录中提取可选模型名。"""
        rows = payload.get("data")
        if not isinstance(rows, list):
            rows = payload.get("models")
        if not isinstance(rows, list):
            return []
        models: list[str] = []
        seen: set[str] = set()
        for row in rows:
            value = row if isinstance(row, str) else (
                row.get("id") or row.get("model") or row.get("name") if isinstance(row, dict) else ""
            )
            model = str(value or "").strip()
            if model and model not in seen:
                seen.add(model)
                models.append(model)
        return models[:500]

    def test_connection(self) -> dict[str, Any]:
        """只读取模型目录验证连接；不携带模型名，也不触发模型生成。"""
        try:
            # OpenAI 兼容接口和 Claude 原生接口均提供模型目录；它只验证地址和认证。
            catalog = self._request("GET", "/models", None, timeout_seconds=10)
            return {
                "protocol": self.protocol, "models": self._listed_models(catalog), "check": "model_catalog",
                "message": "可连接",
            }
        except CloudProviderError as error:
            # 有些兼容网关不实现 Models API。404/405 仍说明接口可达；前端不必
            # 打扰用户说明这个兼容差异，诊断信息则保留在服务端调用链中。
            if "HTTP 401" in str(error) or "HTTP 403" in str(error):
                raise
            if "HTTP 404" in str(error) or "HTTP 405" in str(error):
                return {
                    "protocol": self.protocol, "models": [], "check": "endpoint_reachable",
                    "message": "可连接",
                }
            raise

    def create_batch_jsonl(self, questions: list[dict[str, Any]], taxonomy: Any, rules: dict[str, Any]) -> str:
        """生成本地批任务台账。

        OpenAI 系协议每行是其 Batch JSONL 请求；Claude 每行是 Message Batch 的
        ``{custom_id, params}`` 请求。两者都留为 JSONL，只是提交时由协议适配器转成
        供应商所需的载荷。
        """
        endpoint = "/v1/responses" if self.protocol == "responses" else "/v1/chat/completions"
        lines: list[str] = []
        for question in questions:
            # Provider Batch 无法在同一个作业内串联三次依赖调用，因此给每题完整候选；
            # 导入后仍会经过相同的服务端语义冲突校验。
            candidates = taxonomy.all_targets()
            if not candidates:
                raise ValueError(f"题目 {question.get('exercise_id')} 没有可用目录")
            request = self.build_request(question, candidates, rules)
            custom_id = f"exercise-{question['exercise_id']}-{uuid.uuid4().hex[:10]}"
            if self.protocol == "anthropic_messages":
                lines.append(json.dumps({"custom_id": custom_id, "params": request}, ensure_ascii=False, separators=(",", ":")))
            else:
                lines.append(json.dumps({
                    "custom_id": custom_id, "method": "POST", "url": endpoint, "body": request,
                }, ensure_ascii=False, separators=(",", ":")))
        return "\n".join(lines) + ("\n" if lines else "")

    def submit_batch(self, jsonl_bytes: bytes) -> dict[str, Any]:
        if self.protocol == "anthropic_messages":
            try:
                requests = [json.loads(line) for line in jsonl_bytes.decode("utf-8").splitlines() if line.strip()]
            except (UnicodeError, json.JSONDecodeError) as error:
                raise CloudProviderError("Claude 批处理本地任务文件无效") from error
            if not requests or any(not isinstance(item, dict) or not isinstance(item.get("params"), dict) for item in requests):
                raise CloudProviderError("Claude 批处理本地任务内容无效")
            return self._normalize_anthropic_batch(self._request("POST", "/messages/batches", {"requests": requests}))
        boundary = f"----wulou{uuid.uuid4().hex}"
        chunks = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\nbatch\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"classification.jsonl\"\r\nContent-Type: application/jsonl\r\n\r\n".encode(),
            jsonl_bytes, f"\r\n--{boundary}--\r\n".encode(),
        ]
        file_info = self._request("POST", "/files", b"".join(chunks), {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        endpoint = "/v1/responses" if self.protocol == "responses" else "/v1/chat/completions"
        return self._request("POST", "/batches", {"input_file_id": file_info["id"], "endpoint": endpoint, "completion_window": "24h"})

    def get_batch(self, provider_batch_id: str) -> dict[str, Any]:
        if self.protocol == "anthropic_messages":
            return self._normalize_anthropic_batch(self._request("GET", f"/messages/batches/{provider_batch_id}"))
        return self._request("GET", f"/batches/{provider_batch_id}")

    def get_file_content(self, file_id: str) -> bytes:
        return self._request("GET", f"/files/{file_id}/content", raw=True)

    def get_batch_result_content(self, provider_batch: dict[str, Any]) -> bytes:
        """下载已完成批任务的原始 JSONL；差异仅保留在协议边界。"""
        if self.protocol != "anthropic_messages":
            output_file_id = str(provider_batch.get("output_file_id", "")).strip()
            if not output_file_id:
                raise CloudProviderError("OpenAI 批处理未提供结果文件")
            return self.get_file_content(output_file_id)
        results_url = str(provider_batch.get("results_url", "")).strip()
        if not results_url:
            raise CloudProviderError("Claude 批处理未提供结果地址")
        return self._request("GET", self._anthropic_result_path(results_url), raw=True)

    def batch_result_decision(self, record: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        """从供应商 JSONL 的一行提取统一分类结果；失败项返回 None。"""
        custom_id = str(record.get("custom_id", "")).strip()
        if not custom_id:
            return "", None
        if self.protocol == "anthropic_messages":
            result = record.get("result")
            if not isinstance(result, dict) or result.get("type") != "succeeded":
                return custom_id, None
            message = result.get("message")
            return custom_id, self._decode(message) if isinstance(message, dict) else None
        response = record.get("response")
        body = response.get("body") if isinstance(response, dict) else None
        status_code = response.get("status_code") if isinstance(response, dict) else 0
        if not isinstance(body, dict) or int(status_code or 0) >= 300:
            return custom_id, None
        return custom_id, self._decode(body)

    @staticmethod
    def _normalize_anthropic_batch(batch: dict[str, Any]) -> dict[str, Any]:
        """把 Claude 的 processing_status 映射为本地台账已有的 status 语义。"""
        status = str(batch.get("processing_status", "unknown"))
        normalized = {"ended": "completed", "in_progress": "in_progress", "canceling": "cancelling"}.get(status, status)
        return {**batch, "status": normalized}

    def _anthropic_result_path(self, results_url: str) -> str:
        """将 Claude 返回的相对/绝对 results_url 安全转成当前 base_url 下的路径。"""
        parsed = urlparse(results_url)
        base = urlparse(self.base_url)
        if parsed.scheme and (parsed.scheme != base.scheme or parsed.netloc != base.netloc):
            raise CloudProviderError("Claude 批处理结果地址与当前接口地址不一致")
        path = parsed.path or results_url
        base_path = base.path.rstrip("/")
        if base_path and path.startswith(f"{base_path}/"):
            path = path[len(base_path):]
        if not path.startswith("/"):
            path = f"/{path}"
        return path

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
        if self.protocol == "anthropic_messages":
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        if body is not None and not isinstance(payload, bytes): headers["Content-Type"] = "application/json"
        if extra_headers: headers.update(extra_headers)
        headers.update(self._compatibility_headers())
        request = Request(self._request_url(path), data=body, method=method, headers=headers)
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

    def _compatibility_headers(self) -> dict[str, str]:
        """按当前模型方案应用可复用的请求兼容预设和自定义头。"""
        presets = {
            "standard": {},
            "go_http": {"User-Agent": "Go-http-client/1.1"},
        }
        return {**presets.get(self.request_compatibility, {}), **self.extra_headers}

    def _request_url(self, path: str) -> str:
        """把接口路径拼到已标准化的根地址，避免已有版本前缀重复。"""
        request_path = path if path.startswith("/") else f"/{path}"
        version = urlparse(self.base_url).path.rstrip("/").rsplit("/", 1)[-1]
        if version and (
            request_path == f"/{version}"
            or request_path.startswith(f"/{version}/")
        ):
            request_path = request_path[len(version) + 1:] or "/"
        return self.base_url + request_path

    def _decode(self, response: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise CloudProviderError("云端接口响应必须是 JSON 对象")
        if self.protocol == "chat_completions":
            choices = response.get("choices") or []
            message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                content = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
            return self._decode_json_result(content, "Chat Completions")
        if self.protocol == "anthropic_messages":
            for item in response.get("content") or []:
                if isinstance(item, dict) and item.get("type") == "tool_use" and isinstance(item.get("input"), dict):
                    return item["input"]
            text = "".join(str(item.get("text", "")) for item in response.get("content") or [] if isinstance(item, dict) and item.get("type") == "text")
            return self._decode_json_result(
                text,
                "Claude Messages",
                self._claude_response_diagnostic(response),
            )
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
            return self._decode_json_result(content, "Responses")
        raise CloudProviderError("云端 Responses 未返回可解析的结构化结果")

    @staticmethod
    def _claude_response_diagnostic(response: dict[str, Any]) -> str:
        """生成不含题目、回答或密钥的 Claude 响应形态摘要，供兼容问题定位。"""
        raw_content = response.get("content")
        if not isinstance(raw_content, list):
            content_summary = f"content={type(raw_content).__name__}"
        else:
            blocks = []
            for item in raw_content:
                if not isinstance(item, dict):
                    blocks.append(type(item).__name__)
                    continue
                block_type = str(item.get("type", "unknown"))[:40]
                text_length = len(item["text"]) if isinstance(item.get("text"), str) else None
                blocks.append(f"{block_type}({text_length if text_length is not None else '-'})")
            content_summary = f"content=[{','.join(blocks)}]"
        stop_reason = str(response.get("stop_reason", "missing"))[:80]
        return f"stop_reason={stop_reason}; {content_summary}"

    @staticmethod
    def _decode_json_result(content: Any, protocol_name: str, diagnostic: str = "") -> dict[str, Any]:
        if not isinstance(content, str) or not content.strip():
            suffix = f"（{diagnostic}）" if diagnostic else ""
            raise CloudProviderError(f"云端 {protocol_name} 未返回可解析的结构化结果{suffix}")
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError as error:
            raise CloudProviderError("云端模型返回的分类结果不是有效 JSON") from error
        if isinstance(decoded, dict):
            return decoded
        raise CloudProviderError("云端模型返回的分类结果必须是 JSON 对象")

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


# 兼容已保存的本地 Python 导入路径；实际请求协议由模型方案中的 protocol 决定。
OpenAIResponsesProvider = OpenAIChatCompletionsProvider
