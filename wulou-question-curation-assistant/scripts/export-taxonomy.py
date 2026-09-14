"""从目录工作簿只读导出题湖分类所需的专题 / 大题目录 YAML。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.taxonomy_export import dump_taxonomy, export_taxonomy


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--sheet", default="目录")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    data = export_taxonomy(args.workbook, args.sheet)
    dump_taxonomy(data, args.output)
    print(f"已导出 {len(data['topics'])} 个专题：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
