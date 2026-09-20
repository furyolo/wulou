r"""在运行本地服务的这台机器上弹出系统的「选择文件夹」窗口，把用户选的路径回给前端。

浏览器打不开本机原生对话框，也拿不到真实的本机绝对路径：`<input type="file">` 只会给
`C:\fakepath\...` 这种假路径，File System Access API 只给句柄、不给路径。而打开目录
工作簿这件事必须由本机进程拿到真路径。所以由服务端开框：用户点「目录来源」那个框，
真正的窗口是他自己机器上的系统窗口，选中的绝对路径由服务端读回来交给前端固化进配置。

两个关键约束：

1. **同一时刻只允许一个窗口。** 弹出第二个窗口，用户根本分不清哪个算数；
   所以在窗口真正关掉之前，后续请求一律回「已经有一个窗口开着」。
2. **超时不等于取消。** 用户看半天是常态，超时只是这一轮 HTTP 请求没等到结果，
   窗口还开着，锁也还握着；等他关掉窗口，下一轮才能再开。

测试通过 ``opener`` 参数注入替代实现，绝不真的弹窗。
"""

from __future__ import annotations

import subprocess
import sys
import threading
from typing import Any, Callable

# 用户在文件夹树里翻半天很正常，但 HTTP 请求不能无限挂着一根连接。
DEFAULT_TIMEOUT_SECONDS = 600.0

# 开框实现：收初始目录，返回用户选中的路径；取消时返回空串。
Opener = Callable[[str], str]


class DirectoryPickerError(RuntimeError):
    """选择窗口无法打开。"""


# 用户点开窗口之后，键盘焦点在系统窗口那边；这里只保证同一时刻一个窗口。
_dialog_lock = threading.Lock()


def _run_tkinter(initial_dir: str) -> str:
    """用 Tk 的 filedialog 开框。

    Windows 上 Tk 走的是系统自己的目录浏览对话框，不是 Python 画出来的窗口；
    tkinter 随 CPython 一起装，不需要额外依赖。
    """
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    root.withdraw()
    try:
        # 服务多半由后台控制台启动，不置顶的话窗口会藏在浏览器后面，用户以为没反应。
        root.attributes("-topmost", True)
        root.update()
        options: dict[str, Any] = {}
        if initial_dir:
            options["initialdir"] = initial_dir
        return filedialog.askdirectory(title="选择存放目录工作簿的文件夹", **options)
    finally:
        root.destroy()


def _powershell_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_powershell(initial_dir: str) -> str:
    """没有 tkinter 时的退路：借 Windows 自带的 .NET 对话框。"""
    if sys.platform != "win32":  # pragma: no cover - 非 Windows 上不会走到
        raise DirectoryPickerError("本机没有可用的图形选择框（缺少 tkinter）")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null;"
        "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog;"
        f"$dialog.SelectedPath = {_powershell_quote(initial_dir)};"
        "$dialog.Description = '选择存放目录工作簿的文件夹';"
        "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
        "{ [Console]::Out.Write($dialog.SelectedPath) }"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-Command", script],
        capture_output=True, text=True, timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    return completed.stdout.strip()


def _open_dialog(initial_dir: str) -> str:
    """真正弹窗的入口，返回用户选的文件夹路径；取消时返回空串。"""
    try:
        return _run_tkinter(initial_dir)
    except ImportError:
        return _run_powershell(initial_dir)
    except Exception as error:  # TclError：没有图形会话、或 Tk 初始化失败
        raise DirectoryPickerError(f"打不开本机选择窗口：{error}") from error


def pick_path(
    *, initial_dir: str = "",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS, opener: Opener | None = None,
) -> dict[str, Any]:
    """开一次「选择文件夹」窗口，返回 ``{path, cancelled, busy, timed_out, message}``。"""
    open_dialog = opener or _open_dialog
    if not _dialog_lock.acquire(blocking=False):
        return {
            "path": "", "cancelled": True, "busy": True, "timed_out": False,
            "message": "已经有一个选择窗口开着，先在那边选完或关掉它。",
        }

    result: dict[str, Any] = {}

    def run() -> None:
        try:
            result["path"] = open_dialog(str(initial_dir or ""))
        except Exception as error:  # 窗口开不出来也只是这一次失败，不能拖垮分类服务
            result["error"] = str(error)
        finally:
            # 锁交给这条线程持有：窗口不关，后面就别再开新窗口。
            _dialog_lock.release()

    thread = threading.Thread(target=run, name="directory-picker", daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        return {
            "path": "", "cancelled": True, "busy": False, "timed_out": True,
            "message": "选择窗口还开着，没等到结果；选完或关掉那个窗口之后再点一次。",
        }
    if "error" in result:
        raise DirectoryPickerError(str(result["error"]))
    path = str(result.get("path") or "").strip()
    return {
        "path": path, "cancelled": not path, "busy": False, "timed_out": False,
        "message": "" if path else "没有选择任何文件夹。",
    }
