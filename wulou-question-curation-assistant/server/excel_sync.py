"""Excel 目录的只读预检和受控新版本写入。

基准工作簿永不覆盖；写入仅接受已人工审核的明确目录方案。

资源约定：openpyxl 的 ``Workbook`` **不支持 with 语句**（3.1.5 实测无 ``__enter__``），
也没有自动关闭机制，必须显式 ``close()``。其中 ``read_only=True`` 打开的句柄会一直占住
文件（Windows 上表现为改不了名、删不掉），所以每个 ``load_workbook`` 都必须配
``try/finally: book.close()``；``read_only=False`` 虽不占文件，也一并关闭以免对象滞留内存。

编号约定：知识点编号是**署名制**，谁建的目录谁定编号，现存 E 列会出现 ZCSQG / ZCSZKH /
ZCSQGLQ / CSZSDCF 等多种前缀，同一前缀下写法也不统一——全都合法。本模块一律按 E 列
**原值等值反查**（``taxonomy.resolve_knowledge_point_id``），**不做任何格式校验**，因为
新增校验会让现在能用的合法编号突然失效。

（原 ``next_sf_number()`` / ``SF_PATTERN`` 已于 2026-09-17 删除：全仓无调用方，
且它是唯一打开 ``read_only=True`` 却遗漏 ``close()`` 的路径。署名制的完整说明见 README。）
"""

from __future__ import annotations

import hashlib
import json
from copy import copy
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from server.taxonomy_export import load_catalogue_workbook


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_workbook(path: Path, topic_title: str) -> dict[str, Any]:
    book = load_catalogue_workbook(path)
    try:
        matches: list[dict[str, Any]] = []
        for sheet in book.worksheets:
            topic_rows = [row for row in range(1, sheet.max_row + 1) if str(sheet.cell(row, 2).value or "").strip() == topic_title]
            for topic_row in topic_rows:
                end = next((row for row in range(topic_row + 1, sheet.max_row + 1) if str(sheet.cell(row, 2).value or "").strip().startswith("专题")), sheet.max_row + 1)
                large_row = next((row for row in range(topic_row + 1, end) if str(sheet.cell(row, 2).value or "").strip() == "【大题】"), None)
                matches.append({"sheet": sheet.title, "topic_row": topic_row, "topic_end_row": end - 1, "large_row": large_row})
        if len(matches) != 1: raise ValueError(f"应唯一找到专题“{topic_title}”，实际找到 {len(matches)} 处")
        sheets = list(book.sheetnames)
    finally:
        book.close()
    return {"baseline": str(path.resolve()), "baseline_sha256": sha256(path), "sheets": sheets, "topic": matches[0]}


def write_approved_plan(baseline: Path, output: Path, plan_path: Path) -> dict[str, Any]:
    """将明确行号和审核状态均已固定的方案写入新文件，并重新打开核验。"""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("status") != "approved": raise ValueError("Excel 方案必须先人工审核为 approved")
    if sha256(baseline) != plan.get("baseline_sha256"): raise ValueError("基准工作簿已变化，拒绝写入")
    if output.exists(): raise ValueError("目标文件已存在，拒绝覆盖")
    book = load_catalogue_workbook(baseline)
    try:
        sheet = book[plan["sheet"]]
        insert_at = int(plan["insert_at_row"]); rows = plan.get("rows") or []
        if not rows: raise ValueError("方案没有待写入目录行")
        sheet.insert_rows(insert_at, amount=len(rows))
        for offset, item in enumerate(rows):
            row = insert_at + offset
            level = item.get("level")
            if level not in {3, 4}: raise ValueError("只允许写入三级或四级目录")
            col = 3 if level == 3 else 4
            sheet.cell(row, col).value = item["title"]
            if item.get("knowledge_point_id"): sheet.cell(row, 5).value = item["knowledge_point_id"]
            if item.get("reason"): sheet.cell(row, 14).value = item["reason"]
            source_row = insert_at - 1 if insert_at > 1 else insert_at + len(rows)
            for source_cell in sheet[source_row]:
                target = sheet.cell(row, source_cell.column); target._style = copy(source_cell._style); target.number_format = source_cell.number_format
        book.save(output)
    finally:
        book.close()
    probe = load_workbook(output, read_only=True)
    probe.close()
    if sha256(baseline) != plan["baseline_sha256"]: raise ValueError("写入后基准工作簿发生变化")
    return {"output": str(output), "baseline_sha256": plan["baseline_sha256"], "inserted_rows": len(rows)}


def _as_catalog_id(value: Any) -> Any:
    """目录 ID 在基准工作簿里是数字。纯数字串按数字写回，避免类型漂移。"""
    text = str(value).strip()
    return int(text) if text.isdigit() else text


