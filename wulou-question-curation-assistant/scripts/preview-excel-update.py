"""读取基准工作簿并生成只读预检 JSON，不修改任何 Excel 文件。"""
from __future__ import annotations
import argparse, json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.excel_sync import inspect_workbook

parser = argparse.ArgumentParser()
parser.add_argument("baseline", type=Path); parser.add_argument("topic_title"); parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
args.output.write_text(json.dumps(inspect_workbook(args.baseline, args.topic_title), ensure_ascii=False, indent=2), encoding="utf-8")
print(args.output)
