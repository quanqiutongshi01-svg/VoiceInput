"""把文本注入到当前光标位置。

Windows 主路径: SendInput + KEYEVENTF_UNICODE(不占剪贴板)。
Windows 回退路径: 剪贴板 + Ctrl V(带重试;会先快照原剪贴板文本并在粘贴后恢复,
但只能恢复纯文本,图片/文件等格式无法恢复)。
macOS(开发调试用): osascript keystroke。
注意:注入不进以管理员权限运行的窗口(UIPI 限制),日常应用均正常。
"""
import sys
import time


def make_injector(method: str, clipboard_fallback: bool):
    if sys.platform == "win32":
        return WindowsInjector(method, clipboard_fallback)
    if sys.platform == "darwin":
        return MacInjector()
    return PrintInjector()


class PrintInjector:
    def inject(self, text: str):
        print(f"[inject] {text}")


class MacInjector(PrintInjector):
    """原生 NSPasteboard 写剪贴板 + Cmd+V 粘贴。用 AppKit 不依赖 locale,
    彻底避免独立 App 无 UTF-8 环境时 pbcopy 把中文写成乱码。
    需要 辅助功能 权限(粘贴按键)。0.6s 后恢复原剪贴板文本。"""

    def _pb(self):
        from AppKit import NSPasteboard
        return NSPasteboard.generalPasteboard()

    def _get_text(self):
        try:
            from AppKit import NSPasteboardTypeString
            return self._pb().stringForType_(NSPasteboardTypeString)
        except Exception:
            return None

    def _set_text(self, text):
        from AppKit import NSPasteboardTypeString
        pb = self._pb()
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)

    def inject(self, text: str):
        import subprocess
        import threading

        try:
            old = self._get_text()
            self._set_text(text)
        except Exception as e:
            print(f"[inject] 写剪贴板失败: {e}")
            return
        time.sleep(0.05)
        subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to keystroke "v" using {command down}'],
            check=False)

        def restore():
            time.sleep(0.6)
            if old is not None:
                try:
                    self._set_text(old)
                except Exception:
                    pass

        threading.Thread(target=restore, daemon=True).start()


class WindowsInjector:
    def __init__(self, method: str, clipboard_fallback: bool):
        self.method = method
        self.clipboard_fallback = clipboard_fallback

    def inject(self, text: str):
        if self.method == "clipboard":
            self._safe_clipboard(text)
            return
        try:
            self._send_unicode(text)
        except Exception as e:
            print(f"[inject] SendInput 失败: {e}")
            if self.clipboard_fallback:
                self._safe_clipboard(text)

    def _safe_clipboard(self, text: str):
        try:
            self._via_clipboard(text)
        except Exception as e:
            # 任何异常都不能穿透到调用方线程
            print(f"[inject] 剪贴板注入失败,本次文本: {text}")
            print(f"         错误: {e}")

    # ---- SendInput unicode ----

    def _send_unicode(self, text: str):
        import ctypes
        from ctypes import wintypes

        KEYEVENTF_UNICODE = 0x0004
        KEYEVENTF_KEYUP = 0x0002
        ULONG_PTR = ctypes.POINTER(ctypes.c_ulong)

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR),
            ]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR),
            ]

        # union 必须包含最大的成员 MOUSEINPUT,否则 sizeof(INPUT) 偏小,SendInput 会静默失败
        class _INPUTunion(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("union", _INPUTunion)]

        INPUT_KEYBOARD = 1
        inputs = []
        # 按 UTF-16 code unit 逐个发送(支持 BMP 外字符的代理对)
        raw = text.encode("utf-16-le")
        units = [raw[i : i + 2] for i in range(0, len(raw), 2)]
        for u in units:
            code = int.from_bytes(u, "little")
            for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                inp = INPUT()
                inp.type = INPUT_KEYBOARD
                inp.union.ki = KEYBDINPUT(0, code, flags, 0, None)
                inputs.append(inp)
        arr = (INPUT * len(inputs))(*inputs)
        sent = ctypes.windll.user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
        if sent != len(inputs):
            raise OSError(f"SendInput 只发送了 {sent}/{len(inputs)}")

    # ---- 剪贴板回退 ----

    @staticmethod
    def _winapi():
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.restype = wintypes.HGLOBAL
        return ctypes, user32, kernel32

    @classmethod
    def _open_clipboard_retry(cls, user32, tries=10, wait=0.03):
        for _ in range(tries):
            if user32.OpenClipboard(None):
                return True
            time.sleep(wait)
        return False

    @classmethod
    def _set_clipboard_text(cls, text: str) -> bool:
        """写文本进剪贴板。返回是否成功。调用方负责已 OpenClipboard。"""
        ctypes, user32, kernel32 = cls._winapi()
        GMEM_MOVEABLE = 0x0002
        CF_UNICODETEXT = 13
        data = text.encode("utf-16-le") + b"\x00\x00"
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        p = kernel32.GlobalLock(h)
        if not p:
            kernel32.GlobalFree(h)
            return False
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)  # 设置失败时所有权仍在本进程,须释放
            return False
        return True  # 成功后句柄归系统,不得再 GlobalFree

    def _via_clipboard(self, text: str):
        ctypes, user32, kernel32 = self._winapi()
        CF_UNICODETEXT = 13

        if not self._open_clipboard_retry(user32):
            print("[inject] 剪贴板被其他程序占用,放弃。本次文本:")
            print(f"         {text}")
            return
        old_text = None
        ok = False
        try:
            # 快照旧文本(必须在 EmptyClipboard 前复制出来)
            h_old = user32.GetClipboardData(CF_UNICODETEXT)
            if h_old:
                p_old = kernel32.GlobalLock(h_old)
                if p_old:
                    old_text = ctypes.wstring_at(p_old)
                    kernel32.GlobalUnlock(h_old)
            ok = self._set_clipboard_text(text)
        finally:
            user32.CloseClipboard()
        if not ok:
            print("[inject] 写入剪贴板失败,本次文本:")
            print(f"         {text}")
            return

        import keyboard

        time.sleep(0.05)
        keyboard.send("ctrl+v")

        if old_text is not None:
            def restore():
                time.sleep(0.6)  # 等目标应用完成粘贴
                _, u32, _k = self._winapi()
                if self._open_clipboard_retry(u32):
                    try:
                        self._set_clipboard_text(old_text)
                    finally:
                        u32.CloseClipboard()

            import threading

            threading.Thread(target=restore, daemon=True).start()


def grab_selection() -> str:
    """模拟复制读取当前选中文本(润色热键用)。取不到返回空串。"""
    if sys.platform == "darwin":
        import subprocess

        subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to keystroke "c" using {command down}'],
            check=False)
        time.sleep(0.3)
        out = subprocess.run(["pbpaste"], capture_output=True)
        return out.stdout.decode("utf-8", "replace")
    if sys.platform == "win32":
        import keyboard

        keyboard.send("ctrl+c")
        time.sleep(0.3)
        ctypes, user32, kernel32 = WindowsInjector._winapi()
        CF_UNICODETEXT = 13
        if not WindowsInjector._open_clipboard_retry(user32):
            return ""
        try:
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ""
            p = kernel32.GlobalLock(h)
            if not p:
                return ""
            text = ctypes.wstring_at(p)
            kernel32.GlobalUnlock(h)
            return text
        finally:
            user32.CloseClipboard()
    return ""
