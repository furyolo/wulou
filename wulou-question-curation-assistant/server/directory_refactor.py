"""目录整理的交接输入：锁定焦点范围，并把题库整理成可供外部 Skill 使用的上下文。

本模块不生成目录方案、不写 Excel、也不接触题湖。它只把三件事确定下来——整理哪些题、
参考哪一棵目录树、采集有没有缺口——交给 ``directory_exports`` 打包，由人工在外部会话
里启动的目录整理 Skill 消费；结论再经 ``/api/v1/skill-classifications`` 回到本机。
"""

from __future__ import annotations

from typing import Any


MIN_LEVEL4_QUESTION_COUNT = 6
# 抽样只用于生成待审核的目录候选，不能替代全量题量核验。三道样本题可
# 证明该考法并非单题偶发；四道及以上在界面标为“强候选”。
SAMPLED_LEVEL4_CANDIDATE_MIN_COUNT = 3
SAMPLED_LEVEL4_STRONG_CANDIDATE_MIN_COUNT = 4


class DirectoryRefactorError(ValueError):
    """目录整理的输入不满足安全边界。"""


def prepare_refactor_context(payload: dict[str, Any], taxonomy: Any) -> dict[str, Any]:
    """校验焦点与题目全集，并提取本专题的旧目录和参考目录。"""
    focus = payload.get("focus")
    questions = payload.get("questions")
    if not isinstance(focus, dict) or not isinstance(questions, list) or not questions:
        raise DirectoryRefactorError("目录重构必须包含 focus 和非空 questions")
    level = int(focus.get("level", 0))
    if level not in {2, 3}:
        raise DirectoryRefactorError("Focus 只能是二级或三级目录")
    topic_id = str(focus.get("topic_id", "")).strip()
    level2_id = str(focus.get("level2_id", "")).strip()
    level3_id = str(focus.get("level3_id", "")).strip() or None
    if not topic_id or not level2_id or (level == 3 and not level3_id):
        raise DirectoryRefactorError("Focus 缺少有效的目录标识")

    topic = next((item for item in taxonomy.raw.get("topics", []) if str(item.get("id")) == topic_id), None)
    if not topic:
        raise DirectoryRefactorError("Focus 专题不存在于当前目录版本")
    level2 = next((item for item in topic.get("level2", []) if str(item.get("id")) == level2_id), None)
    if not level2:
        raise DirectoryRefactorError("Focus 二级目录不存在于当前目录版本")
    selected_level3 = [item for item in level2.get("level3", []) if level == 2 or str(item.get("id")) == level3_id]
    if level == 3 and not selected_level3:
        raise DirectoryRefactorError("Focus 三级目录不存在于当前目录版本")

    seen: set[str] = set()
    normalized_questions: list[dict[str, Any]] = []
    for question in questions:
        if not isinstance(question, dict):
            raise DirectoryRefactorError("题目必须是对象")
        exercise_id = str(question.get("exercise_id", "")).strip()
        if not exercise_id or exercise_id in seen:
            raise DirectoryRefactorError("题目 ID 不能为空且不能重复")
        seen.add(exercise_id)
        normalized_questions.append(question)

    raw_collection = payload.get("collection")
    if raw_collection is None:
        collection = {
            "discovered_question_count": len(normalized_questions),
            "collected_question_count": len(normalized_questions),
            "failed_exercise_ids": [],
            "failed_page_urls": [],
            "failed_signal_exercise_ids": [],
            "sampling": {"mode": "full", "source_question_count": len(normalized_questions)},
        }
    elif not isinstance(raw_collection, dict):
        raise DirectoryRefactorError("采集覆盖信息格式无效")
    else:
        try:
            discovered_count = int(raw_collection.get("discovered_question_count", 0))
            collected_count = int(raw_collection.get("collected_question_count", 0))
        except (TypeError, ValueError) as error:
            raise DirectoryRefactorError("采集题量必须是整数") from error
        failed_ids = [str(item).strip() for item in (raw_collection.get("failed_exercise_ids") or [])]
        failed_pages = [str(item).strip() for item in (raw_collection.get("failed_page_urls") or [])]
        sampling = raw_collection.get("sampling") or {"mode": "full", "source_question_count": discovered_count}
        if discovered_count < collected_count or collected_count != len(normalized_questions):
            raise DirectoryRefactorError("采集题量与实际提交题目不一致")
        if any(not item for item in failed_ids) or len(set(failed_ids)) != len(failed_ids):
            raise DirectoryRefactorError("失败题目 ID 无效或重复")
        if set(failed_ids) & seen:
            raise DirectoryRefactorError("失败题目不能同时作为可用题目提交")
        if not isinstance(sampling, dict):
            raise DirectoryRefactorError("抽样信息格式无效")
        sampling_mode = str(sampling.get("mode", "full")).strip()
        try:
            source_question_count = int(sampling.get("source_question_count", discovered_count))
        except (TypeError, ValueError) as error:
            raise DirectoryRefactorError("抽样原始题量必须是整数") from error
        if sampling_mode not in {"full", "stratified_page"} or source_question_count != discovered_count:
            raise DirectoryRefactorError("抽样模式或原始题量无效")
        if sampling_mode == "full" and discovered_count != collected_count + len(failed_ids):
            raise DirectoryRefactorError("全量采集题量与失败题目数量不一致")
        collection = {
            "discovered_question_count": discovered_count,
            "collected_question_count": collected_count,
            "failed_exercise_ids": failed_ids,
            "failed_page_urls": [item for item in failed_pages if item],
            "failed_signal_exercise_ids": [],
            "sampling": {"mode": sampling_mode, "source_question_count": source_question_count},
        }

    # 目录骨架须以整专题的二、三、四级树为参考；当前 Focus 的旧树仍单独返回，避免混淆“待优化对象”和“参考样本”。
    reference_directory_tree = [
        {
            "id": item.get("id"),
            "title": item.get("title"),
            "level3": item.get("level3") or [],
        }
        for item in topic.get("level2", [])
    ]
    return {
        "focus": {"level": level, "topic_id": topic_id, "level2_id": level2_id, "level3_id": level3_id},
        "questions": normalized_questions,
        "selected_level3": selected_level3,
        "reference_directory_tree": reference_directory_tree,
        "collection": collection,
        "minimum_level4_question_count": MIN_LEVEL4_QUESTION_COUNT,
        "sampled_level4_candidate_min_count": SAMPLED_LEVEL4_CANDIDATE_MIN_COUNT,
        "sampled_level4_strong_candidate_min_count": SAMPLED_LEVEL4_STRONG_CANDIDATE_MIN_COUNT,
    }
