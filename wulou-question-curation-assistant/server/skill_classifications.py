"""导入目录整理 Skill 的逐题归类结果。

``math-exam-directory-curation`` Skill 运行在外部会话里：它拿不到本机回环地址，
也无法预知本机目录 ID——本地 ID 是由 Excel 行号推导出来的（``l3-107-...``），
插入或删除一行就会整体改变。因此两侧只用一件事对齐：Excel E 列的知识点编号。
表格里写着的编号，Skill 读得到，本机也读得到。

Skill 交回「题号 + 知识点编号」清单，本模块按当前 taxonomy 反查目录、逐条校验，
再由 ``ResultCache`` 写入人工修正表并标记 ``source='skill'``。前端原有的
``/api/v1/cache/classifications/lookup`` 链路随即就能展示这些分类建议，
无需为导入再开一条读接口。
"""

from __future__ import annotations

import json
from typing import Any

try:
    from .taxonomy import Target, Taxonomy
except ImportError:  # 支持直接运行 python server/main.py。
    from taxonomy import Target, Taxonomy


IMPORT_SCHEMA_VERSION = "skill-classification-import-v1"
MAX_IMPORT_ITEMS = 20_000
MAX_IDENTIFIER_LENGTH = 120
MAX_PATH_DEPTH = 8
PREVIEW_LIMIT = 20
UNCATEGORIZED_SOURCE = "__uncategorized__"


class SkillImportError(ValueError):
    """导入内容整体不可用，尚未进入逐条校验。"""


