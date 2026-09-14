"""从已确认的 Excel 工作簿导出分类目录快照。"""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import yaml
from openpyxl import load_workbook


TOPIC_PATTERN = re.compile(r"^专题\s*(\d+)\s*[：:]?\s*(.+)$")
EXPORT_SCHEMA_VERSION = "v2"
LOGGER = logging.getLogger(__name__)
_EMPTY_PAGE_MARGIN = re.compile(rb'\s(?:left|right|top|bottom)=""')


def node_id(prefix: str, row: int, title: str) -> str:
    digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}-{row}-{digest}"


def _text(value: Any) -> str:
    return str(value or "").strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _compatible_workbook_copy(workbook_path: Path) -> Path | None:
    """修复部分 Excel 导出器写出的空白页边距，仅用于内存外的临时读取副本。"""
    changed = False
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as file:
        temporary_path = Path(file.name)
    try:
        with ZipFile(workbook_path) as source, ZipFile(temporary_path, "w", ZIP_DEFLATED) as destination:
            for member in source.infolist():
                content = source.read(member.filename)
                if member.filename.startswith("xl/worksheets/") and member.filename.endswith(".xml"):
                    normalized = _EMPTY_PAGE_MARGIN.sub(b"", content)
                    changed = changed or normalized != content
                    content = normalized
                destination.writestr(member, content)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    if not changed:
        temporary_path.unlink(missing_ok=True)
        return None
    return temporary_path


def _load_workbook(workbook_path: Path):
    try:
        return load_workbook(workbook_path, read_only=False, data_only=False), None
    except TypeError:
        compatible_path = _compatible_workbook_copy(workbook_path)
        if compatible_path is None:
            raise
        try:
            book = load_workbook(compatible_path, read_only=False, data_only=False)
        except Exception:
            compatible_path.unlink(missing_ok=True)
            raise
        LOGGER.warning("目录工作簿含空白页边距，已使用临时兼容副本读取：%s", workbook_path)
        return book, compatible_path


def export_taxonomy(workbook_path: Path, sheet_name: str) -> dict[str, Any]:
    """只读提取 A 至 E 列定义的专题、大题、三级和四级目录。"""
    source_path = workbook_path.resolve()
    book, compatible_path = _load_workbook(source_path)
    try:
        sheet = book[sheet_name]
        topic_rows: list[tuple[int, str, int]] = []
        for row in range(1, sheet.max_row + 1):
            title = _text(sheet.cell(row, 1).value)
            match = TOPIC_PATTERN.match(title)
            if match:
                topic_rows.append((row, title, int(match.group(1))))

        topics: list[dict[str, Any]] = []
        for index, (topic_row, topic_title, topic_order) in enumerate(topic_rows):
            end = topic_rows[index + 1][0] if index + 1 < len(topic_rows) else sheet.max_row + 1
            large_row = next(
                (row for row in range(topic_row + 1, end) if _text(sheet.cell(row, 2).value) == "【大题】"),
                None,
            )
            if large_row is None:
                continue

            # B 列出现下一个二级目录时，【大题】范围结束；不得继承前一个 B 列标题。
            stop = next(
                (row for row in range(large_row + 1, end) if _text(sheet.cell(row, 2).value)),
                end,
            )
            level3: list[dict[str, Any]] = []
            active: dict[str, Any] | None = None
            for row in range(large_row + 1, stop):
                level3_title = _text(sheet.cell(row, 3).value)
                level4_title = _text(sheet.cell(row, 4).value)
                knowledge_point_id = _text(sheet.cell(row, 5).value)
                if level3_title:
                    active = {
                        "id": node_id("l3", row, level3_title), "source_row": row,
                        "title": level3_title,
                        "knowledge_point_id": knowledge_point_id or None,
                        "level4": [],
                    }
                    level3.append(active)
                elif level4_title and active is not None:
                    active["level4"].append({
                        "id": node_id("l4", row, level4_title), "source_row": row,
                        "title": level4_title,
                        "knowledge_point_id": knowledge_point_id or None,
                    })

            if level3:
                topics.append({
                    "id": node_id("topic", topic_row, topic_title), "source_row": topic_row,
                    "title": topic_title,
                    "order": topic_order,
                    "level2": [{
                        "id": node_id("large", large_row, topic_title), "source_row": large_row,
                        "title": "【大题】",
                        "level3": level3,
                    }],
                })
    finally:
        book.close()
        if compatible_path:
            compatible_path.unlink(missing_ok=True)

    if not topics:
        raise ValueError("未从工作簿 A 列专题范围内提取到任何【大题】目录")
    source_sha256 = sha256_file(source_path)
    return {
        "taxonomy_version": f"excel-{EXPORT_SCHEMA_VERSION}-{source_sha256[:12]}",
        "source_workbook": str(source_path),
        "source_sheet": sheet_name,
        "source_workbook_sha256": source_sha256,
        "topics": topics,
    }


def dump_taxonomy(data: dict[str, Any], output_path: Path) -> None:
    """以统一编码和换行写入导出的 YAML。调用方负责原子替换。"""
    with output_path.open("w", encoding="utf-8", newline="\n") as file:
        yaml.safe_dump(data, file, allow_unicode=True, sort_keys=False)
