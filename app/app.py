#!/usr/bin/env python3
"""语音输入 v2 — 图形界面版(悬浮条 + 托盘 + 设置,流式草稿上屏)。

用法:
  python app.py            GUI 常驻(pythonw 下无控制台,日志写 logs/voiceinput.log)
  python main.py --test x  流水线离线测试(命令行,见 main.py)

线程模型:
  keyboard 钩子线程  --Signal(Queued)-->  Qt 主线程(状态机/UI)
  音频回调线程  --draft 队列(常驻,ndarray=音频块 / tuple=会话标记)-->  常驻草稿线程
  终稿:单工作线程消费 jobs 队列(SenseVoice+标点+纠错+注入+存档)
  所有 UI 信号携带会话代数 gen,过期会话的 partial/done/error 不再触碰悬浮条。
"""
import json
import os
import queue
import sys
import threading
import time
import traceback

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
sys.path.insert(0, BASE)

LOG_DIR = os.path.join(BASE, "logs")
LOG_PATH = os.path.join(LOG_DIR, "voiceinput.log")
_LOG_FILE = None


def _native_msgbox(text, title="听晓"):
    """Qt 起不来时的兜底弹窗(仅 Windows)。"""
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)
        except Exception:
            pass


class _LogFile:
    """带大小上限的日志文件:超 5MB 时轮转到 .old(运行中也生效)。"""

    LIMIT = 5 * 1048576

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._open()

    def _open(self):
        self.f = open(self.path, "a", encoding="utf-8", errors="replace")
        self.size = self.f.tell()

    def write(self, s):
        with self._lock:
            self.f.write(s)
            self.f.flush()
            self.size += len(s)
            if self.size > self.LIMIT:
                try:
                    self.f.close()
                    os.replace(self.path, self.path + ".old")
                except Exception:
                    pass
                self._open()

    def flush(self):
        pass


def _setup_logging():
    global _LOG_FILE
    os.makedirs(LOG_DIR, exist_ok=True)
    _LOG_FILE = _LogFile(LOG_PATH)
    _LOG_FILE.write(f"\n===== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")

    class Tee:
        def __init__(self, *targets):
            self.targets = [t for t in targets if t is not None]

        def write(self, s):
            for t in self.targets:
                try:
                    t.write(s)
                except Exception:
                    pass

        def flush(self):
            pass

    console = sys.stdout if (sys.stdout and hasattr(sys.stdout, "write")) else None
    sys.stdout = Tee(console, _LOG_FILE)
    sys.stderr = Tee(console, _LOG_FILE)
    try:
        import faulthandler

        faulthandler.enable(file=_LOG_FILE.f, all_threads=True)
    except Exception:
        pass


try:
    _setup_logging()
except Exception as e:  # 日志都建不起来:目录只读等
    _native_msgbox(f"无法创建日志目录:{e}\n请把 VoiceInput 放到可写的位置(如 D 盘)再运行。")
    sys.exit(1)


def _single_instance_guard():
    """防止双击两次 → 两个键盘钩子、每句注入两遍。"""
    if sys.platform != "win32":
        return None
    import ctypes

    handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\VoiceInputLeoMutex")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        _native_msgbox("听晓已经在运行了(看屏幕右下角托盘)。")
        sys.exit(0)
    return handle  # 保持引用,进程退出自动释放


_MUTEX = _single_instance_guard()

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6 import QtCore
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QSystemTrayIcon,
    QVBoxLayout, QWidget,
)


def _qt_msg_handler(mode, ctx, message):
    try:
        _LOG_FILE.write(f"[qt:{mode}] {message}\n")
    except Exception:
        pass


QtCore.qInstallMessageHandler(_qt_msg_handler)


def _check_qt_platform_plugin():
    """QApplication 构造前预检平台插件,缺失时 C++ 层会 abort,必须提前拦。"""
    import PySide6

    base = os.path.dirname(PySide6.__file__)
    name = {"win32": "qwindows.dll", "darwin": "libqcocoa.dylib"}.get(sys.platform)
    if not name:
        return
    for sub in ("plugins", os.path.join("Qt", "plugins")):
        if os.path.isfile(os.path.join(base, sub, "platforms", name)):
            return
    print(f"[app] 致命:PySide6 平台插件缺失({name})")
    if sys.platform == "win32":
        _native_msgbox(f"界面组件不完整,无法启动。\n请重新完整解压 VoiceInput。\n日志:{LOG_PATH}")
        sys.exit(1)


from main import load_config  # noqa: E402
from asr import resample_to_16k  # noqa: E402
from sounds import play  # noqa: E402  柔和提示音(带蜂鸣兜底)


# 界面层(悬浮条/设置窗口/动画控件/历史)在 ui.py
from ui import Overlay, SettingsDialog, HistoryDialog  # noqa: E402

VERSION = "3.5.4"


# ---------- 信号桥:非 Qt 线程 → Qt 主线程(int 均为会话代数,-1=应用级) ----------

