"""界面层:悬浮条(非线性动画+声波律动)、设置窗口(macOS 风格)、动画开关控件。

设计语言:深色半透明胶囊、大圆角、发丝描边、缓动曲线(OutCubic/OutBack)、
状态色渐变。字体优先 Segoe UI Variable(Win11 系统字体),回退微软雅黑/苹方。
"""
import json
import math
import os
import sys

from PySide6.QtCore import (
    QEasingCurve, QPoint, QPropertyAnimation, QRect, Qt, QTimer,
    QVariantAnimation, Property, Signal,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

ACCENT = QColor(10, 132, 255)        # macOS 系统蓝
RED = QColor(255, 69, 58)            # 聆听
AMBER = QColor(255, 214, 10)         # 处理中
GREEN = QColor(48, 209, 88)          # 完成
PILL_BG = QColor(24, 24, 28, 242)
HAIRLINE = QColor(255, 255, 255, 30)
TEXT = QColor(242, 242, 247)
TEXT_DIM = QColor(174, 174, 178)


def ui_font(size=14, weight=QFont.Normal):
    f = QFont()
    f.setFamilies([
        "Segoe UI Variable Display", "Segoe UI Variable", "Segoe UI",
        "Microsoft YaHei UI", "PingFang SC",
    ])
    f.setPointSize(size)
    f.setWeight(weight)
    return f


# ---------- 悬浮条 ----------

class Overlay(QWidget):
    """胶囊悬浮条。动画:淡入上浮(OutCubic)、宽度过渡(OutCubic)、
    状态色渐变、聆听态由真实麦克风电平驱动的五柱声波。可拖动。"""

    MAX_W, MIN_W, H = 640, 250, 60
    MARGIN = 24  # 四周留白用于画柔和阴影

    def __init__(self, cfg_ui, get_level=None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlag(Qt.WindowDoesNotAcceptFocus, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFont(ui_font(14))

        self._cfg_ui = cfg_ui or {}
        self._get_level = get_level or (lambda: 0.0)
        self._state = "idle"
        self._text = ""
        self._drag = None
        self._pill_w = float(self.MIN_W)
        self._dot_color = QColor(RED)
        self._bars = [0.12] * 5
        self._level_smooth = 0.0
        self._phase = 0.0
        self._llm_badge = False

        # 声波/呼吸动画时钟(仅显示期间运行)
        self._tick = QTimer(self)
        self._tick.setInterval(33)
        self._tick.timeout.connect(self._on_tick)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)

        # 淡入淡出
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(200)
        # 宽度非线性过渡
        self._wanim = QVariantAnimation(self)
        self._wanim.setDuration(180)
        self._wanim.setEasingCurve(QEasingCurve.OutCubic)
        self._wanim.valueChanged.connect(self._apply_width)
        # 状态色渐变
        self._canim = QVariantAnimation(self)
        self._canim.setDuration(220)
        self._canim.valueChanged.connect(self._apply_color)

        self.resize(self.MIN_W + self.MARGIN * 2, self.H + self.MARGIN * 2)
        self._place()

    # ---- 对外状态入口 ----

    def show_listening(self):
        self._set_state("listening", "", RED)

    def show_partial(self, text):
        if self._state != "listening":
            return
        self._text = text[-120:]
        self._animate_width()
        self.update()

    def show_processing(self):
        self._set_state("processing", self._text, AMBER)

    def show_done(self, text, llm_used=False):
        self._llm_badge = bool(llm_used)
        self._set_state("done", text[:80], GREEN)
        self._hide_timer.start(1400)

    def show_error(self, msg):
        self._set_state("error", msg, RED)
        self._hide_timer.start(2600)

    # ---- 内部 ----

    def _set_state(self, state, text, color):
        self._state, self._text = state, text
        self._hide_timer.stop()
        self._canim.stop()
        self._canim.setStartValue(QColor(self._dot_color))
        self._canim.setEndValue(QColor(color))
        self._canim.start()
        if state == "listening":
            if not self._tick.isActive():
                self._tick.start()
        else:
            self._tick.stop()
        self._animate_width()
        self._pop_in()
        self.update()

    def _is_hiding(self):
        return (getattr(self, "_fade_connected", False)
                and self._fade.state() == QPropertyAnimation.Running)

    def _pop_in(self):
        self._hide_timer.stop()
        if self.isVisible() and not self._is_hiding() and self.windowOpacity() > 0.9:
            return
        if self._is_hiding():
            # 打断正在进行的淡出,从当前透明度直接淡回来(不重播入场动画)
            self._fade.stop()
            self._disconnect_fade()
            self._fade.setStartValue(self.windowOpacity())
            self._fade.setEndValue(1.0)
            self._fade.setEasingCurve(QEasingCurve.OutCubic)
            self._fade.start()
            return
        # 真正的入场:宽度先快照到目标值(避免 pos 与宽度动画并发互写 x)
        self._wanim.stop()
        self._apply_width(float(self._target_width()))
        self._place()
        end = self.pos()
        self.move(end.x(), end.y() + 14)  # 从下方轻轻浮上来
        self.setWindowOpacity(0.0)
        self.show()
        if self._state == "listening":
            self._tick.start()
        move = QVariantAnimation(self)  # 只动 y,x 的所有权归 _apply_width
        move.setDuration(260)
        move.setEasingCurve(QEasingCurve.OutBack)
        move.setStartValue(float(end.y() + 14))
        move.setEndValue(float(end.y()))
        move.valueChanged.connect(lambda y: self.move(self.x(), int(y)))
        move.start(QVariantAnimation.DeleteWhenStopped)
        self._fade.stop()
        self._disconnect_fade()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)
        self._fade.start()

    def dismiss(self):
        """立即隐藏并停掉全部定时器/动画(绕过淡出流程时的完整清理)。"""
        self._hide_timer.stop()
        self._fade.stop()
        self._disconnect_fade()
        self._tick.stop()
        self.hide()

    def _fade_out(self):
        self._fade.stop()
        self._disconnect_fade()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.setEasingCurve(QEasingCurve.InCubic)
        self._fade.finished.connect(self._after_fade)
        self._fade_connected = True
        self._fade.start()

    def _disconnect_fade(self):
        if getattr(self, "_fade_connected", False):
            self._fade.finished.disconnect(self._after_fade)
            self._fade_connected = False

    def _after_fade(self):
        self.hide()
        self._tick.stop()

    def _on_tick(self):
        if self._state != "listening":
            return
        # 电平平滑(攻快释慢的非线性包络)+ 相位推进
        lv = 0.0
        try:
            lv = float(self._get_level())
        except Exception:
            pass
        lv = min(1.0, lv * 8.0)
        a = 0.55 if lv > self._level_smooth else 0.12
        self._level_smooth += (lv - self._level_smooth) * a
        self._phase += 0.25
        base = 0.10 + self._level_smooth * 0.9
        for i in range(5):
            wob = 0.5 + 0.5 * math.sin(self._phase + i * 1.1)
            target = base * (0.35 + 0.65 * wob)
            self._bars[i] += (target - self._bars[i]) * 0.5
        self.update()

    def _display_text(self):
        if self._state == "listening":
            return self._text or "正在聆听…"
        if self._state == "processing":
            return (self._text + "  ›  识别中…") if self._text else "识别中…"
        if self._state == "done":
            return "✓  " + self._text
        return self._text

    def _target_width(self):
        fm = self.fontMetrics()
        extra = 44 if (self._state == "done" and self._llm_badge) else 0
        return min(self.MAX_W, max(self.MIN_W, fm.horizontalAdvance(self._display_text()) + 110 + extra))

    def _animate_width(self):
        t = float(self._target_width())
        if abs(t - self._pill_w) < 2:
            return
        self._wanim.stop()
        self._wanim.setStartValue(self._pill_w)
        self._wanim.setEndValue(t)
        self._wanim.start()

    def _apply_width(self, w):
        old_left = self.x()
        cx = self.x() + self.width() // 2
        self._pill_w = float(w)
        full = int(self._pill_w) + self.MARGIN * 2
        self.setGeometry(cx - full // 2, self.y(), full, self.H + self.MARGIN * 2)
        if self._drag is not None:
            self._drag -= QPoint(self.x() - old_left, 0)
        self.update()

    def _apply_color(self, c):
        self._dot_color = QColor(c)
        self.update()

    def _place(self):
        pos = self._cfg_ui.get("overlay_pos")
        if pos and isinstance(pos, list) and len(pos) == 2:
            pt = QPoint(int(pos[0]), int(pos[1]))
            for s in QApplication.screens():
                if s.availableGeometry().adjusted(-40, -40, 40, 40).contains(pt):
                    self.move(pt)
                    return
        scr = QApplication.primaryScreen().availableGeometry()
        self.move(scr.center().x() - self.width() // 2, scr.bottom() - 150)

    def reset_position(self):
        self._cfg_ui.pop("overlay_pos", None)
        self._place()

    # ---- 绘制 ----

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        m = self.MARGIN
        pill = QRect(int((self.width() - self._pill_w) / 2), m, int(self._pill_w), self.H)
        r = self.H / 2 - 1

        # 柔和投影(多层递减透明度)
        for i in range(8, 0, -1):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, int(3.2 * i)))
            p.drawRoundedRect(pill.adjusted(-i // 2, -i // 2 + 2, i // 2, i), r + i // 2, r + i // 2)

        # 胶囊本体 + 发丝描边
        p.setBrush(PILL_BG)
        p.setPen(QPen(HAIRLINE, 1))
        p.drawRoundedRect(pill, r, r)

        # 左侧:五柱声波(听) / 圆点(其他状态)
        cx0 = pill.x() + 26
        cy = pill.center().y() + 1
        if self._state == "listening":
            p.setPen(Qt.NoPen)
            p.setBrush(self._dot_color)
            for i, b in enumerate(self._bars):
                h = max(4, int(b * 30))
                p.drawRoundedRect(cx0 + i * 7 - 2, cy - h // 2, 4, h, 2, 2)
        else:
            p.setPen(Qt.NoPen)
            glow = QColor(self._dot_color)
            glow.setAlpha(56)
            p.setBrush(glow)
            p.drawEllipse(QPoint(cx0 + 12, cy), 11, 11)
            p.setBrush(self._dot_color)
            p.drawEllipse(QPoint(cx0 + 12, cy), 6, 6)

        # 完成态右侧 AI 徽章(这句经过云端润色)
        badge_w = 0
        if self._state == "done" and self._llm_badge:
            badge_w = 34
            br = QRect(pill.right() - badge_w - 12, pill.center().y() - 10, badge_w, 20)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(ACCENT.red(), ACCENT.green(), ACCENT.blue(), 60))
            p.drawRoundedRect(br, 10, 10)
            p.setPen(QColor(140, 200, 255))
            bf = ui_font(10, QFont.DemiBold)
            p.setFont(bf)
            p.drawText(br, Qt.AlignCenter, "AI")
            p.setFont(self.font())

        # 文本
        p.setPen(TEXT if self._state != "processing" else TEXT_DIM)
        fm = p.fontMetrics()
        tx = pill.x() + 64
        avail = pill.width() - 64 - 22 - badge_w
        mode = Qt.ElideRight if self._state in ("done", "error") else Qt.ElideLeft
        p.drawText(QRect(tx, pill.y(), avail, pill.height()),
                   Qt.AlignVCenter | Qt.AlignLeft,
                   fm.elidedText(self._display_text(), mode, avail))

    # ---- 拖动 ----

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._drag = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, ev):
        if self._drag is not None:
            self.move(ev.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _ev):
        if self._drag is not None:
            self._drag = None
            self._cfg_ui["overlay_pos"] = [self.x(), self.y()]


# ---------- 动画开关(macOS Toggle) ----------

class Switch(QWidget):
    toggled = Signal(bool)

    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setFixedSize(44, 26)
        self.setCursor(Qt.PointingHandCursor)
        self._checked = bool(checked)
        self._t = 1.0 if checked else 0.0
        self._anim = QPropertyAnimation(self, b"t", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    def _get_t(self):
        return self._t

    def _set_t(self, v):
        self._t = v
        self.update()

    t = Property(float, _get_t, _set_t)

    def isChecked(self):
        return self._checked

    def setChecked(self, v):
        v = bool(v)
        if v == self._checked:
            return
        self._checked = v
        self._anim.stop()
        self._anim.setStartValue(self._t)
        self._anim.setEndValue(1.0 if v else 0.0)
        self._anim.start()
        self.toggled.emit(v)

    def mouseReleaseEvent(self, _ev):
        self.setChecked(not self._checked)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        off = QColor(120, 120, 128, 90)
        on = QColor(GREEN)
        bg = QColor(
            int(off.red() + (on.red() - off.red()) * self._t),
            int(off.green() + (on.green() - off.green()) * self._t),
            int(off.blue() + (on.blue() - off.blue()) * self._t),
            int(off.alpha() + (255 - off.alpha()) * self._t),
        )
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(self.rect(), 13, 13)
        x = 3 + self._t * (self.width() - 26)
        p.setBrush(Qt.white)
        p.drawEllipse(int(x), 3, 20, 20)


# ---------- 设置窗口 ----------

QSS = """
QDialog { background: #1c1c1e; }
QLabel { color: #f2f2f7; font-size: 13px; }
QLabel[dim="true"] { color: #98989e; font-size: 11px; }
QLabel[header="true"] { color: #98989e; font-size: 11px; font-weight: 600; }
QFrame[card="true"] { background: #2c2c2e; border-radius: 10px; }
QLineEdit, QComboBox {
  background: #3a3a3c; color: #f2f2f7; border: 1px solid #48484a;
  border-radius: 6px; padding: 4px 8px; font-size: 13px; min-height: 20px;
}
QLineEdit:focus, QComboBox:focus { border: 1px solid #0a84ff; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView { background: #2c2c2e; color: #f2f2f7; selection-background-color: #0a84ff; }
QPushButton {
  background: #3a3a3c; color: #f2f2f7; border: none; border-radius: 6px;
  padding: 5px 14px; font-size: 13px;
}
QPushButton:hover { background: #48484a; }
QPushButton:disabled { background: #2c2c2e; color: #6e6e73; }
QPushButton[primary="true"] { background: #0a84ff; font-weight: 600; }
QPushButton[primary="true"]:hover { background: #339dff; }
QScrollArea { border: none; background: transparent; }
"""


def _row(label, widget, sub=None):
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(14, 8, 14, 8)
    col = QVBoxLayout()
    col.setSpacing(1)
    lab = QLabel(label)
    col.addWidget(lab)
    if sub:
        s = QLabel(sub)
        s.setProperty("dim", True)
        s.setWordWrap(True)
        col.addWidget(s)
    lay.addLayout(col, 1)
    lay.addWidget(widget, 0, Qt.AlignRight)
    return box


def _card(*rows):
    card = QFrame()
    card.setProperty("card", True)
    lay = QVBoxLayout(card)
    lay.setContentsMargins(0, 2, 0, 2)
    lay.setSpacing(0)
    for i, r in enumerate(rows):
        lay.addWidget(r)
        if i < len(rows) - 1:
            line = QFrame()
            line.setFixedHeight(1)
            line.setStyleSheet("background:#3a3a3c; margin-left:14px;")
            lay.addWidget(line)
    return card


def _header(text):
    h = QLabel(text.upper())
    h.setProperty("header", True)
    h.setContentsMargins(6, 10, 0, 2)
    return h


AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def get_autostart(exe_path=""):
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as k:
            val, _t = winreg.QueryValueEx(k, "VoiceInput")
        if exe_path and os.path.normcase(str(val).strip('"')) != os.path.normcase(exe_path):
            return False  # 键还在但指向旧位置(安装目录被移动过),按未开启显示
        return True
    except OSError:
        return False


def set_autostart(enable, exe_path):
    if sys.platform != "win32":
        return
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enable:
            winreg.SetValueEx(k, "VoiceInput", 0, winreg.REG_SZ, f'"{exe_path}"')
        else:
            try:
                winreg.DeleteValue(k, "VoiceInput")
            except OSError:
                pass


class SettingsDialog(QDialog):
    VALID_KEYS = ["f9", "f8", "scroll lock", "pause"]

    def __init__(self, cfg, devices, data_dir="data", exe_path="", on_reset_overlay=None,
                 version="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("语音输入 设置")
        self.setMinimumSize(460, 560)
        self.setStyleSheet(QSS)
        self.setFont(ui_font(13))
        self.cfg = cfg
        self.exe_path = exe_path

        # -- 控件 --
        self.record_mode = QComboBox()
        self.record_mode.addItem("按住说话,松开出字", "hold")
        self.record_mode.addItem("单击开始,再按一下结束", "toggle")
        self.record_mode.setCurrentIndex(
            1 if cfg.get("record_mode", "hold") == "toggle" else 0)

        self.hotkey = QComboBox()
        self.hotkey.addItems(self.VALID_KEYS)
        cur = cfg.get("hotkey", "f9")
        if cur not in self.VALID_KEYS:
            self.hotkey.addItem(cur)
        self.hotkey.setCurrentText(cur)

        self.mic = QComboBox()
        self.mic.addItem("系统默认", "")
        cur_mic = cfg.get("mic_name_contains", "")
        for _i, name, _api, _sr in devices:
            self.mic.addItem(name[:42], name)
        idx = self.mic.findData(cur_mic)
        if idx < 0 and cur_mic:
            self.mic.addItem(cur_mic + "(未连接)", cur_mic)
            idx = self.mic.count() - 1
        self.mic.setCurrentIndex(max(0, idx))

        self.beep = Switch(bool(cfg.get("beep", True)))
        self.persistent = Switch(bool(cfg.get("persistent_mic", True)))
        self.autostart = Switch(get_autostart(exe_path))

        llm = cfg.get("llm") or {}
        self.llm_on = Switch(bool(llm.get("enabled")))
        self.llm_url = QLineEdit(llm.get("base_url", "https://api.deepseek.com/v1"))
        self.llm_key = QLineEdit(llm.get("api_key", ""))
        self.llm_key.setEchoMode(QLineEdit.Password)
        self.llm_key.setPlaceholderText("未配置(功能关闭,不会联网)")
        self.llm_model = QLineEdit(llm.get("model", "deepseek-chat"))

        arch = cfg.get("archive") or {}
        self.archive_on = Switch(bool(arch.get("enabled", True)))
        size_mb = 0.0
        try:
            size_mb = sum(e.stat().st_size for e in os.scandir(data_dir) if e.is_file()) / 1048576
        except OSError:
            pass
        btn_data = QPushButton("打开文件夹")
        btn_data.clicked.connect(lambda: self._open_dir(data_dir))

        btn_reset = QPushButton("重置位置")

        def _do_reset():
            if on_reset_overlay:
                on_reset_overlay()
            btn_reset.setText("已恢复")
            btn_reset.setEnabled(False)

        btn_reset.clicked.connect(_do_reset)

        # -- 布局 --
        content = QWidget()
        v = QVBoxLayout(content)
        v.setContentsMargins(16, 8, 16, 8)
        v.setSpacing(4)
        v.addWidget(_header("通用"))
        v.addWidget(_card(
            _row("说话热键", self.hotkey),
            _row("录音方式", self.record_mode, "长段听写建议用「单击开始」,不用一直按着"),
            _row("提示音", self.beep),
            _row("开机自动启动", self.autostart),
            _row("悬浮条", btn_reset, "拖动可移动位置;点按钮恢复到屏幕下方中央"),
        ))
        v.addWidget(_header("麦克风"))
        v.addWidget(_card(
            _row("输入设备", self.mic),
            _row("常驻占用", self.persistent, "按键即录不丢字;蓝牙耳机会保持通话音质"),
        ))
        v.addWidget(_header("智能纠错(可选)"))
        v.addWidget(_card(
            _row("LLM 纠错", self.llm_on, "只上传识别文本,绝不上传录音;不填 Key 则完全离线"),
            _row("API 地址", self.llm_url),
            _row("API Key", self.llm_key),
            _row("模型", self.llm_model),
        ))
        v.addWidget(_header("数据"))
        v.addWidget(_card(
            _row("本地存档", self.archive_on, f"已用 {size_mb:.0f} MB · 用于持续改进识别,仅存本机"),
            _row("存档位置", btn_data),
        ))
        about = QLabel(f"语音输入 {version or 'v3'} · 专属微调模型 · 识别全程本地运行")
        about.setProperty("dim", True)
        about.setAlignment(Qt.AlignCenter)
        about.setContentsMargins(0, 10, 0, 4)
        v.addWidget(about)
        v.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)

        btns = QHBoxLayout()
        ok = QPushButton("保存")
        ok.setProperty("primary", True)
        ok.clicked.connect(self.accept)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 8, 0, 12)
        root.addWidget(scroll)
        bw = QWidget()
        bw.setLayout(btns)
        bw.setContentsMargins(16, 0, 16, 0)
        root.addWidget(bw)

    def _open_dir(self, d):
        try:
            os.makedirs(d, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(os.path.abspath(d))  # noqa
            else:
                import subprocess

                subprocess.Popen(["open", d])
        except Exception as e:
            print(f"[ui] 打开存档目录失败: {e}")
            QMessageBox.warning(self, "打开文件夹", f"无法打开存档文件夹:\n{e}")

    def apply_to(self, cfg):
        cfg["record_mode"] = self.record_mode.currentData() or "hold"
        cfg["hotkey"] = self.hotkey.currentText().strip().lower() or "f9"
        cfg["mic_name_contains"] = self.mic.currentData() or ""
        cfg["beep"] = self.beep.isChecked()
        cfg["persistent_mic"] = self.persistent.isChecked()
        llm = cfg.get("llm") or {}
        llm["enabled"] = self.llm_on.isChecked()
        llm["base_url"] = self.llm_url.text().strip() or "https://api.deepseek.com/v1"
        llm["api_key"] = self.llm_key.text().strip()
        llm["model"] = self.llm_model.text().strip() or "deepseek-chat"
        llm.setdefault("timeout_seconds", 5)
        cfg["llm"] = llm
        arch = cfg.get("archive") or {}
        arch["enabled"] = self.archive_on.isChecked()
        cfg["archive"] = arch
        err = None
        try:
            set_autostart(self.autostart.isChecked(), self.exe_path)
        except Exception as e:
            err = e
        if sys.platform == "win32" and (
            err is not None or get_autostart(self.exe_path) != self.autostart.isChecked()
        ):
            print(f"[ui] 设置开机自启失败: {err or '写入后复核不一致(可能被安全软件拦截)'}")
            QMessageBox.warning(
                None, "开机自启",
                "设置「开机自动启动」没有成功,可能被安全软件或系统策略拦截。\n"
                "其余设置均已正常保存,不受影响。")


# ---------- 听写历史 ----------

def read_recent_records(path, limit=50):
    """读 records.jsonl 尾部最近 limit 条(只读末尾 256KB,大文件也秒开)。"""
    if not os.path.isfile(path):
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 256 * 1024))
        data = f.read().decode("utf-8", "replace")
    lines = data.splitlines()
    if size > 256 * 1024 and lines:
        lines = lines[1:]  # 掐掉可能被截断的首行
    out = []
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("corrected"):
            out.append(e)
        if len(out) >= limit:
            break
    return out


class HistoryDialog(QDialog):
    """最近听写:双击任意一条复制全文。找回打错窗口/被覆盖的文字用。"""

    def __init__(self, records_path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("最近听写")
        self.setMinimumSize(480, 520)
        self.setStyleSheet(QSS + """
QListWidget { background: #2c2c2e; border: none; border-radius: 10px;
              color: #f2f2f7; font-size: 13px; padding: 4px; }
QListWidget::item { padding: 9px 10px; border-bottom: 1px solid #3a3a3c; }
QListWidget::item:selected { background: #0a84ff; border-radius: 6px; }
""")
        self.setFont(ui_font(13))

        self.listw = QListWidget()
        self.listw.setWordWrap(True)
        recs = read_recent_records(records_path)
        if not recs:
            empty = QListWidgetItem("暂无记录\n(本地存档开启后,每次听写都会出现在这里)")
            empty.setFlags(Qt.NoItemFlags)
            self.listw.addItem(empty)
        for e in recs:
            ts = e.get("ts", "")
            when = f"{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}" if len(ts) >= 13 else ""
            text = e["corrected"]
            item = QListWidgetItem(f"{when}   {text[:64]}{'…' if len(text) > 64 else ''}")
            item.setData(Qt.UserRole, text)
            self.listw.addItem(item)
        self.listw.itemDoubleClicked.connect(self._copy_item)

        tip = QLabel("双击一条即可复制全文")
        tip.setProperty("dim", True)
        tip.setAlignment(Qt.AlignCenter)

        btns = QHBoxLayout()
        copy = QPushButton("复制选中")
        copy.setProperty("primary", True)
        copy.clicked.connect(self._copy_current)
        close = QPushButton("关闭")
        close.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(close)
        btns.addWidget(copy)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 12)
        root.addWidget(self.listw, 1)
        root.addWidget(tip)
        root.addLayout(btns)

    def _copy_item(self, item):
        text = item.data(Qt.UserRole)
        if text:
            QApplication.clipboard().setText(text)
            self.setWindowTitle("最近听写 — 已复制 ✓")

    def _copy_current(self):
        it = self.listw.currentItem()
        if it:
            self._copy_item(it)
