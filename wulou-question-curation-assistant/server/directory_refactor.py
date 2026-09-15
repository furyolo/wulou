"""目录焦点重构方案的输入、语义边界和数量审核。

该模块只生成待审核方案，不写入题湖或 Excel。Excel 写入必须另行经过
``excel_sync.write_approved_refactor`` 的范围和基准哈希校验。
"""

from __future__ import annotations

from typing import Any


MIN_LEVEL4_QUESTION_COUNT = 6
# 抽样只用于生成待审核的目录候选，不能替代全量题量核验。三道样本题可
# 证明该考法并非单题偶发；四道及以上在界面标为“强候选”。
SAMPLED_LEVEL4_CANDIDATE_MIN_COUNT = 3
SAMPLED_LEVEL4_STRONG_CANDIDATE_MIN_COUNT = 4


class DirectoryRefactorError(ValueError):
    """目录重构输入或模型方案不满足安全边界。"""


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


def validate_refactor_plan(context: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """验证粗粒度目录方案；抽样只生成候选，全量方案才以六题作为硬门槛。"""
    if not isinstance(plan, dict):
        raise DirectoryRefactorError("目录重构模型没有返回对象")
    level3_rows = plan.get("level3")
    if not isinstance(level3_rows, list) or not level3_rows:
        raise DirectoryRefactorError("目录重构方案没有三级目录")

    is_sampled = context["collection"]["sampling"]["mode"] == "stratified_page"
    minimum_support_count = (
        context["sampled_level4_candidate_min_count"]
        if is_sampled
        else context["minimum_level4_question_count"]
    )
    maximum_support_count = context["minimum_level4_question_count"]
    level3_by_key: dict[str, dict[str, Any]] = {}
    level4_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in level3_rows:
        if not isinstance(row, dict):
            raise DirectoryRefactorError("三级目录行格式无效")
        key = str(row.get("key", "")).strip()
        title = str(row.get("title", "")).strip()
        basis = str(row.get("basis", "")).strip()
        if not key or not title or not basis or key in level3_by_key:
            raise DirectoryRefactorError("三级目录 key 或标题无效、重复")
        level4 = row.get("level4") or []
        if not isinstance(level4, list):
            raise DirectoryRefactorError("四级目录必须是数组")
        level3_by_key[key] = {"key": key, "title": title, "basis": basis, "level4": []}
        for item in level4:
            if not isinstance(item, dict):
                raise DirectoryRefactorError("四级目录行格式无效")
            level4_key = str(item.get("key", "")).strip()
            level4_title = str(item.get("title", "")).strip()
            level4_basis = str(item.get("basis", "")).strip()
            if not level4_key or not level4_title.startswith("考法") or not level4_basis:
                raise DirectoryRefactorError("四级目录必须使用唯一 key 且标题以“考法”开头")
            identity = (key, level4_key)
            if identity in level4_by_key:
                raise DirectoryRefactorError("同一三级目录下四级 key 重复")
            supporting_ids = item.get("supporting_exercise_ids")
            if not isinstance(supporting_ids, list):
                raise DirectoryRefactorError("四级目录必须提供用于题量核验的匹配题号")
            normalized_supporting_ids = [str(exercise_id).strip() for exercise_id in supporting_ids]
            if not minimum_support_count <= len(normalized_supporting_ids) <= maximum_support_count:
                expected = (
                    f"{minimum_support_count} 至 {maximum_support_count}"
                    if is_sampled else str(minimum_support_count)
                )
                raise DirectoryRefactorError(
                    f"四级目录必须提供 {expected} 个匹配题号用于题量核验"
                )
            if any(not exercise_id for exercise_id in normalized_supporting_ids) or len(set(normalized_supporting_ids)) != len(normalized_supporting_ids):
                raise DirectoryRefactorError("四级目录题量核验题号无效或重复")
            normalized = {
                "key": level4_key, "title": level4_title, "basis": level4_basis,
                "supporting_exercise_ids": normalized_supporting_ids,
            }
            level3_by_key[key]["level4"].append(normalized)
            level4_by_key[identity] = normalized

    if context["focus"]["level"] == 3 and len(level3_by_key) != 1:
        raise DirectoryRefactorError("三级 Focus 不允许新建、删除或移动三级目录")
    if context["focus"]["level"] == 3:
        source_level3 = context["selected_level3"][0]
        only_level3 = next(iter(level3_by_key.values()))
        if only_level3["key"] != str(source_level3["id"]) or only_level3["title"] != str(source_level3["title"]):
            raise DirectoryRefactorError("三级 Focus 只能产出其下四级目录，不能修改三级目录")
    if context["focus"]["level"] == 3 and len(context["questions"]) >= minimum_support_count:
        only_level3 = next(iter(level3_by_key.values()))
        if not only_level3["level4"]:
            raise DirectoryRefactorError("三级 Focus 有足够题量时必须给出四级目录分类")
    question_ids = {str(item["exercise_id"]) for item in context["questions"]}
    used_supporting_ids: set[str] = set()
    level4_counts: dict[tuple[str, str], int] = {}
    for identity, item in level4_by_key.items():
        supporting_ids = set(item["supporting_exercise_ids"])
        if not supporting_ids <= question_ids:
            raise DirectoryRefactorError(f"四级目录 {item['title']} 引用了 Focus 范围外的题目")
        if used_supporting_ids & supporting_ids:
            raise DirectoryRefactorError("不同四级目录的题量核验题号不能重复")
        used_supporting_ids.update(supporting_ids)
        level4_counts[identity] = len(supporting_ids)

    # 前端目录方案只生成待审核候选，不执行编号承接、题目迁移或 Excel 写入。
    # 因而不能在已有编号的三级下自行新增四级；该完整重构由目录整理 Skill 的
    # 正式写入流程负责，并需在同一事务中承接原编号。
    selected_by_id = {str(item["id"]): item for item in context["selected_level3"]}
    if context["focus"]["level"] == 3:
        source = selected_by_id[context["focus"]["level3_id"]]
        if source.get("knowledge_point_id") and next(iter(level3_by_key.values()))["level4"]:
            raise DirectoryRefactorError("父三级已有知识点编号，需先单独完成承接四级目录重构")

    return {
        "status": "review",
        "focus": context["focus"],
        "minimum_level4_question_count": context["minimum_level4_question_count"],
        "sampled_level4_candidate_min_count": context["sampled_level4_candidate_min_count"],
        "sampled_level4_strong_candidate_min_count": context["sampled_level4_strong_candidate_min_count"],
        "old_directory_tree": context["selected_level3"],
        "reference_directory_tree": context["reference_directory_tree"],
        "level3": list(level3_by_key.values()),
        "audit": {
            "passed": True,
            "question_count": len(question_ids),
            "supporting_question_count": len(used_supporting_ids),
            "classification_scope": "directory_outline_only",
            "coverage_complete": (
                context["collection"]["sampling"]["mode"] == "full"
                and not context["collection"]["failed_exercise_ids"]
                and not context["collection"]["failed_page_urls"]
                and not context["collection"].get("failed_signal_exercise_ids")
            ),
            "level4_counts": [
                {
                    "level3_key": key[0], "level4_key": key[1], "count": count,
                    "evidence_tier": (
                        "strong_candidate" if is_sampled and count >= context["sampled_level4_strong_candidate_min_count"]
                        else "candidate" if is_sampled
                        else "verified"
                    ),
                }
                for key, count in sorted(level4_counts.items())
            ],
        },
        "collection": context["collection"],
        "model_notes": [str(item) for item in (plan.get("notes") or []) if str(item).strip()],
    }
