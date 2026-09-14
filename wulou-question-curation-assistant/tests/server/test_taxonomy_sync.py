from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from server.taxonomy_sync import TaxonomySyncError, discover_latest_workbook, synchronize_taxonomy


def _write_workbook(path: Path, level3_title: str) -> None:
    book = Workbook()
    sheet = book.active
    sheet.title = "目录"
    sheet.cell(1, 1).value = "专题1：实数"
    sheet.cell(2, 2).value = "【大题】"
    sheet.cell(3, 3).value = level3_title
    book.save(path)
    book.close()


class TaxonomySyncTests(unittest.TestCase):
    def test_selects_latest_date_then_revision_and_rebuilds_only_when_changed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_workbook = root / "目录,ID-3777-2026-09-05 v5.xlsx"
            newest_workbook = root / "目录,ID-3777-2026-09-12 v1.xlsx"
            _write_workbook(old_workbook, "旧目录")
            _write_workbook(newest_workbook, "新目录")
            settings_path = root / "settings.local.yaml"
            output_path = root / "taxonomy.yaml"

            self.assertEqual(discover_latest_workbook(old_workbook), newest_workbook.resolve())
            first = synchronize_taxonomy(
                config_path=settings_path,
                workbook_settings={"path": str(old_workbook), "sheet": "目录"},
                output_path=output_path,
            )
            second = synchronize_taxonomy(
                config_path=settings_path,
                workbook_settings={"path": str(old_workbook), "sheet": "目录"},
                output_path=output_path,
            )

            self.assertEqual(first.status, "updated")
            self.assertEqual(first.workbook_path, newest_workbook.resolve())
            self.assertEqual(second.status, "up_to_date")
            self.assertIn("新目录", output_path.read_text(encoding="utf-8"))

    def test_rejects_missing_unversioned_workbook(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(TaxonomySyncError):
                discover_latest_workbook(Path(directory) / "目录.xlsx")


if __name__ == "__main__":
    unittest.main()
