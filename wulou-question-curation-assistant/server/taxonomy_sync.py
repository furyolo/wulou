"""启动时发现最新目录工作簿并原子更新 taxonomy 快照。"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

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


class EmptyWorkbookFolder(TaxonomySyncError):
    """用户指到了文件夹，但里面一个符合版本命名的目录工作簿都没有。"""


class AmbiguousWorkbookFolder(TaxonomySyncError):
    """文件夹里有多组命名不同的目录工作簿，选哪一组不能替用户猜。

    命名前缀就是「哪一套目录」的身份（题湖导出的工作簿把课程 ID 写在前缀里），
    猜错了会静默换掉整套目录，所以这里只报告候选，由用户点一份。
    """

    def __init__(self, folder: Path, families: list["WorkbookFamily"]) -> None:
        self.folder = folder
        self.families = list(families)
        listed = "；".join(f"{family.prefix.strip(' ,，')}（最新 {family.latest.name}）" for family in self.families)
        super().__init__(
            f"这个文件夹里有 {len(self.families)} 组目录工作簿，不是唯一一套，请直接指定其中一份：{listed}"
        )


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


WORKBOOK_NAME_PATTERN = _WORKBOOK_NAME
"""公开别名：前端设置界面与回归测试都要按同一套版本规则识别目录工作簿。"""


@dataclass(frozen=True)
class WorkbookFamily:
    """同一个文件夹里、同一命名前缀下的一组目录工作簿版本。"""

    folder: Path
    prefix: str
    latest: Path
    revision_date: date
    revision: int
    version_count: int


def resolve_workbook_anchor(raw_path: Any) -> Path:
    """把前端提交的目录来源规范成可被自动发现复用的锚点路径。

    锚点不要求文件当前存在：它只是「哪个文件夹、哪个命名前缀」的定位信息，
    这样用户选的旧版本被删掉之后，同一文件夹里的新版本仍然能被发现。

    用户可以指文件夹，也可以指具体某一份工作簿：浏览器里的选择框只能二选一，
    所以两条都必须认。指文件夹时交给 :func:`resolve_workbook_folder` 判唯一性。
    """
    text = str(raw_path or "").strip().strip('"').strip("'")
    if not text:
        raise TaxonomySyncError("请选择目录工作簿或它所在的文件夹，也可以直接填写完整路径")
    candidate = Path(text)
    if not candidate.is_absolute():
        raise TaxonomySyncError("目录来源要填完整路径，例如 D:\\...\\导出目录\\★全国中考数学-导出目录,ID-3777-2026-09-18 v2.xlsx")
    if candidate.is_dir():
        return resolve_workbook_folder(candidate)
    if candidate.name.startswith("~$") or candidate.suffix.lower() != ".xlsx":
        raise TaxonomySyncError("目录来源必须是 .xlsx 目录工作簿或它所在的文件夹，不能是临时文件或其它格式")
    if not _WORKBOOK_NAME.fullmatch(candidate.name):
        raise TaxonomySyncError("目录工作簿文件名要符合“名称 YYYY-MM-DD vN.xlsx”，否则没法自动跟随同文件夹里的更新版本；也可以直接填它所在的文件夹")
    if not candidate.parent.is_dir():
        raise TaxonomySyncError(f"找不到这个文件夹：{candidate.parent}")
    return candidate.resolve()


def resolve_workbook_folder(folder: Path) -> Path:
    """把「存放目录工作簿的文件夹」解析成锚点：文件夹里唯一那一族的最新版本。

    只扫这一层，不递归——用户指的是这个文件夹，隔壁子目录里的工作簿不算数。
    多族就报错让用户自己挑，不替他猜，免得静默换掉整套目录。
    """
    if not folder.is_dir():
        raise TaxonomySyncError(f"找不到这个文件夹：{folder}")
    families = discover_workbook_families([folder], maximum_depth=0)
    if not families:
        raise EmptyWorkbookFolder(
            f"这个文件夹里没有「名称 YYYY-MM-DD vN.xlsx」格式的目录工作簿：{folder}"
        )
    if len(families) > 1:
        raise AmbiguousWorkbookFolder(folder, families)
    return families[0].latest.resolve()


def discover_workbook_families(
    roots: Iterable[Path], *, maximum_depth: int = 3,
) -> list[WorkbookFamily]:
    """扫描候选根目录，按“文件夹 + 命名前缀”归组，每组只留最新版本。"""
    grouped: dict[tuple[str, str], list[Path]] = {}
    visited: set[str] = set()
    for root in roots:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for current, subdirectories, filenames in os.walk(root_path):
            current_path = Path(current)
            current_key = str(current_path.resolve())
            try:
                depth = len(current_path.relative_to(root_path).parts)
            except ValueError:
                depth = maximum_depth
            # 只递归到指定层数，并跳过隐藏目录、临时目录与归档目录。
            subdirectories[:] = [] if depth >= maximum_depth else [
                name for name in subdirectories
                if not name.startswith((".", "~$", "_"))
            ]
            if current_key in visited:
                continue
            visited.add(current_key)
            for name in filenames:
                if name.startswith("~$"):
                    continue
                match = _WORKBOOK_NAME.fullmatch(name)
                if not match:
                    continue
                grouped.setdefault((current_key, match.group("prefix")), []).append(current_path / name)
    families: list[WorkbookFamily] = []
    for (folder_key, prefix), paths in grouped.items():
        latest = max(paths, key=lambda path: _workbook_sort_key(path, prefix))
        revision_date, revision = _workbook_sort_key(latest, prefix)
        families.append(WorkbookFamily(
            folder=Path(folder_key), prefix=prefix, latest=latest.resolve(),
            revision_date=revision_date, revision=revision, version_count=len(paths),
        ))
    families.sort(key=lambda family: (str(family.folder).casefold(), family.prefix.casefold()))
    return families


def describe_workbook_families(families: Iterable[WorkbookFamily]) -> list[dict[str, Any]]:
    """把族信息摊平成一串候选条目，供前端下拉框和「就地挑一份」共用同一套字段。"""
    return [{
        "folder": str(family.folder),
        "prefix": family.prefix,
        "path": str(family.latest),
        "file_name": family.latest.name,
        "date": family.revision_date.isoformat(),
        "revision": family.revision,
        "version_count": family.version_count,
    } for family in families]


def summarize_directory_sources(
    configured_path: Path | None, roots: Iterable[Path], *, maximum_depth: int = 3,
) -> dict[str, Any]:
    """给前端设置面板用的目录来源清单，含当前配置是否还解析得到。"""
    families = discover_workbook_families(roots, maximum_depth=maximum_depth)
    anchor = configured_path.resolve() if configured_path else None
    active: Path | None = None
    error_message = ""
    if anchor is not None:
        try:
            active = discover_latest_workbook(anchor)
        except TaxonomySyncError as error:
            error_message = str(error)
    candidates = describe_workbook_families(families)
    for item, family in zip(candidates, families):
        item["is_active"] = active is not None and family.latest == active
        item["is_anchor"] = anchor is not None and family.folder == anchor.parent
    return {
        "anchor": str(anchor) if anchor else "",
        "active": str(active) if active else "",
        "anchor_valid": active is not None,
        "error": error_message,
        "candidates": candidates,
    }


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
