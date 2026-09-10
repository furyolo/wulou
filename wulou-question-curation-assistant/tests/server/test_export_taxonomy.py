from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "export-taxonomy.py"
SPEC = importlib.util.spec_from_file_location("export_taxonomy_script", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("无法加载目录导出脚本")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ExportTaxonomyTests(unittest.TestCase):
    def test_uses_column_a_topic_and_does_not_promote_previous_level2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workbook_path = Path(directory) / "taxonomy.xlsx"
            book = Workbook()
            sheet = book.active
            sheet.title = "目录"
            sheet.cell(1, 1).value = "专题12：锐角三角函数"
            sheet.cell(2, 2).value = "12.4 一般角的三角函数值"
            sheet.cell(3, 3).value = "一般角三角函数"
            sheet.cell(4, 2).value = "【大题】"
            sheet.cell(5, 3).value = "实数综合计算（含三角比）"
            sheet.cell(5, 5).value = "TRIG-001"
            sheet.cell(6, 2).value = "【微专题】三角函数"
            sheet.cell(7, 3).value = "不得进入大题"
            book.save(workbook_path)
            book.close()

            exported = MODULE.export_taxonomy(workbook_path, "目录")

        self.assertTrue(exported["taxonomy_version"].startswith("excel-v2-"))
        self.assertEqual(len(exported["topics"]), 1)
        topic = exported["topics"][0]
        self.assertEqual(topic["title"], "专题12：锐角三角函数")
        self.assertEqual(topic["order"], 12)
        self.assertEqual(topic["level2"][0]["title"], "【大题】")
        self.assertEqual(
            topic["level2"][0]["level3"][0]["title"],
            "实数综合计算（含三角比）",
        )
        self.assertNotIn("12.4 一般角的三角函数值", str(exported["topics"]))
        self.assertNotIn("不得进入大题", str(exported["topics"]))


if __name__ == "__main__":
    unittest.main()