class Bridge(QObject):
    pressed = Signal()
    released = Signal()
    partial = Signal(int, str)
    done = Signal(int, str, bool)  # gen, 文本, 是否经LLM润色
    error = Signal(int, str)
    status = Signal(str)
    notify = Signal(str, str, int)  # 托盘气泡(标题, 内容, 毫秒)——worker线程安全
    update_found = Signal(dict)     # 远程发现新版本
    polish = Signal()               # 润色热键
    busy = Signal(str)              # 悬浮条忙碌提示
    xfer_in = Signal(str, str, str) # 收到快传(kind, from, payload)
    xfer_peers = Signal(list)       # 在线设备表变化


# ---------- 主控 ----------

class VoiceInputApp:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        try:
            self.cfg = load_config()  # 失败时它会 print + sys.exit
        except SystemExit:
            raise RuntimeError(
                "config.json 缺失或格式有误(多半是最近一次修改改坏了),"
                "可找 Leo 要一份原始 config.json"
            ) from None
        self.cfg.setdefault("ui", {})

        self.bridge = Bridge()
        self.overlay = Overlay(self.cfg["ui"], get_level=lambda: getattr(getattr(self, "recorder", None), "level", 0.0))
        self._build_tray()

        self.bridge.pressed.connect(self._on_press, Qt.QueuedConnection)
        self.bridge.released.connect(self._on_release, Qt.QueuedConnection)
        self.bridge.partial.connect(self._on_partial, Qt.QueuedConnection)
        self.bridge.done.connect(self._on_done, Qt.QueuedConnection)
        self.bridge.error.connect(self._on_error, Qt.QueuedConnection)
        self.bridge.status.connect(self._set_tray_tip, Qt.QueuedConnection)
        self.bridge.notify.connect(
            lambda t, m, ms: self.tray.showMessage(t, m, QSystemTrayIcon.Information, ms),
            Qt.QueuedConnection)
        self.bridge.update_found.connect(self._on_update_found, Qt.QueuedConnection)
        self.bridge.polish.connect(self._do_polish, Qt.QueuedConnection)
        self.bridge.busy.connect(self.overlay.show_busy, Qt.QueuedConnection)
        self.bridge.xfer_in.connect(self._on_xfer_in, Qt.QueuedConnection)
        self.bridge.xfer_peers.connect(self._on_xfer_peers, Qt.QueuedConnection)

        self.ready = False
        self.recording = False
        self.t0 = 0.0
        self._key_down = False   # toggle 模式:过滤按住时的自动重复
        self._key_down_t = 0.0   # 上一次 down 的单调时钟
        self._watch = QTimer()   # toggle 模式:录满上限自动收束
        self._watch.setInterval(1000)
        self._watch.timeout.connect(self._watch_tick)
        self._pending_update = None  # 远程发现的新版本 info
        self._polishing = False      # 润色单飞标志
        self._xfer = None            # TransferService
        self._xfer_dialog = None
        self._update_timer = QTimer()  # 每24小时静默再查一次
        self._update_timer.setInterval(24 * 3600 * 1000)
        self._update_timer.timeout.connect(
            lambda: threading.Thread(target=self._check_remote_update, daemon=True).start())
        self._update_timer.start()
        self._gen = 0            # 会话代数:每次按下+1
        self._hooks = []         # keyboard 钩子句柄
        self._loading = False
        self._workers_started = False
        self._draft_q = queue.Queue(maxsize=512)   # 常驻:ndarray=音频块, tuple=(标记, gen)
        self._jobs = queue.Queue()

        QTimer.singleShot(50, self._load_engines)

    # -- 初始化(可重入:启动失败后从设置窗口重试) --

    def _load_engines(self):
        if self._loading or self.ready:
            return
        self._loading = True

        def load():
            try:
                from asr import Recognizer
                from corrector import Corrector
                from archiver import Archiver
                from injector import make_injector
                from recorder import Recorder
                from streaming import StreamingDraft

                print("[app] 加载识别模型...")
                self.rec = Recognizer(self.cfg.get("models_dir", "models"),
                                      punctuation=self.cfg.get("punctuation", True),
                                      language=self.cfg.get("language", "auto"))
                try:
                    self.draft = StreamingDraft(self.cfg.get("models_dir", "models"))
                    print(f"[app] 流式草稿引擎: {self.draft.kind}")
                except Exception as e:
                    self.draft = None
                    print(f"[app] 流式模型缺失,退化为松键后出字: {e}")
                self.cor = Corrector(self.cfg.get("llm"), self.cfg.get("hotwords"), self.cfg.get("glossary"))
                self.arc = Archiver(self.cfg.get("archive"))
                self.injector = make_injector(
                    self.cfg.get("inject_method", "sendinput"),
                    self.cfg.get("inject_fallback_clipboard", True),
                )
                self.recorder = Recorder(
                    self.cfg.get("mic_name_contains", ""),
                    max_seconds=float(self.cfg.get("max_speech_seconds", 60)),
                    persistent=self.cfg.get("persistent_mic", True),
                )
                self.recorder.open()
                self._bind_hotkey(self.cfg.get("hotkey", "f9"))
                if not self._workers_started:
                    self._workers_started = True
                    threading.Thread(target=self._final_worker, daemon=True).start()
                    threading.Thread(target=self._draft_worker, daemon=True).start()
                    self._start_transfer()
                self.ready = True
                hk = self.cfg.get("hotkey", "f9").upper()
                self.bridge.status.emit(f"就绪 — 按住 {hk} 说话")
                print("[app] 就绪")
                self.bridge.notify.emit(
                    "听晓已就绪", f"按住 {hk} 说话,松开出字。图标可能收在托盘 ^ 里。", 4000)
                self.bridge.done.emit(-1, f"听晓已启动 · 按住 {hk} 说话", False)
                self._run_flywheel()
                self._check_remote_update()
            except Exception as e:
                traceback.print_exc()
                self.bridge.error.emit(-1, f"启动失败: {e}")
                self.bridge.status.emit("启动失败 — 右键托盘可打开日志/设置")
            finally:
                self._loading = False

        threading.Thread(target=load, daemon=True).start()

    def _bind_hotkey(self, hotkey):
        from hotkeys import make_hotkeys

        if not hasattr(self, "_hk"):
            self._hk = make_hotkeys()
        bindings = [(hotkey,
                     lambda: self.bridge.pressed.emit(),
                     lambda: self.bridge.released.emit())]
        pk = self.cfg.get("polish_hotkey", "")
        if pk and pk != "off" and pk != hotkey:
            bindings.append((pk, lambda: self.bridge.polish.emit(), None))
        self._hk.bind_many(bindings)
        self._key_down = False  # 重绑即视为无键按下(丢失的 release 在此清账)

    def _unbind_hotkey(self):
        if hasattr(self, "_hk"):
            self._hk.unbind_all()

    # -- 托盘 --

    def _tray_icon(self, color):
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawRoundedRect(22, 8, 20, 32, 10, 10)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(color), 5))
        p.drawArc(14, 22, 36, 26, 180 * 16, 180 * 16)
        p.drawLine(32, 48, 32, 56)
        p.end()
        return QIcon(pm)

    def _build_tray(self):
        # 菜单和 QAction 必须挂在 self 上,否则被 Python GC 回收后菜单失效
        self.tray = QSystemTrayIcon(self._tray_icon("#4cd964"))
        self._menu = QMenu()
        self._tip_action = QAction("正在启动…")
        self._tip_action.setEnabled(False)
        self._menu.addAction(self._tip_action)
        self._menu.addSeparator()
        self._act_settings = QAction("设置…")
        self._act_settings.triggered.connect(self._open_settings)
        self._menu.addAction(self._act_settings)
        self._act_log = QAction("打开日志")
        self._act_log.triggered.connect(self._open_log)
        self._menu.addAction(self._act_log)
        self._act_xfer = QAction("家庭快传…")
        self._act_xfer.triggered.connect(self._open_transfer)
        self._menu.addAction(self._act_xfer)
        self._menu.addSeparator()
        self._act_history = QAction("最近听写…")
        self._act_history.triggered.connect(self._open_history)
        self._menu.addAction(self._act_history)
        self._act_export = QAction("导出个人语音包")
        self._act_export.triggered.connect(self._export_profile)
        self._menu.addAction(self._act_export)
        self._act_rime = QAction("安装键盘输入法(小狼毫)…")
        self._act_rime.setVisible(sys.platform == "win32")
        self._act_rime.triggered.connect(self._install_keyboard_ime)
        self._menu.addAction(self._act_rime)
        self._act_checkupd = QAction("检查更新")
        self._act_checkupd.triggered.connect(self._check_update_now)
        self._menu.addAction(self._act_checkupd)
        self._act_update = QAction("从文件安装更新包…")
        self._act_update.triggered.connect(self._install_update)
        self._menu.addAction(self._act_update)
        self._act_upgrade = QAction("")  # 发现新版本时才可见
        self._act_upgrade.setVisible(False)
        self._act_upgrade.triggered.connect(self._upgrade_now)
        self._menu.addAction(self._act_upgrade)
        self._act_llm = QAction("云端润色(LLM)")
        self._act_llm.setCheckable(True)
        llm0 = self.cfg.get("llm") or {}
        self._act_llm.setChecked(bool(llm0.get("enabled")) and bool(llm0.get("api_key")))
        self._act_llm.triggered.connect(self._toggle_llm)
        self._menu.addAction(self._act_llm)
        self._menu.addSeparator()
        self._act_quit = QAction("退出")
        self._act_quit.triggered.connect(self._quit)
        self._menu.addAction(self._act_quit)
        self.tray.setContextMenu(self._menu)
        self.tray.setToolTip("听晓")
        self.tray.show()

    def _set_tray_tip(self, text):
        self._tip_action.setText(text)
        self.tray.setToolTip(f"听晓 — {text}")

    def _open_log(self):
        try:
            if sys.platform == "win32":
                os.startfile(LOG_PATH)  # noqa
            else:
                import subprocess

                subprocess.Popen(["open", LOG_PATH])
        except Exception:
            try:  # .log 没有关联程序时用记事本兜底
                import subprocess

                subprocess.Popen(["notepad.exe", LOG_PATH])
            except Exception as e:
                print(f"[app] 打开日志失败: {e}")

    # -- 设置 --

    def _open_settings(self):
        # 对话框期间:丢弃进行中的录音、摘掉热键,防止嵌套事件循环里互相踩踏
        self._abort_recording()
        self._unbind_hotkey()

        devices = []
        try:
            from recorder import list_input_devices, refresh_devices

            if getattr(self, "recorder", None) is not None and self.ready:
                self.recorder._close()
            refresh_devices()  # 刷新 PortAudio 设备表,新连的蓝牙耳机才会出现
            devices = list_input_devices()
        except Exception:
            traceback.print_exc()

        _d = (self.cfg.get("archive") or {}).get("dir", "data")
        dlg = SettingsDialog(self.cfg, devices,
                             data_dir=_d if os.path.isabs(_d) else os.path.join(BASE, _d),
                             exe_path=os.path.abspath(os.path.join(BASE, "..", "VoiceInput.exe")),
                             on_reset_overlay=self.overlay.reset_position,
                             version=f"v{VERSION}")
        accepted = dlg.exec() == QDialog.Accepted
        if accepted:
            try:
                dlg.apply_to(self.cfg)
                self._save_config()
            except Exception as e:
                traceback.print_exc()
                self.overlay.show_error(f"设置保存失败: {e}")

        if not self.ready:
            # 首次启动失败:带新配置整体重试
            self._load_engines()
            return
        try:
            from corrector import Corrector

            self.cor = Corrector(self.cfg.get("llm"), self.cfg.get("hotwords"), self.cfg.get("glossary"))
            self.recorder.name_contains = self.cfg.get("mic_name_contains", "")
            self.recorder.persistent = self.cfg.get("persistent_mic", True)
            self.recorder.open()
            self._bind_hotkey(self.cfg.get("hotkey", "f9"))
            self.bridge.status.emit(f"就绪 — 按住 {self.cfg.get('hotkey','f9').upper()} 说话")
        except Exception as e:
            traceback.print_exc()
            # 至少把热键绑回去,不能让程序失聪
            try:
                self._bind_hotkey(self.cfg.get("hotkey", "f9"))
            except Exception:
                pass
            self.overlay.show_error(f"应用设置失败: {e}")

    def _save_config(self):
        path = os.path.join(BASE, "config.json")
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)  # 原子替换,崩溃不会留半截 JSON
        except Exception as e:
            print(f"[app] 保存配置失败: {e}")
            self.overlay.show_error("配置保存失败(目录只读?),本次修改重启后失效")

    def _run_flywheel(self):
        """越用越准:从使用记录挖掘个人热词,升级为本地即时替换(不联网)。"""
        try:
            from flywheel import mine, merge_into

            arch = self.cfg.get("archive") or {}
            rec_path = os.path.join(arch.get("dir", "data"), "records.jsonl")
            if not os.path.isfile(rec_path):
                return
            n = sum(1 for _ in open(rec_path, encoding="utf-8"))
            meta = self.cfg.setdefault("flywheel", {})
            if n - int(meta.get("mined_lines", 0)) < 50:
                return  # 攒够50条新记录才挖一次
            new, total = mine(rec_path, self.cfg.get("hotwords") or {})
            added = merge_into(self.cfg, new) if new else 0
            meta["mined_lines"] = n
            self._save_config()
            if added:
                from corrector import Corrector

                self.cor = Corrector(self.cfg.get("llm"), self.cfg.get("hotwords"),
                                     self.cfg.get("glossary"))
                print(f"[flywheel] 从 {total} 条使用记录中学会 {added} 个新词形: {new}")
                try:
                    self.tray.showMessage("越用越准",
                                          f"从使用记录中学会了 {added} 个新词",
                                          QSystemTrayIcon.Information, 3000)
                except Exception:
                    pass
        except Exception:
            traceback.print_exc()

    def _toggle_llm(self, checked):
        llm = self.cfg.setdefault("llm", {})
        if checked and not llm.get("api_key"):
            self._act_llm.setChecked(False)
            self.overlay.show_error("先在「设置」里填 API Key 才能开启云端润色")
            return
        llm["enabled"] = bool(checked)
        try:
            from corrector import Corrector

            self.cor = Corrector(llm, self.cfg.get("hotwords"), self.cfg.get("glossary"))
        except Exception:
            traceback.print_exc()
        self._save_config()
        self._llm_degraded = False
        state = "云端润色:开" if checked else "云端润色:关(纯本地)"
        print(f"[app] {state}")
        try:
            self.tray.showMessage("听晓", state, QSystemTrayIcon.Information, 2000)
        except Exception:
            pass

    def _on_update_found(self, info):
        self._pending_update = info
        v = info.get("version", "")
        self._act_upgrade.setText(f"立即更新到 v{v}…")
        self._act_upgrade.setVisible(True)
        self.bridge.status.emit(f"有新版本 v{v} — 右键托盘「立即更新」")
        self.tray.showMessage(
            "发现新版本", f"v{v} 已发布:{info.get('notes','')[:60]}\n"
            "右键托盘图标 →「立即更新」,一分钟搞定。",
            QSystemTrayIcon.Information, 8000)

    def _upgrade_now(self):
        """从 GitHub 下载新版并安装(下载到临时文件后走本地安装同一条路)。"""
        info = self._pending_update
        if not info or not info.get("url"):
            return
        self._abort_recording()
        self._unbind_hotkey()
        try:
            import tempfile
            import updater

            self.bridge.status.emit("正在下载更新…")
            tmp = os.path.join(tempfile.gettempdir(), "voiceinput_upgrade.zip")
            try:
                with updater._urlopen(info["url"], timeout=120) as r, open(tmp, "wb") as f:
                    import shutil as _sh

                    _sh.copyfileobj(r, f)
            except Exception as e:
                traceback.print_exc()
                QMessageBox.warning(None, "下载失败",
                                    f"新版本下载失败:{e}\n稍后再试,或让 Leo 直接发包。")
                return
        finally:
            try:
                self._bind_hotkey(self.cfg.get("hotkey", "f9"))
            except Exception:
                traceback.print_exc()
        # 复用本地安装流程(确认框/备份/配置合并/重启提示都在里面)
        self._install_update(zip_path=tmp)

    def _start_transfer(self):
        try:
            from transfer import TransferService

            tc = self.cfg.setdefault("transfer", {})
            if not tc.get("device_id"):
                import uuid

                tc["device_id"] = uuid.uuid4().hex[:12]
                self._save_config()
            self._xfer = TransferService(
                tc,
                on_incoming=lambda k, f, p: self.bridge.xfer_in.emit(k, f, p),
                on_peers=lambda ps: self.bridge.xfer_peers.emit(ps))
            self._xfer.start()
            if self._xfer.error:
                self.bridge.notify.emit("快传启动异常", self._xfer.error, 6000)
        except Exception:
            traceback.print_exc()

    def _open_transfer(self):
        if not self._xfer or getattr(self._xfer, "error", ""):
            msg = getattr(self._xfer, "error", "") if self._xfer else "快传服务未就绪"
            QMessageBox.information(None, "家庭快传", msg or "快传服务未就绪")
            return
        from transfer_ui import QuickTransferDialog

        if self._xfer_dialog is None:
            self._xfer_dialog = QuickTransferDialog(self._xfer)
        self._xfer_dialog.refresh_peers(self._xfer.peers())
        self._xfer_dialog.show()
        self._xfer_dialog.raise_()
        self._xfer_dialog.activateWindow()

    def _on_xfer_peers(self, peers):
        if self._xfer_dialog and self._xfer_dialog.isVisible():
            self._xfer_dialog.refresh_peers(peers)

    def _on_xfer_in(self, kind, frm, payload):
        if kind == "text":
            try:
                from PySide6.QtWidgets import QApplication as _QA

                _QA.clipboard().setText(payload)
            except Exception:
                pass
            preview = (payload[:50] + "…") if len(payload) > 50 else payload
            self.bridge.notify.emit(
                f"{frm} 发来文字", preview + "  (已复制到剪贴板)", 6000)
            self.overlay.show_done(f"{frm}发来文字,已复制", False)
        else:
            import os as _os

            name = _os.path.basename(payload)
            self.bridge.notify.emit(
                f"{frm} 发来文件", f"{name}\n已存到快传文件夹", 6000)
            self.overlay.show_done(f"收到文件:{name}", False)

    def _do_polish(self):
        """润色热键:取选中文字→热词+LLM修正→原地替换(覆盖选区)。"""
        if not self.ready or self._polishing or self.recording:
            return
        self._polishing = True

        def work():
            try:
                from injector import grab_selection

                text = grab_selection()
                if not text or not text.strip():
                    self.bridge.error.emit(-1, "先选中要润色的文字,再按润色键")
                    return
                if len(text) > 2000:
                    self.bridge.error.emit(-1, "选中内容太长(超过2000字)")
                    return
                self.bridge.busy.emit("润色中…")
                fixed = self.cor.polish(text.strip())
                if fixed == text.strip():
                    self.bridge.done.emit(-1, "已检查,没有需要修改的地方", self.cor.last_llm_used)
                    return
                self.injector.inject(fixed)  # 润色不产生音频,不入数据飞轮
                self.bridge.done.emit(-1, fixed, self.cor.last_llm_used)
            except Exception:
                traceback.print_exc()
                self.bridge.error.emit(-1, "润色失败,详情见日志")
            finally:
                self._polishing = False

        threading.Thread(target=work, daemon=True).start()

    def _install_keyboard_ime(self):
        """一键安装 RIME 小狼毫 + 写入听晓个人词库/口音容错 + 部署。"""
        ret = QMessageBox.question(
            None, "安装键盘输入法",
            "将下载并安装开源输入法「小狼毫(RIME)」(约15MB,官方渠道),\n"
            "并自动写入你的个人词库和口音容错配置。\n\n"
            "安装时 Windows 会弹权限确认,请选「是」。继续吗?",
            QMessageBox.Yes | QMessageBox.No)
        if ret != QMessageBox.Yes:
            return

        def work():
            try:
                import glob
                import subprocess
                import tempfile

                import updater

                self.bridge.busy.emit("正在下载小狼毫…")
                with updater._urlopen(
                        "https://api.github.com/repos/rime/weasel/releases/latest",
                        timeout=15) as r:
                    rel = json.loads(r.read().decode("utf-8"))
                exes = [a["browser_download_url"] for a in rel.get("assets", [])
                        if a.get("name", "").endswith(".exe")]
                if not exes:
                    self.bridge.error.emit(-1, "没找到小狼毫安装包,稍后再试")
                    return
                tmp = os.path.join(tempfile.gettempdir(), "weasel-setup.exe")
                with updater._urlopen(exes[0], timeout=300) as r, open(tmp, "wb") as f:
                    import shutil as _sh

                    _sh.copyfileobj(r, f)
                self.bridge.busy.emit("正在安装(请在弹窗中确认)…")
                # NSIS 静默参数 /S;若安装器不支持会转为交互界面,由用户点完
                subprocess.run([tmp, "/S"], timeout=900)
                self.bridge.busy.emit("正在写入个人词库…")
                import rime_export

                dst, n = rime_export.export(os.path.join(BASE, "config.json"))
                # 尝试自动部署(找 WeaselDeployer)
                deployed = False
                for pat in (r"C:\Program Files (x86)\Rime\weasel*\WeaselDeployer.exe",
                            r"C:\Program Files\Rime\weasel*\WeaselDeployer.exe"):
                    hits = glob.glob(pat)
                    if hits:
                        subprocess.run([hits[-1], "/deploy"], timeout=120)
                        deployed = True
                        break
                self.bridge.done.emit(
                    -1, f"小狼毫安装完成,已写入 {n} 个个人词条"
                        + ("" if deployed else "(请在输入法菜单点一次「重新部署」)"), False)
                self.bridge.notify.emit(
                    "键盘输入法就绪",
                    "按 Win+空格 切换到「小狼毫」。打 claude 出 Claude,支持口音容错。", 8000)
            except Exception:
                traceback.print_exc()
                self.bridge.error.emit(-1, "键盘输入法安装失败,详情见日志")

        threading.Thread(target=work, daemon=True).start()

    def _open_history(self):
        # 全局热键在模态对话框期间照样生效,识别结果会注入进对话框——先摘掉
        self._abort_recording()
        self._unbind_hotkey()
        try:
            _d = (self.cfg.get("archive") or {}).get("dir", "data")
            if not os.path.isabs(_d):
                _d = os.path.join(BASE, _d)
            dlg = HistoryDialog(os.path.join(_d, "records.jsonl"))
            dlg.exec()
        finally:
            try:
                self._bind_hotkey(self.cfg.get("hotkey", "f9"))
            except Exception:
                traceback.print_exc()

    def _install_update(self, _checked=False, zip_path=None):
        from PySide6.QtWidgets import QFileDialog

        import updater

        # 防护:丢弃录音、摘热键(写盘期间异步播放 sounds/*.wav 也会和更新冲突)
        self._abort_recording()
        self._unbind_hotkey()
        quitting = False
        try:
            if not zip_path:
                zip_path, _f = QFileDialog.getOpenFileName(
                    None, "选择更新包", os.path.expanduser("~/Desktop"), "更新包 (*.zip)")
                if not zip_path:
                    return
            ok_btn = QMessageBox.question(
                None, "安装更新",
                f"确定安装这个更新包吗?\n{os.path.basename(zip_path)}\n\n"
                "你的设置、API Key、热词和使用记录都会保留。\n更新完成后需要重新启动程序。",
                QMessageBox.Yes | QMessageBox.No)
            if ok_btn != QMessageBox.Yes:
                return
            try:
                ok, msg = updater.install_from_zip(zip_path, BASE)
            except Exception as e:
                traceback.print_exc()
                ok, msg = False, f"安装出错:{e}(可能有文件被占用,稍等几秒重试)"
            print(f"[app] 更新: ok={ok} {msg}")
            if ok:
                # 把磁盘上刚合并好的新键并回内存(缺失键合并,保留运行期状态),
                # 否则 _quit 的 _save_config 会把新键抹掉
                try:
                    with open(os.path.join(BASE, "config.json"), encoding="utf-8-sig") as f:
                        disk = json.load(f)
                    for k, v in disk.items():
                        if k not in self.cfg:
                            self.cfg[k] = v
                        elif isinstance(v, dict) and isinstance(self.cfg.get(k), dict):
                            for k2, v2 in v.items():
                                if k2 not in self.cfg[k]:
                                    self.cfg[k][k2] = v2
                except Exception:
                    traceback.print_exc()
                QMessageBox.information(
                    None, "更新完成",
                    f"{msg}\n\n程序即将退出,请重新双击 VoiceInput.exe。")
                quitting = True
                self._quit()
            else:
                QMessageBox.warning(None, "更新失败", msg)
        finally:
            if not quitting:
                try:
                    self._bind_hotkey(self.cfg.get("hotkey", "f9"))
                except Exception:
                    traceback.print_exc()

    def _check_update_now(self):
        """手动检查更新:立刻查,有新版走一键更新,没有也明确提示。"""
        url = self.cfg.get("update_url", "")
        if not url:
            QMessageBox.information(None, "检查更新", "未配置更新地址。")
            return
        self.bridge.status.emit("正在检查更新…")
        self.bridge.notify.emit("检查更新", "正在联网查询最新版本…", 2500)

        def work():
            try:
                import updater

                has_new, info = updater.check_remote(url, VERSION)
                if has_new:
                    self.bridge.update_found.emit(info)
                else:
                    self.bridge.notify.emit(
                        "已是最新版", f"当前 v{VERSION},没有更新。", 4000)
                    self.bridge.status.emit(
                        f"就绪 — 按住 {self.cfg.get('hotkey','f9').upper()} 说话")
            except Exception as e:
                traceback.print_exc()
                self.bridge.notify.emit("检查更新失败", f"联网查询出错:{e}", 5000)

        threading.Thread(target=work, daemon=True).start()

    def _check_remote_update(self):
        """启动后静默检查一次(update_url 为空则完全不联网)。"""
        try:
            import updater

            has_new, info = updater.check_remote(
                self.cfg.get("update_url", ""), VERSION)
            if has_new:
                v = info.get("version", "")
                print(f"[app] 发现新版本 {v}")
                self.bridge.update_found.emit(info)
        except Exception:
            traceback.print_exc()

    def _export_profile(self):
        """导出个人语音包:热词/词表/口音画像/使用记录(API Key 脱敏)。
        供未来在其他设备/新输入法项目上复用她的个性化成果。"""
        try:
            import zipfile

            dst = os.path.join(os.path.expanduser("~/Desktop"),
                               f"个人语音包_{time.strftime('%Y%m%d')}.zip")
            safe = json.loads(json.dumps(self.cfg, ensure_ascii=False))
            (safe.get("llm") or {}).pop("api_key", None)  # 脱敏
            with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("profile/config.json",
                           json.dumps(safe, ensure_ascii=False, indent=2))
                rec = os.path.join((self.cfg.get("archive") or {}).get("dir", "data"),
                                   "records.jsonl")
                if os.path.isfile(rec):
                    z.write(rec, "profile/records.jsonl")
                z.writestr("profile/README.txt",
                           "个人语音包\n"
                           "- config.json: 热词表/专有词表/口音纠错画像(已去除 API Key)\n"
                           "- records.jsonl: 使用记录(音频对应的识别与修正文本)\n"
                           "- 专属声学模型(较大,未打包): app/models/sherpa-onnx-sense-voice-*/model.int8.onnx\n"
                           "迁移到新设备/新输入法项目时,带上以上文件即可继承全部个性化能力。\n")
            print(f"[app] 个人语音包已导出: {dst}")
            try:
                self.tray.showMessage("导出完成", f"已保存到桌面: {os.path.basename(dst)}",
                                      QSystemTrayIcon.Information, 4000)
            except Exception:
                pass
        except Exception:
            traceback.print_exc()
            self.overlay.show_error("导出失败,详情见日志")

    # -- 录音状态机(全部在 Qt 主线程) --

    def _start_recording(self, quiet=False):
        try:
            self._gen += 1
            if self.draft:
                self._put_draft_marker(("new", self._gen))
            self.recorder.start_capture(tap=self._draft_q if self.draft else None)
            self.recording = True
            self.t0 = time.time()
            if not quiet:
                play("start", self.cfg.get("beep", True))
            self.overlay.show_listening()
            if self.cfg.get("record_mode", "hold") == "toggle":
                self._watch.start()
        except Exception:
            self.recording = False
            play("error", self.cfg.get("beep", True))
            traceback.print_exc()
            self.overlay.show_error("麦克风不可用,请检查耳机连接")

    def _stop_recording(self, quiet=False):
        if not self.recording:
            return
        self.recording = False
        self._watch.stop()
        try:
            samples, sr, truncated = self.recorder.stop_capture()
        except Exception:
            traceback.print_exc()
            self.overlay.show_error("录音结束异常")
            return
        if not quiet:  # SND_ASYNC 后音掐前音,只播一个语义化的
            play("error" if truncated else "stop", self.cfg.get("beep", True))
        if self.draft:
            self._put_draft_marker(("end", self._gen))
        if len(samples) == 0:
            self.overlay.show_error("没有录到声音")
            return
        self.overlay.show_processing()
        self._jobs.put((self._gen, samples, sr, truncated))

    def _abort_recording(self):
        """静默丢弃进行中的录音(打开对话框/退出前用),不注入结果。
        _watch/_key_down 无条件回收,顺带清掉任何泄漏。"""
        self._watch.stop()
        self._key_down = False
        if not self.recording:
            return
        self.recording = False
        try:
            self.recorder.stop_capture()
        except Exception:
            pass
        if self.draft:
            self._put_draft_marker(("end", self._gen))
        self.overlay.dismiss()

    def _watch_tick(self):
        """toggle 模式:快到上限时无缝切段接续——长文听写没有60秒天花板。
        前一段照常识别注入,新段自带0.3s预滚,接缝最多重复一个音节。"""
        if not self.recording:
            self._watch.stop()
            return
        max_sec = float(self.cfg.get("max_speech_seconds", 60))
        if (time.time() - self.t0 >= max_sec - 1.0
                or getattr(self.recorder, "truncated", False)):
            print("[app] 长录音自动分段接续")
            self._stop_recording(quiet=True)
            self._start_recording(quiet=True)

    def _on_press(self):
        if not self.ready:
            return
        if self.cfg.get("record_mode", "hold") == "toggle":
            now = time.monotonic()
            if self._key_down and now - self._key_down_t < 1.5:
                self._key_down_t = now
                return  # 按住不放的自动重复(≥1.5s 间隔不可能是重复,即使 up 丢了)
            self._key_down = True
            self._key_down_t = now
            if self.recording:
                self._stop_recording()
            else:
                self._start_recording()
            return
        # hold 模式
        if self.recording:
            if time.time() - self.t0 > float(self.cfg.get("max_speech_seconds", 60)) + 5:
                try:
                    self.recorder.stop_capture()
                except Exception:
                    pass
                self.recording = False
                print("[app] 上次录音未正常结束,已自动重置")
            else:
                return  # 按住时的自动重复
        self._start_recording()

    def _on_release(self):
        self._key_down = False
        if self.cfg.get("record_mode", "hold") == "hold":
            self._stop_recording()

    def _put_draft_marker(self, marker):
        """会话标记不能像音频块那样丢弃;队列满时清空重放(草稿丢了没关系,标记不能丢)。"""
        try:
            self._draft_q.put_nowait(marker)
        except queue.Full:
            try:
                while True:
                    self._draft_q.get_nowait()
            except queue.Empty:
                pass
            self._draft_q.put_nowait(marker)

    # -- 常驻草稿线程:单线程独占 OnlineRecognizer,会话由标记切换 --

    def _draft_worker(self):
        from streaming import resample_chunk

        cur_gen, stream, last, sr = -1, None, "", 16000
        while True:
            item = self._draft_q.get()
            if isinstance(item, tuple):
                kind, gen = item
                if kind == "new":
                    cur_gen, last = gen, ""
                    sr = self.recorder.sample_rate
                    try:
                        stream = self.draft.new_session()
                    except Exception:
                        traceback.print_exc()
                        stream = None
                elif kind == "end" and gen == cur_gen:
                    stream = None
                continue
            if stream is None:
                continue
            try:
                text = self.draft.feed(stream, resample_chunk(item, sr))
            except Exception:
                traceback.print_exc()
                stream = None
                continue
            if text and text != last:
                last = text
                self.bridge.partial.emit(cur_gen, text)

    # -- 终稿单工作线程 --

    def _final_worker(self):
        while True:
            gen, samples, sr, truncated = self._jobs.get()
            try:
                if truncated:
                    print(f"[app] 录音被截断为前 {self.cfg.get('max_speech_seconds',60)}s")
                samples16 = resample_to_16k(samples, sr)
                dur = len(samples16) / 16000.0
                if dur < float(self.cfg.get("min_speech_seconds", 0.4)):
                    self.bridge.error.emit(gen, "说话太短")
                    continue
                t0 = time.time()
                raw = self.rec.transcribe(samples16, 16000)
                if not raw:
                    self.bridge.error.emit(gen, "没有识别到内容")
                    continue
                fixed = self.cor.correct(raw)
                print(f"[app] #{gen} {dur:.1f}s 音频, 识别+纠错 {time.time()-t0:.2f}s")
                print(f"  原始: {raw}")
                if fixed != raw:
                    print(f"  纠错: {fixed}")
                self.injector.inject(fixed)
                self.arc.save(samples16, raw, fixed)
                self.bridge.done.emit(gen, fixed, self.cor.last_llm_used)
                if self.cor.circuit_just_opened:
                    self.cor.circuit_just_opened = False
                    self._llm_degraded = True
                    self.bridge.status.emit("就绪 — 本地模式(云端润色暂时连不上)")
                    self.bridge.error.emit(-1, "云端润色连不上,已自动切为纯本地(10分钟后自动重试)")
                elif self.cor.last_llm_used and getattr(self, "_llm_degraded", False):
                    self._llm_degraded = False
                    self.bridge.status.emit(f"就绪 — 按住 {self.cfg.get('hotkey','f9').upper()} 说话")
            except Exception:
                traceback.print_exc()
                self.bridge.error.emit(gen, "处理失败,详情见日志")
            finally:
                self._jobs.task_done()

    # -- UI 信号处理(带会话代数过滤:过期会话不触碰悬浮条) --

    def _on_partial(self, gen, text):
        if gen == self._gen and self.recording:
            self.overlay.show_partial(text)

    def _on_done(self, gen, text, llm_used=False):
        if gen != -1 and gen != self._gen:
            print(f"[app] 过期会话#{gen}已注入,不更新UI")
            return
        if self.recording:  # 新一句正在录,别打断聆听态
            return
        self.overlay.show_done(text, llm_used)

    def _on_error(self, gen, msg):
        if gen != -1 and (gen != self._gen or self.recording):
            print(f"[app] 过期会话#{gen}错误: {msg}")
            return
        self.overlay.show_error(msg)

    # -- 退出 --

    def _quit(self):
        try:
            self._save_config()  # 悬浮条位置等
            self._abort_recording()
            self._unbind_hotkey()
            # 等终稿队列排空(最多4秒),别把正在识别的句子扔掉
            deadline = time.time() + 4
            while self._jobs.unfinished_tasks and time.time() < deadline:
                time.sleep(0.1)
            if hasattr(self, "recorder"):
                self.recorder.close()
        except Exception:
            pass
        self.tray.hide()
        self.app.quit()

    def run(self):
        return self.app.exec()


def main():
    try:
        _check_qt_platform_plugin()
        app = VoiceInputApp()
        sys.exit(app.run())
    except SystemExit:
        raise
    except Exception as e:
        traceback.print_exc()
        shown = False
        try:
            qa = QApplication.instance() or QApplication(sys.argv)
            QMessageBox.critical(None, "听晓", f"启动失败:{e}\n\n日志在:\n{LOG_PATH}")
            shown = True
        except Exception:
            pass
        if not shown:
            _native_msgbox(f"启动失败:{e}\n日志:{LOG_PATH}")
        sys.exit(1)


if __name__ == "__main__":
    main()
