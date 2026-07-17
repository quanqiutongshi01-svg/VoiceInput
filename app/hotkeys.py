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


class _MacHotkeys:
    """Quartz CGEventTap 装在主线程的 CFRunLoop 上,只读虚拟键码,
    不碰输入源接口(pynput 在后台线程调 TIS 会触发 macOS 队列断言崩溃)。
    只支持修饰键(右Option/右Command)和功能键——足够听晓用。"""

    # macOS 虚拟键码
    KEYCODE = {
        "right option": 61, "right command": 54,
        "f9": 101, "f8": 100, "f10": 109, "f7": 98,
    }
    # 修饰键在 flagsChanged 事件里对应的 flag 掩码(判断按下/松开)
    MOD_MASK = {61: 0x00080000, 54: 0x00100000}  # alternate / command

    def __init__(self):
        self._tap = None
        self._src = None
        self._downs = {}
        self._ups = {}

    def bind_many(self, bindings):
        import Quartz

        downs, ups = {}, {}
        for key, on_down, on_up in bindings:
            code = self.KEYCODE.get(str(key).lower())
            if code is None:
                raise ValueError(
                    f"macOS 不支持热键「{key}」,可选:{'、'.join(self.KEYCODE)}")
            downs[code] = on_down
            if on_up is not None:
                ups[code] = on_up
        self._downs, self._ups = downs, ups
        if self._tap is not None:
            return  # tap 已装,换绑只更新回调字典即可

        mask = ((1 << Quartz.kCGEventKeyDown) | (1 << Quartz.kCGEventKeyUp)
                | (1 << Quartz.kCGEventFlagsChanged))

        def callback(proxy, etype, event, refcon):
            try:
                code = Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGKeyboardEventKeycode)
                if etype == Quartz.kCGEventFlagsChanged:
                    m = self.MOD_MASK.get(code)
                    if m is not None:
                        flags = Quartz.CGEventGetFlags(event)
                        (self._downs if (flags & m) else self._ups).get(
                            code, lambda: None)()
                elif etype == Quartz.kCGEventKeyDown:
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
