"""全新机器上的启动兜底：没有 .local-data 也得能起来，起来之后还得有目录可用。

背景：配置里 taxonomy_path 沿用示例名时，实际快照会被重定向到 Git 忽略的
.local-data/taxonomy.yaml，而这个文件出厂时并不存在。以前没人铺它，全新 clone 直接
FileNotFoundError，服务当场退不出来——用户连进设置选目录来源的机会都没有。
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[2]

from server import main as main_module
from server.main import ServiceState
from server.taxonomy import TaxonomyError

EXAMPLE_TAXONOMY = """taxonomy_version: 2026-01-01.1
topics:
  - id: topic-01-real-numbers
    title: 专题一 实数
    order: 1
    level2:
      - id: topic-01-large
        title: 【大题】
        level3:
          - id: real-number-calculation
            title: 实数综合计算
            knowledge_point_id: null
            level4: []
"""


def _write_local_config(root: Path, *, directory_workbook: dict | None = None) -> Path:
    """按出厂默认铺一份本机配置：taxonomy_path 沿用示例名（也就是会被重定向的那条路）。"""
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "taxonomy.example.yaml").write_text(EXAMPLE_TAXONOMY, encoding="utf-8")
    settings: dict = {
        "host": "127.0.0.1",
        "port": 0,
        "taxonomy_path": "taxonomy.example.yaml",
        "rules_path": str(ROOT / "config" / "classification-rules.yaml"),
        "cache_path": "cache.sqlite3",
    }
    if directory_workbook is not None:
        settings["directory_workbook"] = directory_workbook
    path = root / "config" / "settings.local.yaml"
    path.write_text(yaml.safe_dump(settings, allow_unicode=True), encoding="utf-8")
    return path


@contextmanager
def _fake_project(root: Path):
    """把 PROJECT_ROOT 指到临时目录，让「出厂路径」这个词在测试里真的成立。"""
    with mock.patch.object(main_module, "PROJECT_ROOT", root):
        yield


class TaxonomyBootstrapTests(unittest.TestCase):
    def test_starts_with_empty_local_data_and_bootstraps_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = _write_local_config(root)
            snapshot = root / ".local-data" / "taxonomy.yaml"
            self.assertFalse(snapshot.exists(), "前置条件：出厂时这个文件不该存在")

            with _fake_project(root):
                state = ServiceState(config_path)
                try:
                    self.assertEqual(state.taxonomy_path, snapshot)
                    self.assertTrue(snapshot.is_file(), "启动后必须把兜底快照铺出来")
                    # 还没选目录来源，所以同步是关着的；关键是服务活着，前端才有机会引导用户。
                    self.assertEqual(state.taxonomy_sync_status, "disabled")
                    self.assertEqual(state.taxonomy_sync_error, "")
                    self.assertEqual(state.taxonomy.version, "2026-01-01.1")
                    self.assertEqual(state.active_directory_workbook(), "")
                finally:
                    state.close()

    def test_falls_back_to_bootstrap_snapshot_when_anchor_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # 锚点指向一个不存在的文件夹：工作簿被搬走后就是这个样子。
            config_path = _write_local_config(root, directory_workbook={
                "path": str(root / "已经搬走的文件夹" / "★目录,ID-3777-2026-09-18 v1.xlsx"),
                "sheet": "目录",
            })

            with _fake_project(root):
                state = ServiceState(config_path)
                try:
                    # 旧代码在这里直接崩；现在带着故障活着，用户才能把路径改回来。
                    self.assertEqual(state.taxonomy_sync_status, "failed")
                    self.assertIn("未找到目录工作簿", state.taxonomy_sync_error)
                    self.assertTrue((root / ".local-data" / "taxonomy.yaml").is_file())
                    self.assertEqual(state.active_directory_workbook(), "")
                finally:
                    state.close()

    def test_bootstrap_never_overwrites_an_existing_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = _write_local_config(root)
            snapshot = root / ".local-data" / "taxonomy.yaml"
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_text(EXAMPLE_TAXONOMY.replace("2026-01-01.1", "现役目录.9"), encoding="utf-8")

            with _fake_project(root):
                state = ServiceState(config_path)
                try:
                    # 兜底只在「没有」的时候铺，绝不能把机器上正在用的目录顶掉。
                    self.assertEqual(state.taxonomy.version, "现役目录.9")
                finally:
                    state.close()

    def test_missing_example_file_reports_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = _write_local_config(root)
            (root / "config" / "taxonomy.example.yaml").unlink()

            with _fake_project(root):
                with self.assertRaises(TaxonomyError) as raised:
                    ServiceState(config_path)
            self.assertIn("找不到目录示例文件", str(raised.exception))

    def test_user_specified_missing_snapshot_is_a_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = _write_local_config(root)
            settings = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            # 用户自己把 taxonomy_path 指到别处：这种路径缺失属于配置错误，不该偷偷铺兜底。
            settings["taxonomy_path"] = "别处的目录.yaml"
            config_path.write_text(yaml.safe_dump(settings, allow_unicode=True), encoding="utf-8")

            with _fake_project(root):
                with self.assertRaises(TaxonomyError) as raised:
                    ServiceState(config_path)
            self.assertIn("目录快照不存在", str(raised.exception))
            self.assertFalse((root / "config" / "别处的目录.yaml").exists())


if __name__ == "__main__":
    unittest.main()
