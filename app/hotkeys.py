"""全局热键的平台适配层。

Windows: keyboard 库(低层钩子,支持 suppress,不把热键漏给焦点应用)。
macOS : pynput(需要「辅助功能」和「输入监控」权限;无法 suppress,
        所以默认热键选不碍事的右 Option 键;修饰键按住不产生自动重复,
        对 hold 模式反而更干净)。
"""
import sys


class _WinHotkeys:
    def __init__(self):
        self._hooks = []

    def bind_many(self, bindings):
        """bindings: [(key, on_down, on_up 或 None)]。
        先全部绑新钩子,成功后再摘旧的——绑定失败不会落入"无热键"状态。"""
        import keyboard

        new = []
        for key, on_down, on_up in bindings:
            new.append(keyboard.on_press_key(
                key, lambda _e, f=on_down: f(), suppress=True))
            if on_up is not None:
                new.append(keyboard.on_release_key(
                    key, lambda _e, f=on_up: f(), suppress=True))
        old = self._hooks
        self._hooks = new
        for h in old:
            try:
                keyboard.unhook(h)
            except Exception:
                pass

    def unbind_all(self):
        import keyboard

        for h in self._hooks:
            try:
                keyboard.unhook(h)
            except Exception:
                pass
        self._hooks = []


# macOS 可选热键:配置值 -> (虚拟键码, 中文名, 修饰键flag掩码或None)
MAC_KEYS = {
    "right option":  (61, "右 Option 键", 0x00080000),
    "left option":   (58, "左 Option 键", 0x00080000),
    "right command": (54, "右 Command 键", 0x00100000),
    "right control": (62, "右 Control 键", 0x00040000),
    "right shift":   (60, "右 Shift 键", 0x00020000),
    "f6":  (97, "F6", None), "f7": (98, "F7", None), "f8": (100, "F8", None),
    "f9":  (101, "F9", None), "f10": (109, "F10", None), "f11": (103, "F11", None),
    "f12": (111, "F12", None), "f13": (105, "F13", None),
    "f14": (107, "F14", None), "f15": (113, "F15", None),
}


def mac_input_monitoring_ok(request=False):
    """检查(可选请求)macOS 输入监控权限——全局热键必需。"""
    try:
        import Quartz

        if request and hasattr(Quartz, "CGRequestListenEventAccess"):
            Quartz.CGRequestListenEventAccess()
        if hasattr(Quartz, "CGPreflightListenEventAccess"):
            return bool(Quartz.CGPreflightListenEventAccess())
    except Exception:
        pass
    return True  # 老系统没这套 API,当作已授权,交给 tap 创建自己失败


def mac_accessibility_ok(prompt=False):
    """检查(可选请求)macOS 辅助功能权限——模拟按键(粘贴)必需。"""
    try:
        from ApplicationServices import (
            AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt)

        return bool(AXIsProcessTrustedWithOptions(
            {kAXTrustedCheckOptionPrompt: bool(prompt)}))
    except Exception:
        try:
            from ApplicationServices import AXIsProcessTrusted
            return bool(AXIsProcessTrusted())
        except Exception:
            return True  # 拿不到 API 就不拦,交给实际操作自己失败


class _MacHotkeys:
    """Quartz CGEventTap 装在主线程 CFRunLoop 上,只读虚拟键码,
    不碰输入源接口(pynput 后台线程调 TIS 会触发 macOS 断言崩溃)。"""

    def __init__(self):
        self._tap = None
        self._src = None
        self._downs = {}
        self._ups = {}
        self._mask = {}  # keycode -> flag掩码(修饰键)

    def bind_many(self, bindings):
        import Quartz

        downs, ups, mods = {}, {}, {}
        for key, on_down, on_up in bindings:
            spec = MAC_KEYS.get(str(key).lower())
            if spec is None:
                raise ValueError(
                    f"macOS 不支持热键「{key}」,可在设置里另选一个")
            code, _label, mask = spec
            downs[code] = on_down
            if on_up is not None:
                ups[code] = on_up
            if mask is not None:
                mods[code] = mask
        self._downs, self._ups, self._mask = downs, ups, mods
        if self._tap is not None:
            return  # tap 已装,换绑只更新回调字典即可

        # 权限自检:没有"输入监控"授权,tap 收不到任何事件 = 按键无反应
        if not mac_input_monitoring_ok(request=True):
            raise PermissionError(
                "缺少「输入监控」权限。请在 系统设置 → 隐私与安全性 → 输入监控 "
                "里勾选「听晓」,然后重启听晓。")

        mask = ((1 << Quartz.kCGEventKeyDown) | (1 << Quartz.kCGEventKeyUp)
                | (1 << Quartz.kCGEventFlagsChanged))

        def callback(proxy, etype, event, refcon):
            try:
                code = Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGKeyboardEventKeycode)
                if etype == Quartz.kCGEventFlagsChanged:
                    m = self._mask.get(code)
                    if m is not None:
                        flags = Quartz.CGEventGetFlags(event)
                        down = bool(flags & m)
                        print(f"[hotkey] 修饰键 {code} {'按下' if down else '松开'}")
                        (self._downs if down else self._ups).get(code, lambda: None)()
                elif etype == Quartz.kCGEventKeyDown:
                    if code in self._downs:
                        print(f"[hotkey] 键 {code} 按下")
                    self._downs.get(code, lambda: None)()
                elif etype == Quartz.kCGEventKeyUp:
                    self._ups.get(code, lambda: None)()
            except Exception:
                import traceback
                traceback.print_exc()
            return event

        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly, mask, callback, None)
        if not tap:
            raise RuntimeError(
                "无法创建键盘监听(请在 系统设置→隐私与安全性→输入监控 里勾选听晓/终端)")
        src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        Quartz.CFRunLoopAddSource(
            Quartz.CFRunLoopGetMain(), src, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)
        self._tap, self._src = tap, src

    def unbind_all(self):
        self._downs, self._ups = {}, {}
        if self._tap is not None:
            try:
                import Quartz

                Quartz.CGEventTapEnable(self._tap, False)
                Quartz.CFRunLoopRemoveSource(
                    Quartz.CFRunLoopGetMain(), self._src,
                    Quartz.kCFRunLoopCommonModes)
            except Exception:
                pass
            self._tap = self._src = None
            self._listener = None


def make_hotkeys():
    if sys.platform == "darwin":
        return _MacHotkeys()
    return _WinHotkeys()
