"""本机「选择文件夹」窗口模块的回归测试。

这里测的是「服务端替浏览器开本机窗口」这件事的规矩：同一时刻只准开一个窗口、
超时不算用户取消、窗口开不出来只算这一次失败。真的弹窗没法测，所以全部走注入的
替身实现，测试里永远不会冒出窗口。
"""

from __future__ import annotations

import importlib.util
import threading
import time
import unittest

from server.directory_picker import DirectoryPickerError, pick_path


class PickPathTests(unittest.TestCase):
    def test_returns_the_folder_the_user_picked(self) -> None:
        seen: list[str] = []

        def opener(initial_dir: str) -> str:
            seen.append(initial_dir)
            return r"D:\materials\导出目录"

        picked = pick_path(initial_dir=r"D:\materials", opener=opener)
        self.assertEqual(picked["path"], r"D:\materials\导出目录")
        self.assertFalse(picked["cancelled"])
        self.assertFalse(picked["busy"])
        # 开框位置应当是服务端给的建议目录，用户少点几层。
        self.assertEqual(seen, [r"D:\materials"])

    def test_user_closing_the_dialog_is_a_normal_cancel(self) -> None:
        result = pick_path(opener=lambda initial_dir: "")
        self.assertEqual(result["path"], "")
        self.assertTrue(result["cancelled"])
        self.assertFalse(result["timed_out"])
        self.assertIn("没有选择", result["message"])

    def test_dialog_failure_only_fails_this_call(self) -> None:
        def opener(initial_dir: str) -> str:
            raise DirectoryPickerError("打不开本机选择窗口：no display name")

        with self.assertRaises(DirectoryPickerError):
            pick_path(opener=opener)
        # 上一次失败不能把锁留住，否则这台机器再也弹不出窗口了。
        self.assertEqual(pick_path(opener=lambda initial_dir: r"D:\ok")["path"], r"D:\ok")

    def test_second_request_while_a_window_is_open_reports_busy(self) -> None:
        release = threading.Event()
        entered = threading.Event()

        def blocking(initial_dir: str) -> str:
            entered.set()
            release.wait(timeout=5)
            return r"D:\late"

        first: dict = {}
        thread = threading.Thread(target=lambda: first.update(pick_path(opener=blocking)), daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(timeout=5), "替身窗口没有真的进入等待")
            second = pick_path(opener=lambda initial_dir: r"D:\should-not-happen")
            self.assertTrue(second["busy"])
            self.assertEqual(second["path"], "")
            self.assertIn("已经有一个选择窗口开着", second["message"])
        finally:
            release.set()
            thread.join(timeout=5)
        self.assertEqual(first["path"], r"D:\late")
        # 窗口关掉之后必须恢复正常，不能一直卡在「忙」。
        self.assertEqual(pick_path(opener=lambda initial_dir: r"D:\again")["path"], r"D:\again")

    def test_timeout_is_not_treated_as_the_user_cancelling(self) -> None:
        release = threading.Event()
        entered = threading.Event()

        def slow(initial_dir: str) -> str:
            entered.set()
            release.wait(timeout=5)
            return r"D:\eventually"

        try:
            result = pick_path(timeout_seconds=0.05, opener=slow)
            self.assertTrue(result["timed_out"])
            self.assertFalse(result["busy"])
            self.assertEqual(result["path"], "")
            self.assertIn("还开着", result["message"])
            self.assertTrue(entered.is_set(), "替身窗口应当已经打开")
            # 窗口还开着、锁也还握着：这时再来一轮依然是「忙」，而不是叠出第二个窗口。
            self.assertTrue(pick_path(opener=lambda initial_dir: r"D:\nope")["busy"])
        finally:
            release.set()

        # 等替身线程真正退出，锁才会交回来；这里轮询而不是死等，避免测试挂住。
        probe: dict = {}
        deadline = time.time() + 5
        while time.time() < deadline:
            probe = pick_path(opener=lambda initial_dir: r"D:\clean")
            if probe["path"] == r"D:\clean":
                break
            time.sleep(0.02)
        self.assertEqual(probe["path"], r"D:\clean")


@unittest.skipUnless(importlib.util.find_spec("tkinter") is not None, "本机没有 tkinter，走 PowerShell 退路")
class TkinterDialogWiringTests(unittest.TestCase):
    """真开一次 Tk 根窗口，但把对话框本身换成替身。

    这样能验到「窗口怎么开」这一层：标题、初始目录、以及关掉根窗口。
    真正的对话框得由人来点，自动化里不弹。
    """

    def test_folder_dialog_gets_the_right_options(self) -> None:
        import server.directory_picker as directory_picker
        from tkinter import TclError, filedialog

        calls: list[dict] = []
        original = filedialog.askdirectory
        filedialog.askdirectory = lambda **kwargs: calls.append(kwargs) or r"D:\materials\导出目录"
        try:
            first = directory_picker._run_tkinter(r"D:\materials")
            second = directory_picker._run_tkinter("")
        except TclError as error:  # 没有图形会话（远程/无桌面）时不算失败
            self.skipTest(f"本机没有图形会话：{error}")
        finally:
            filedialog.askdirectory = original

        self.assertTrue(first.endswith("导出目录"))
        self.assertEqual(second, first)
        self.assertEqual(calls[0]["title"], "选择存放目录工作簿的文件夹")
        self.assertEqual(calls[0]["initialdir"], r"D:\materials")
        # 没有建议目录时不能传空的 initialdir，否则 Tk 会去猜一个用户没去过的位置。
        self.assertNotIn("initialdir", calls[1])


if __name__ == "__main__":
    unittest.main()