def parse_import_text(text: str) -> tuple[list[dict[str, Any]], str]:
    """解析 Skill 输出的 JSON 对象、JSON 数组或 JSONL 文本。

    返回 ``(条目列表, 文件声明的目录版本)``。容错是必要的：Skill 每次的输出
    格式未必相同，而把一份上千行的清单退回重做代价很高。
    """
    stripped = str(text or "").strip()
    if not stripped:
        raise SkillImportError("导入内容为空")
    if stripped[0] == "[":
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError as error:
            raise SkillImportError(f"导入内容不是合法 JSON：{error.msg}") from error
        return _coerce_items(decoded), ""
    if stripped[0] == "{":
        try:
            decoded: Any = json.loads(stripped)
        except json.JSONDecodeError:
            # 多行 JSONL 也会以 "{" 开头；交给下面的逐行解析。
            decoded = None
        if isinstance(decoded, dict):
            version = str(decoded.get("taxonomy_version", "")).strip()
            if "items" in decoded:
                return _coerce_items(decoded.get("items")), version
            if "exercise_id" in decoded:
                return [dict(decoded)], version
            raise SkillImportError("导入对象既没有 items 数组，也不是单条归类结果")
    items: list[dict[str, Any]] = []
    for number, line in enumerate(stripped.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise SkillImportError(f"第 {number} 行不是合法 JSON：{error.msg}") from error
        if not isinstance(row, dict):
            raise SkillImportError(f"第 {number} 行不是 JSON 对象")
        # 兼容「首行是清单头、其余行是归类结果」的 JSONL 写法。
        if "items" in row and "exercise_id" not in row:
            return _coerce_items(row.get("items")), str(row.get("taxonomy_version", "")).strip()
        items.append(row)
    return _coerce_items(items), ""


def _coerce_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise SkillImportError("导入内容必须包含非空条目数组")
    if len(value) > MAX_IMPORT_ITEMS:
        raise SkillImportError(f"单次最多导入 {MAX_IMPORT_ITEMS} 条归类结果")
    items: list[dict[str, Any]] = []
    for index, row in enumerate(value, start=1):
        if not isinstance(row, dict):
            raise SkillImportError(f"第 {index} 条归类结果不是 JSON 对象")
        items.append(row)
    return items


def target_path_for(target: Target) -> list[str]:
    """把目录目标还原成标题数组，供前端在题湖目录树中逐段定位。"""
    return target.published_path


def _optional_path(value: Any) -> list[str] | None:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > MAX_PATH_DEPTH:
        return None
    path = [str(item).strip() for item in value]
    if any(not item or len(item) > 160 for item in path):
        return None
    return path


def resolve_skill_items(
    items: list[dict[str, Any]], taxonomy: Taxonomy
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """逐条把原始条目解析成可写入行。

    返回 ``(可写入行, 失败条目, 提示)``。失败逐条汇报而不是整批拒绝：一份上千题
    的清单里出现少数编号对不上很正常，不该让其余题目一起作废。
    """
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen: dict[str, int] = {}
    for index, item in enumerate(items, start=1):
        exercise_id = str(item.get("exercise_id", "")).strip()
        if not exercise_id or len(exercise_id) > MAX_IDENTIFIER_LENGTH:
            failures.append({
                "item": index, "exercise_id": exercise_id,
                "code": "invalid_exercise_id", "message": "题目 ID 为空或过长",
            })
            continue
        if exercise_id in seen:
            failures.append({
                "item": index, "exercise_id": exercise_id, "code": "duplicate_exercise_id",
                "message": f"同一题号在导入文件里出现了多次（另见第 {seen[exercise_id]} 条）",
            })
            continue
        knowledge_point_id = str(item.get("knowledge_point_id", "")).strip()
        target, error = taxonomy.resolve_knowledge_point_id(knowledge_point_id)
        if target is None:
            failures.append({
                "item": index, "exercise_id": exercise_id, "knowledge_point_id": knowledge_point_id,
                "code": "unresolved_knowledge_point_id", "message": error or "知识点编号无法定位目录",
            })
            continue
        source_catalogue_id = str(item.get("current_catalogue_id", "")).strip()
        if len(source_catalogue_id) > MAX_IDENTIFIER_LENGTH:
            failures.append({
                "item": index, "exercise_id": exercise_id,
                "code": "invalid_current_catalogue_id", "message": "当前目录 ID 过长",
            })
            continue
        original_target_path = _optional_path(item.get("original_target_path"))
        if original_target_path is None:
            failures.append({
                "item": index, "exercise_id": exercise_id,
                "code": "invalid_original_target_path", "message": "原目录路径格式无效",
            })
            continue
        seen[exercise_id] = index
        rows.append({
            "exercise_id": exercise_id,
            "source_catalogue_id": source_catalogue_id or UNCATEGORIZED_SOURCE,
            "stable_code": str(item.get("stable_code", "")).strip()[:MAX_IDENTIFIER_LENGTH],
            "knowledge_point_id": knowledge_point_id,
            "original_target_path": original_target_path,
            "target_path": target_path_for(target),
        })
    if rows and len(failures) > len(rows):
        warnings.append("失败条目多于成功条目，请先核对导入文件与当前目录版本是否配套。")
    return rows, failures, warnings


def build_import_plan(payload: dict[str, Any], taxonomy: Taxonomy) -> dict[str, Any]:
    """解析并校验一次导入，产出与是否真正写库无关的完整报告。

    ``dry_run`` 只影响调用方是否执行写入，报告本身在预演和正式导入时一致，
    这样前端可以先预演、再确认。
    """
    if not isinstance(payload, dict):
        raise SkillImportError("导入请求必须是 JSON 对象")
    raw_text = payload.get("jsonl") or payload.get("text")
    if isinstance(raw_text, str) and raw_text.strip():
        items, file_taxonomy_version = parse_import_text(raw_text)
    elif "items" in payload:
        items = _coerce_items(payload.get("items"))
        file_taxonomy_version = str(payload.get("taxonomy_version", "")).strip()
    else:
        raise SkillImportError("导入请求必须包含 items 数组或 jsonl 文本")

    rows, failures, warnings = resolve_skill_items(items, taxonomy)
    if file_taxonomy_version and file_taxonomy_version != taxonomy.version:
        warnings.append(
            f"导入文件基于目录版本 {file_taxonomy_version}，当前版本为 {taxonomy.version}；"
            "已按当前版本的编号重新定位目录。"
        )
    for row in rows:
        row["taxonomy_version"] = taxonomy.version
    return {
        "schema_version": IMPORT_SCHEMA_VERSION,
        "taxonomy_version": taxonomy.version,
        "file_taxonomy_version": file_taxonomy_version,
        "received": len(items),
        "resolved": len(rows),
        "rows": rows,
        "failed": failures,
        "warnings": warnings,
        "preview": [
            {
                "exercise_id": row["exercise_id"],
                "knowledge_point_id": row["knowledge_point_id"],
                "target_path": row["target_path"],
            }
            for row in rows[:PREVIEW_LIMIT]
        ],
    }
