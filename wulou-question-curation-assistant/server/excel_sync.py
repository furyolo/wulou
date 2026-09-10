"""Excel 目录的只读预检和受控新版本写入。

基准工作簿永不覆盖；写入仅接受已人工审核的明确目录方案。
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import copy
from datetime import date
from pathlib import Path
from typing import Any

from openpyxl import load_workbook


SF_PATTERN = re.compile(r"^ZCSQG(\d{8})SF(\d+)$")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_workbook(path: Path, topic_title: str) -> dict[str, Any]:
    book = load_workbook(path, read_only=False, data_only=False)
    matches: list[dict[str, Any]] = []
    for sheet in book.worksheets:
        topic_rows = [row for row in range(1, sheet.max_row + 1) if str(sheet.cell(row, 2).value or "").strip() == topic_title]
        for topic_row in topic_rows:
            end = next((row for row in range(topic_row + 1, sheet.max_row + 1) if str(sheet.cell(row, 2).value or "").strip().startswith("专题")), sheet.max_row + 1)
            large_row = next((row for row in range(topic_row + 1, end) if str(sheet.cell(row, 2).value or "").strip() == "【大题】"), None)
            matches.append({"sheet": sheet.title, "topic_row": topic_row, "topic_end_row": end - 1, "large_row": large_row})
    if len(matches) != 1: raise ValueError(f"应唯一找到专题“{topic_title}”，实际找到 {len(matches)} 处")
    return {"baseline": str(path.resolve()), "baseline_sha256": sha256(path), "sheets": book.sheetnames, "topic": matches[0]}


def next_sf_number(folder: Path, today: str | None = None) -> int:
    today = today or date.today().strftime("%Y%m%d"); maximum = 0
    for file in folder.glob("★全国中考数学-导出目录*.xlsx"):
        workbook = load_workbook(file, read_only=True, data_only=False)
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows(min_col=5, max_col=5, values_only=True):
                match = SF_PATTERN.match(str(row[0] or ""))
                if match and match.group(1) == today: maximum = max(maximum, int(match.group(2)))
    return maximum + 1


def write_approved_plan(baseline: Path, output: Path, plan_path: Path) -> dict[str, Any]:
    """将明确行号和审核状态均已固定的方案写入新文件，并重新打开核验。"""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("status") != "approved": raise ValueError("Excel 方案必须先人工审核为 approved")
    if sha256(baseline) != plan.get("baseline_sha256"): raise ValueError("基准工作簿已变化，拒绝写入")
    if output.exists(): raise ValueError("目标文件已存在，拒绝覆盖")
    book = load_workbook(baseline, data_only=False); sheet = book[plan["sheet"]]
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
    load_workbook(output, read_only=True).close()
    if sha256(baseline) != plan["baseline_sha256"]: raise ValueError("写入后基准工作簿发生变化")
    return {"output": str(output), "baseline_sha256": plan["baseline_sha256"], "inserted_rows": len(rows)}
