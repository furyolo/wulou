"""保守的目录分类器。

模型尚未配置时只根据目录中的显式关键词提出建议。无法唯一命中时必须复核，
避免把题目静默写入错误目录。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict
from typing import Any

try:
    from .taxonomy import Target, Taxonomy
except ImportError:  # 支持直接运行 python server/main.py。
    from taxonomy import Target, Taxonomy


def normalize_text(value: str) -> str:
    value = value.replace("\u3000", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def content_hash(question: dict[str, Any]) -> str:
    """生成题目本体指纹，排除题湖页面中会随渲染变化的辅助文本。"""
    material = "\n".join([
        str(question.get("exercise_id", "")),
        normalize_text(str(question.get("question_press", ""))),
        normalize_text(str(question.get("answer_press", ""))),
        normalize_text(str(question.get("question_image_url", ""))),
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _score(text: str, target: Target) -> int:
    if any(keyword and keyword in text for keyword in target.exclude_keywords):
        return -1
    return sum(1 for keyword in target.include_keywords if keyword and keyword in text)


def classify(question: dict[str, Any], taxonomy: Taxonomy, rules: dict[str, Any]) -> dict[str, Any]:
    exercise_id = str(question.get("exercise_id", "")).strip()
    if not exercise_id:
        raise ValueError("exercise_id 不能为空")
    source_text = normalize_text(f"{question.get('question_press', '')}\n{question.get('answer_press', '')}")
    scope = question.get("scope") or {}
    candidates = taxonomy.candidates(scope.get("topic_id"), scope.get("level2_id"))
    if not candidates:
        return _review_result(exercise_id, taxonomy, "当前分类范围没有可用目录", "scope_empty")

    scored = [(target, _score(source_text, target)) for target in candidates]
    scored = [(target, score) for target, score in scored if score >= 0]
    best_score = max((score for _, score in scored), default=0)
    winners = [target for target, score in scored if score == best_score and score > 0]

    if len(winners) != 1:
        reason = "现有目录无法唯一命中，需人工复核"
        code = "no_unique_target" if winners else "no_compatible_target"
        return _review_result(exercise_id, taxonomy, reason, code, proposal_required=not winners)

    target = winners[0]
    if target.level4_id and target.level3_knowledge_point_id:
        return _review_result(
            exercise_id, taxonomy, "父三级目录已有知识点编号，不能临时细分四级目录", "parent_knowledge_point_protection"
        )

    result = {
        "exercise_id": exercise_id,
        "taxonomy_version": taxonomy.version,
        "rule_version": str(rules.get("rule_version", "")),
        "status": "review",
        "needs_review": True,
        "review_reasons": ["cloud_model_required_for_skill_protocol"],
        "classification_method": "heuristic",
        "confidence": 0.0,
        "reason": "规则关键词只能提供候选，未执行专题路由和语义审核",
        "target": _target_payload(target),
        "proposal_required": False,
        "proposal_cluster_id": None,
    }
    return result


def validate_model_decision(question: dict[str, Any], decision: dict[str, Any], taxonomy: Taxonomy, rules: dict[str, Any]) -> dict[str, Any]:
    """把模型 JSON 约束回本项目目录和强制复核规则。"""
    exercise_id = str(question.get("exercise_id", "")).strip()
    if not isinstance(decision, dict):
        return _review_result(exercise_id, taxonomy, "模型结果不是 JSON 对象", "invalid_model_payload")
    level3_id, level4_id = decision.get("target_level3_id"), decision.get("target_level4_id")
    # 网页目录只作提示，模型目标必须在完整目录中校验。
    target = taxonomy.global_target(str(level3_id), level4_id) if level3_id else None
    status = str(decision.get("status", "review"))
    try:
        confidence = float(decision.get("confidence", 0))
    except (TypeError, ValueError):
        return _review_result(exercise_id, taxonomy, "模型置信度格式无效", "invalid_model_confidence")
    if not 0.0 <= confidence <= 1.0:
        return _review_result(exercise_id, taxonomy, "模型置信度必须在 0 到 1 之间", "invalid_model_confidence")
    raw_review_reasons = decision.get("review_reasons")
    if raw_review_reasons is not None and not isinstance(raw_review_reasons, list):
        return _review_result(exercise_id, taxonomy, "模型复核原因格式无效", "invalid_model_review_reasons")
    review_reasons = [str(item) for item in (raw_review_reasons or []) if item]
    if status == "suggested" and not target:
        return _review_result(exercise_id, taxonomy, "模型返回了不存在的目录目标", "invalid_model_target")
    routing = decision.get("routing") if isinstance(decision.get("routing"), dict) else None
    if routing:
        routing = dict(routing)
        routed_topic = taxonomy.topic(str(routing.get("latest_topic_id")))
        routing["routed_topic_title"] = routed_topic["title"] if routed_topic else None
        # 兼容已缓存结果和旧版油猴脚本；新界面使用 routed_topic_title。
        routing["latest_topic_title"] = routing["routed_topic_title"]
    if target and routing and routing.get("latest_topic_id") != target.topic_id:
        return _review_result(exercise_id, taxonomy, "专题路由与最终目录不一致", "routing_target_mismatch")
    if target and target.level4_id and target.level3_knowledge_point_id:
        return _review_result(exercise_id, taxonomy, "父三级目录已有知识点编号，不能临时细分四级目录", "parent_knowledge_point_protection")
    proposal = decision.get("proposal") or {}
    if not isinstance(proposal, dict):
        return _review_result(exercise_id, taxonomy, "模型目录候选格式无效", "invalid_model_proposal")
    proposal_kind = proposal.get("kind", "none")
    proposal_required = proposal_kind in {"new_level3", "new_level4"}
    # 三级目录没有四级子节点时，三级本身就是 Skill 所说的唯一末级锚点。
    # 部分模型会把“需要唯一末级”误解为“必须存在四级”，在无其他疑点时予以纠正。
    terminal_level3_only = target is not None and target.level4_id is None
    terminal_level3_reasons = [item for item in review_reasons if _is_terminal_level3_review_reason(item)]
    if terminal_level3_only and terminal_level3_reasons:
        review_reasons = [item for item in review_reasons if item not in terminal_level3_reasons]
        if status == "review" and not review_reasons:
            status = "suggested"
    if status != "suggested":
        review_reasons = review_reasons or ["model_review"]
    scope = question.get("scope") or {}
    if target and scope.get("topic_id") and scope.get("topic_id") != target.topic_id:
        review_reasons.append("page_scope_differs_from_routed_topic")
    auto_threshold = float((rules.get("model", {}) or {}).get("auto_accept_min_confidence", 0.92))
    review_reasons = list(dict.fromkeys(review_reasons))
    needs_review = status != "suggested" or confidence < auto_threshold or proposal_required or bool(review_reasons)
    result = {
        "exercise_id": exercise_id, "taxonomy_version": taxonomy.version, "rule_version": str(rules.get("rule_version", "")),
        "status": "suggested" if status == "suggested" and target else "review", "needs_review": needs_review,
        "review_reasons": review_reasons + (["low_confidence"] if status == "suggested" and confidence < auto_threshold else []),
        "classification_method": "model", "confidence": confidence,
        "reason": normalize_text(str(decision.get("reason", "云端模型未提供依据")))[:500],
        "target": _target_payload(target) if target else None, "proposal_required": proposal_required,
        "proposal_cluster_id": str(proposal.get("cluster_key")) if proposal_required and proposal.get("cluster_key") else None,
        "proposal": proposal if proposal_required else None,
        "routing": routing,
    }
    return result


def _is_terminal_level3_review_reason(value: str) -> bool:
    """识别模型把“无四级目录”误报为异常的中文说明。"""
    text = normalize_text(value)
    lacks_level4 = "四级" in text and "目录" in text and any(marker in text for marker in ("未提供", "没有", "无"))
    return lacks_level4 and any(marker in text for marker in ("唯一", "匹配"))


def _review_result(
    exercise_id: str, taxonomy: Taxonomy, reason: str, code: str, proposal_required: bool = False
) -> dict[str, Any]:
    return {
        "exercise_id": exercise_id,
        "taxonomy_version": taxonomy.version,
        "status": "review",
        "needs_review": True,
        "review_reasons": [code],
        "classification_method": "heuristic",
        "confidence": 0.0,
        "reason": reason,
        "target": None,
        "proposal_required": proposal_required,
        "proposal_cluster_id": f"candidate:{exercise_id}" if proposal_required else None,
    }


def _target_payload(target: Target) -> dict[str, Any]:
    data = asdict(target)
    data["path"] = target.published_path
    data.pop("include_keywords", None)
    data.pop("exclude_keywords", None)
    return data
