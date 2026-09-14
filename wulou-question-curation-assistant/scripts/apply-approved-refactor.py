"""将人工审核、已锚定范围的目录重构方案写入新的 Excel 版本。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from server.excel_sync import write_approved_refactor


def main() -> int:
    parser = argparse.ArgumentParser(description="写入已审核的目录重构 Excel 方案")
    parser.add_argument("baseline", type=Path, help="只读基准工作簿")
    parser.add_argument("plan", type=Path, help="status=approved 且含明确替换行范围的 JSON 方案")
    parser.add_argument("output", type=Path, help="不存在的新版本工作簿路径")
    args = parser.parse_args()
    result = write_approved_refactor(args.baseline, args.output, args.plan)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
