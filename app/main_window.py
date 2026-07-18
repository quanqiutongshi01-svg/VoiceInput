"""听晓主界面:像标准应用一样有一个主窗口。

- 平时住托盘;点托盘图标 / 托盘菜单「主界面」打开;关闭=隐藏回托盘,不退出。
- 打开时在任务栏正常显示(Windows 可借此右键固定到任务栏)。
- Windows 集成:一键创建「开始菜单 / 桌面」快捷方式(免安装,.lnk 指向 VoiceInput.exe)。
"""
import os
import subprocess
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QGridLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout,
    QWidget,
)

from ui import QSS, ui_font


# ---------- Windows 快捷方式(.lnk,经 PowerShell COM,免安装、无需管理员) ----------

def _ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def create_shortcut_win(kind, exe_path, icon_path=""):
    """kind: 'startmenu' | 'desktop'。成功返回 (True, lnk路径)。"""
    if sys.platform != "win32":
        return False, "仅 Windows"
    folder = ("[Environment]::GetFolderPath('Programs')" if kind == "startmenu"
              else "[Environment]::GetFolderPath('Desktop')")
    icon = icon_path if (icon_path and os.path.isfile(icon_path)) else exe_path
    ps = (
        f"$d={folder};"
        f"$p=Join-Path $d '听晓.lnk';"
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut($p);"
        f"$s.TargetPath={_ps_quote(exe_path)};"
        f"$s.WorkingDirectory={_ps_quote(os.path.dirname(exe_path))};"
        f"$s.IconLocation={_ps_quote(icon)};"
        f"$s.Description='听晓 · 会听会打的私人输入法';"
        f"$s.Save();Write-Output $p"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, timeout=20,
            creationflags=0x08000000)  # CREATE_NO_WINDOW
        out = (r.stdout or "").strip()
        if r.returncode == 0 and out:
            return True, out
        return False, (r.stderr or "创建失败").strip()[:200]
    except Exception as e:
        return False, str(e)


class MainWindow(QWidget):
    """主窗口。需要宿主 app 提供各 _open_* 动作与 cfg。"""

    def __init__(self, app, icon: QIcon, version=""):
        super().__init__()
        self.app_ref = app
        self.setWindowTitle("听晓")
        self.setWindowIcon(icon)
        self.setMinimumSize(430, 420)
        self.setStyleSheet(QSS)
        self.setFont(ui_font(13))

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 16)
        root.setSpacing(10)

        # 头部:图标 + 名称 + 状态
        head = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(icon.pixmap(56, 56))
        head.addWidget(logo)
        tb = QVBoxLayout()
        title = QLabel("听晓")
        title.setStyleSheet("font-size:22px;font-weight:700;")
        self.status = QLabel("正在启动…")
        self.status.setProperty("dim", True)
        tb.addWidget(title)
        tb.addWidget(self.status)
        head.addLayout(tb)
        head.addStretch(1)
        ver = QLabel(version)
        ver.setProperty("dim", True)
        ver.setAlignment(Qt.AlignTop | Qt.AlignRight)
        head.addWidget(ver)
        root.addLayout(head)

        # 快捷动作
        grid = QGridLayout()
        grid.setSpacing(8)
        acts = [
            ("设置…", app._open_settings),
            ("最近听写…", app._open_history),
            ("声音记忆库…", app._open_memory_bank),
            ("家庭快传 · 发送…", app._open_transfer),
            ("家庭快传 · 收件箱…", app._open_inbox),
            ("检查更新", app._check_update_now),
        ]
        for i, (label, fn) in enumerate(acts):
            b = QPushButton(label)
            b.setMinimumHeight(38)
            b.clicked.connect(lambda _c=False, f=fn: f())
            grid.addWidget(b, i // 2, i % 2)
        root.addLayout(grid)

        # Windows 集成(免安装的"标准应用"体验)
        if sys.platform == "win32":
            root.addSpacing(4)
            wl = QLabel("系统集成")
            wl.setStyleSheet("font-weight:600;")
            root.addWidget(wl)
            wrow = QHBoxLayout()
            b1 = QPushButton("添加到开始菜单")
            b1.clicked.connect(lambda: self._mk_shortcut("startmenu"))
            b2 = QPushButton("添加桌面快捷方式")
            b2.clicked.connect(lambda: self._mk_shortcut("desktop"))
            wrow.addWidget(b1)
            wrow.addWidget(b2)
            root.addLayout(wrow)
            hint = QLabel("固定到任务栏:本窗口打开时,右键任务栏上的听晓图标 → 固定到任务栏")
            hint.setProperty("dim", True)
            hint.setWordWrap(True)
            root.addWidget(hint)

        root.addStretch(1)
        tip = QLabel("关闭本窗口只是收回托盘,听晓继续在后台听候热键。")
        tip.setProperty("dim", True)
        tip.setAlignment(Qt.AlignCenter)
        root.addWidget(tip)

    # ---- 对外 ----

    def set_status(self, text):
        self.status.setText(text)

    def present(self):
        """显示并前置(任务栏可见)。"""
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        # 关闭=隐藏回托盘,不退出(退出走托盘菜单)
        event.ignore()
        self.hide()

    # ---- 内部 ----

    def _exe_path(self):
        base = getattr(sys.modules.get("app"), "BASE", os.path.dirname(os.path.abspath(__file__)))
        return os.path.abspath(os.path.join(base, "..", "VoiceInput.exe"))

    def _mk_shortcut(self, kind):
        exe = self._exe_path()
        if not os.path.isfile(exe):
            QMessageBox.warning(self, "快捷方式", "没找到 VoiceInput.exe,请确认程序目录完整。")
            return
        ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), "听晓.ico")
        ok, msg = create_shortcut_win(kind, exe, ico)
        where = "开始菜单" if kind == "startmenu" else "桌面"
        if ok:
            QMessageBox.information(self, "快捷方式", f"已添加到{where}:\n{msg}")
        else:
            QMessageBox.warning(self, "快捷方式", f"创建失败:{msg}")
