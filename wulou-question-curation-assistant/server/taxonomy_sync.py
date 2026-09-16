"""启动时发现最新目录工作簿并原子更新 taxonomy 快照。"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

try:
    from .taxonomy import Taxonomy
    from .taxonomy_export import EXPORT_SCHEMA_VERSION, dump_taxonomy, export_taxonomy, sha256_file
except ImportError:  # 支持由 scripts/start-server.* 直接运行 server/main.py。
    from taxonomy import Taxonomy
    from taxonomy_export import EXPORT_SCHEMA_VERSION, dump_taxonomy, export_taxonomy, sha256_file


_WORKBOOK_NAME = re.compile(
    r"^(?P<prefix>.+?)(?P<day>\d{4}-\d{2}-\d{2})\s+v(?P<revision>\d+)\.xlsx$",
    re.IGNORECASE,
)


class TaxonomySyncError(ValueError):
    """启动时无法可靠定位或导出当前目录工作簿。"""


@dataclass(frozen=True)
class TaxonomySyncResult:
    workbook_path: Path | None
    output_path: Path
    status: str


def _resolve_path(config_path: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    return path.resolve() if path.is_absolute() else (config_path.parent / path).resolve()


def _workbook_sort_key(path: Path, prefix: str) -> tuple[date, int]:
    match = _WORKBOOK_NAME.fullmatch(path.name)
    if not match or match.group("prefix") != prefix:
        raise TaxonomySyncError(f"目录工作簿文件名不符合版本规则：{path.name}")
    try:
        return date.fromisoformat(match.group("day")), int(match.group("revision"))
    except ValueError as error:
        raise TaxonomySyncError(f"目录工作簿日期或版本号无效：{path.name}") from error


def discover_latest_workbook(configured_path: Path) -> Path:
    """只在已配置工作簿的同一命名前缀内，按日期和 vN 选择最新版本。"""
    match = _WORKBOOK_NAME.fullmatch(configured_path.name)
    if not match:
        if configured_path.is_file():
            return configured_path
        raise TaxonomySyncError(
            "directory_workbook.path 不存在，且文件名不含“YYYY-MM-DD vN.xlsx”，无法安全发现最新版本"
        )
    prefix = match.group("prefix")
    candidates = [
        path for path in configured_path.parent.glob("*.xlsx")
        if not path.name.startswith("~$") and _WORKBOOK_NAME.fullmatch(path.name)
        and _WORKBOOK_NAME.fullmatch(path.name).group("prefix") == prefix
    ]
    if not candidates:
        raise TaxonomySyncError(f"未找到目录工作簿：{configured_path.parent}")
    return max(candidates, key=lambda path: _workbook_sort_key(path, prefix)).resolve()


def _existing_export_metadata(output_path: Path) -> tuple[str | None, str | None]:
    if not output_path.is_file():
        return None, None
    with output_path.open("r", encoding="utf-8") as file:
        raw = yaml.safe_load(file) or {}
    if not isinstance(raw, dict):
        return None, None
    return (
        str(raw.get("source_workbook_sha256") or "") or None,
        str(raw.get("taxonomy_version") or "") or None,
    )


def synchronize_taxonomy(
    *, config_path: Path, workbook_settings: Any, output_path: Path,
) -> TaxonomySyncResult:
    """同步已配置目录族；同一内容哈希时不打开工作簿，也不改写快照。"""
    if not isinstance(workbook_settings, dict) or not workbook_settings.get("path"):
        return TaxonomySyncResult(None, output_path, "disabled")
    if workbook_settings.get("auto_sync", True) is False:
        return TaxonomySyncResult(None, output_path, "disabled")

    workbook_path = discover_latest_workbook(_resolve_path(config_path, str(workbook_settings["path"])))
    source_hash = sha256_file(workbook_path)
    existing_hash, existing_version = _existing_export_metadata(output_path)
    expected_version_prefix = f"excel-{EXPORT_SCHEMA_VERSION}-"
    if existing_hash == source_hash and existing_version and existing_version.startswith(expected_version_prefix):
        return TaxonomySyncResult(workbook_path, output_path, "up_to_date")

    sheet_name = str(workbook_settings.get("sheet") or "目录")
    exported = export_taxonomy(workbook_path, sheet_name)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=output_path.parent, delete=False) as file:
        temporary_path = Path(file.name)
    try:
        dump_taxonomy(exported, temporary_path)
        Taxonomy.from_file(temporary_path)
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return TaxonomySyncResult(workbook_path, output_path, "updated")
