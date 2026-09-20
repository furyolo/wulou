"""源码编译自检：任何 Python 文件都不许带 SyntaxWarning 编译过去。

起因：`server/directory_picker.py` 的模块 docstring 里写了 `C:\\fakepath\\...`，
非 raw 字符串里的 `\\.` 是无效转义——服务一启动就在控制台刷
`SyntaxWarning: invalid escape sequence '\\.'`，还把 `\\f` 悄悄变成了换页符。
这种错只在启动那一刻露脸，跑测试抓不到，所以直接钉成一条回归。
"""

from __future__ import annotations

import pathlib
import unittest
import warnings

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
SKIP_DIRECTORIES = {".venv", "__pycache__", ".local-data", "node_modules", ".git"}


class SourceSyntaxTests(unittest.TestCase):
    def test_every_python_file_compiles_without_warnings(self) -> None:
        problems: list[str] = []
        for path in sorted(PROJECT_ROOT.rglob("*.py")):
            if SKIP_DIRECTORIES & set(path.parts):
                continue
            relative = path.relative_to(PROJECT_ROOT)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                try:
                    compile(path.read_text(encoding="utf-8"), str(path), "exec")
                except SyntaxError as error:
                    problems.append(f"{relative}:{error.lineno}: {error.msg}")
                    continue
            problems.extend(
                f"{relative}:{item.lineno}: {item.message}"
                for item in caught
                if issubclass(item.category, SyntaxWarning)
            )
        self.assertEqual(problems, [], "以下文件编译时报警：\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
