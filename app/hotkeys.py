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
    def __init__(self):
        self._listener = None

    @staticmethod
    def _keymap():
        from pynput import keyboard as pk

        return {
            "right option": pk.Key.alt_r,
            "right command": pk.Key.cmd_r,
            "f9": pk.Key.f9,
            "f8": pk.Key.f8,
            "f10": pk.Key.f10,
        }

    def bind_many(self, bindings):
        """bindings: [(key, on_down, on_up 或 None)]。单个监听器分发多键。"""
        from pynput import keyboard as pk

        keymap = self._keymap()
        downs, ups = {}, {}
        for key, on_down, on_up in bindings:
            target = keymap.get(str(key).lower())
            if target is None:
                raise ValueError(
                    f"macOS 不支持热键「{key}」,可选:{'、'.join(keymap)}")
            downs[target] = on_down
            if on_up is not None:
                ups[target] = on_up
        listener = pk.Listener(
            on_press=lambda k: downs[k]() if k in downs else None,
            on_release=lambda k: ups[k]() if k in ups else None,
        )
        listener.start()
        old = self._listener
        self._listener = listener
        if old:
            try:
                old.stop()
            except Exception:
                pass

    def unbind_all(self):
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None


def make_hotkeys():
    if sys.platform == "darwin":
        return _MacHotkeys()
    return _WinHotkeys()