def write_approved_refactor(baseline: Path, output: Path, plan_path: Path) -> dict[str, Any]:
    """重建一个已锚定的三级目录范围，并证明范围外单元格未改变。

    方案必须由人工在审核页补全 ``replace_start_row``、``replace_end_row`` 和
    ``rows``。该函数不猜测 Excel 行号，不接受二级容器行，也不覆盖基准文件。

    目录 ID（P 列）继承
    --------------------------------------------------------------------------
    语义沿用、改名或移动的**既有**实体必须继承原 P 列目录 ID，行里写
    ``catalog_id``；实际新增的实体**不要**写 ``catalog_id``，P 列留空由题湖
    导入时生成。这是硬规矩：清空或改写既有实体的目录 ID 会让系统把老节点当
    新节点，题目归属断链。本函数只接受基准工作簿里**已存在**的 ``catalog_id``
    （即只准继承、不准编造），且同一方案里不得重复。
    """
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("status") != "approved":
        raise ValueError("目录重构方案必须先人工审核为 approved")
    if sha256(baseline) != plan.get("baseline_sha256"):
        raise ValueError("基准工作簿已变化，拒绝写入")
    if output.exists():
        raise ValueError("目标文件已存在，拒绝覆盖")
    sheet_name = str(plan.get("sheet", ""))
    start = int(plan.get("replace_start_row", 0))
    end = int(plan.get("replace_end_row", 0))
    rows = plan.get("rows") or []
    if not sheet_name or start < 1 or end < start or not rows:
        raise ValueError("重构方案缺少工作表、明确替换范围或目录行")
    if any(not isinstance(row, dict) or row.get("level") not in {3, 4} for row in rows):
        raise ValueError("重构方案只能写入三级或四级目录行")
    inherited = [_as_catalog_id(row["catalog_id"]) for row in rows if str(row.get("catalog_id") or "").strip()]
    if len(inherited) != len(set(inherited)):
        raise ValueError("重构方案里要继承的目录 ID 有重复")

    baseline_book = load_catalogue_workbook(baseline)
    try:
        if sheet_name not in baseline_book.sheetnames:
            raise ValueError("重构方案引用的工作表不存在")
        baseline_sheet = baseline_book[sheet_name]
        if end > baseline_sheet.max_row:
            raise ValueError("重构范围超出工作表")
        comparison_max_column = max(baseline_sheet.max_column, 19)
        baseline_rows = [
            tuple(cell.value for cell in row)
            for row in baseline_sheet.iter_rows(max_col=comparison_max_column)
        ]
    finally:
        baseline_book.close()

    # 只准继承、不准编造：方案里的目录 ID 必须能在基准工作簿里找到原值。
    baseline_ids = {str(row[15]) for row in baseline_rows if row[15] not in (None, "")}
    unknown = [str(item) for item in inherited if str(item) not in baseline_ids]
    if unknown:
        raise ValueError("方案要继承的目录 ID 在基准工作簿中不存在：%s" % ",".join(unknown))

    book = load_catalogue_workbook(baseline)
    try:
        sheet = book[sheet_name]
        removed = end - start + 1
        sheet.delete_rows(start, removed)
        sheet.insert_rows(start, len(rows))
        style_source_row = start - 1 if start > 1 else start + len(rows)
        for offset, item in enumerate(rows):
            row_index = start + offset
            for source_cell in sheet[style_source_row]:
                target = sheet.cell(row_index, source_cell.column)
                target._style = copy(source_cell._style)
                target.number_format = source_cell.number_format
            level = int(item["level"])
            sheet.cell(row_index, 3 if level == 3 else 4).value = str(item["title"])
            if item.get("knowledge_point_id"):
                sheet.cell(row_index, 5).value = str(item["knowledge_point_id"])
            if item.get("reason"):
                sheet.cell(row_index, 14).value = str(item["reason"])
            if item.get("question_count") is not None:
                sheet.cell(row_index, 19).value = int(item["question_count"])
            if str(item.get("catalog_id") or "").strip():
                sheet.cell(row_index, 16).value = _as_catalog_id(item["catalog_id"])
        book.save(output)
    finally:
        book.close()

    reopened = load_catalogue_workbook(output)
    try:
        output_sheet = reopened[sheet_name]
        def row_values(row_index: int) -> tuple[Any, ...]:
            return tuple(output_sheet.cell(row_index, column).value for column in range(1, comparison_max_column + 1))

        # 被替换范围之外，前缀原样不动，后缀仅允许因行数变化整体平移。
        for row_index in range(1, start):
            if row_values(row_index) != baseline_rows[row_index - 1]:
                raise ValueError("重构范围之前的单元格发生变化")
        shift = len(rows) - removed
        for old_row in range(end + 1, len(baseline_rows) + 1):
            new_row = old_row + shift
            if row_values(new_row) != baseline_rows[old_row - 1]:
                raise ValueError("重构范围之后的单元格发生变化")

        # 替换范围之外已有的编号，不得被新行重复占用（基准自身的历史重复不在此列）。
        written_end = start + len(rows) - 1
        outside_codes = {
            str(output_sheet.cell(index, 5).value)
            for index in range(2, output_sheet.max_row + 1)
            if not (start <= index <= written_end) and output_sheet.cell(index, 5).value not in (None, "")
        }
        for index in range(start, written_end + 1):
            code = output_sheet.cell(index, 5).value
            if code not in (None, "") and str(code) in outside_codes:
                raise ValueError("新行的知识点编号与替换范围之外的编号撞号：%s" % code)
    finally:
        reopened.close()

    if sha256(baseline) != plan["baseline_sha256"]:
        raise ValueError("写入后基准工作簿发生变化")
    return {
        "output": str(output), "baseline_sha256": plan["baseline_sha256"],
        "replace_start_row": start, "replace_end_row": end, "written_rows": len(rows),
    }
