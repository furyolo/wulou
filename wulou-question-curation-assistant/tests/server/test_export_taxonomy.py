from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

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
            sheet.cell(5, 14).value = "须使用三角比完成综合计算。"
            sheet.cell(6, 4).value = "考法1：三角比综合计算"
            sheet.cell(6, 14).value = "须由三角比建立边长关系后求值。"
            sheet.cell(7, 2).value = "【微专题】三角函数"
            sheet.cell(8, 3).value = "不得进入大题"
            book.save(workbook_path)
            book.close()

            exported = MODULE.export_taxonomy(workbook_path, "目录")

        self.assertTrue(exported["taxonomy_version"].startswith("excel-v4-"))
        self.assertEqual(len(exported["topics"]), 1)
        topic = exported["topics"][0]
        self.assertEqual(topic["title"], "专题12：锐角三角函数")
        self.assertEqual(topic["order"], 12)
        self.assertEqual(topic["source_row"], 1)
        self.assertEqual(topic["level2"][0]["title"], "【大题】")
        self.assertEqual(topic["level2"][0]["source_row"], 4)
        self.assertEqual(
            topic["level2"][0]["level3"][0]["title"],
            "实数综合计算（含三角比）",
        )
        self.assertEqual(topic["level2"][0]["level3"][0]["source_row"], 5)
        self.assertEqual(topic["level2"][0]["level3"][0]["classification_basis"], "须使用三角比完成综合计算。")
        self.assertEqual(topic["level2"][0]["level3"][0]["level4"][0]["classification_basis"], "须由三角比建立边长关系后求值。")
        self.assertEqual(exported["source_sheet"], "目录")
        self.assertEqual(len(exported["source_workbook_sha256"]), 64)
        self.assertNotIn("12.4 一般角的三角函数值", str(exported["topics"]))
        self.assertNotIn("不得进入大题", str(exported["topics"]))

    def test_separates_evidence_ids_from_model_classification_basis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workbook_path = Path(directory) / "taxonomy.xlsx"
            book = Workbook()
            sheet = book.active
            sheet.title = "目录"
            sheet.cell(1, 1).value = "专题2：代数式"
            sheet.cell(2, 2).value = "【大题】"
            sheet.cell(3, 3).value = "分式化简与求值"
            sheet.cell(4, 4).value = "考法1：按定义域筛选代入值"
            sheet.cell(4, 14).value = "须依据原式限制筛出可代入值；现有证据题目ID：CS2025ABC_001、CS2025DEF-002"
            book.save(workbook_path)
            book.close()

            exported = MODULE.export_taxonomy(workbook_path, "目录")

        level4 = exported["topics"][0]["level2"][0]["level3"][0]["level4"][0]
        self.assertEqual(level4["classification_basis"], "须依据原式限制筛出可代入值")
        self.assertEqual(level4["audit_evidence_exercise_ids"], ["CS2025ABC_001", "CS2025DEF-002"])

    def test_reads_workbook_with_blank_page_margin_via_temporary_compatible_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workbook_path = Path(directory) / "taxonomy.xlsx"
            book = Workbook()
            sheet = book.active
            sheet.title = "目录"
            sheet.cell(1, 1).value = "专题1：实数"
            sheet.cell(2, 2).value = "【大题】"
            sheet.cell(3, 3).value = "实数运算"
            book.save(workbook_path)
            book.close()

            temporary_path = Path(directory) / "rewritten.xlsx"
            with ZipFile(workbook_path) as source, ZipFile(temporary_path, "w", ZIP_DEFLATED) as destination:
                for member in source.infolist():
                    content = source.read(member.filename)
                    if member.filename == "xl/worksheets/sheet1.xml":
                        content = content.replace(
                            b"<pageMargins left=\"0.75\" right=\"0.75\" top=\"1\" bottom=\"1\" header=\"0.5\" footer=\"0.5\"/>",
                            b"<pageMargins left=\"\" right=\"\" top=\"\" bottom=\"\" header=\"0.3\" footer=\"0.3\"/>",
                        )
                    destination.writestr(member, content)
            temporary_path.replace(workbook_path)

            exported = MODULE.export_taxonomy(workbook_path, "目录")

        self.assertEqual(exported["topics"][0]["level2"][0]["level3"][0]["title"], "实数运算")


if __name__ == "__main__":
    unittest.main()
