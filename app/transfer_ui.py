"""听晓快传界面:发送(暂存卡片+多文件+进度+历史)、收件箱(打开文件/文件夹)。"""
import os
import subprocess
import sys

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QTabWidget, QVBoxLayout, QWidget,
)

from ui import QSS, ui_font


def open_path(path):
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        print(f"[transfer_ui] 打开失败 {path}: {e}")


def reveal_path(path):
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            open_path(os.path.dirname(path))
    except Exception:
        open_path(os.path.dirname(path))


def human_size(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024


def speed_str(bps):
    try:
        return human_size(float(bps)) + "/s"
    except (TypeError, ValueError):
        return ""


XFER_QSS = QSS + """
#dropzone { background: #2c2c2e; border: 2px dashed #48484a; border-radius: 12px;
            color: #98989e; font-size: 14px; }
#dropzone[hot="true"] { border-color: #0a84ff; color: #0a84ff; background: #23324a; }
#chip { background: #3a3a3c; border-radius: 8px; }
QPlainTextEdit { background: #2c2c2e; border: 1px solid #3a3a3c; border-radius: 8px;
                 color: #f2f2f7; padding: 6px; font-size: 13px; }
QProgressBar { background: #2c2c2e; border: none; border-radius: 4px; height: 6px; text-align: center; }
QProgressBar::chunk { background: #0a84ff; border-radius: 4px; }
QTabWidget::pane { border: none; }
QTabBar::tab { background: transparent; color: #98989e; padding: 6px 14px; }
QTabBar::tab:selected { color: #f2f2f7; border-bottom: 2px solid #0a84ff; }
#xbtn { background: transparent; color: #98989e; border: none; font-size: 16px; padding: 0 6px; }
#xbtn:hover { color: #ff5252; }
"""


class DropZone(QLabel):
    dropped = Signal(list)

    def __init__(self):
        super().__init__("把文件拖到这里(可一次拖多个)\n拖不动就点下面「选择文件」")
        self.setObjectName("dropzone")
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(96)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self.setProperty("hot", True)
            self.style().unpolish(self); self.style().polish(self)

    def dragLeaveEvent(self, _e):
        self.setProperty("hot", False)
        self.style().unpolish(self); self.style().polish(self)

    def dropEvent(self, e: QDropEvent):
        self.setProperty("hot", False)
        self.style().unpolish(self); self.style().polish(self)
        paths = [u.toLocalFile() for u in e.mimeData().urls()
                 if u.isLocalFile() and os.path.isfile(u.toLocalFile())]
        if paths:
            self.dropped.emit(paths)


class Chip(QFrame):
    """暂存的一个文件:名字+大小+进度+×删除。"""
    remove = Signal(str)

    def __init__(self, path):
        super().__init__()
        self.setObjectName("chip")
        self.path = path
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 6, 6)
        name = os.path.basename(path)
        try:
            sz = human_size(os.path.getsize(path))
        except OSError:
            sz = "?"
        self.label = QLabel(f"{name}  ·  {sz}")
        self.label.setStyleSheet("color:#f2f2f7;font-size:13px;")
        self.bar = QProgressBar()
        self.bar.setMaximumWidth(90)
        self.bar.setVisible(False)
        self.bar.setTextVisible(False)
        self.xbtn = QPushButton("✕")
        self.xbtn.setObjectName("xbtn")
        self.xbtn.setFixedWidth(28)
        self.xbtn.clicked.connect(lambda: self.remove.emit(self.path))
        lay.addWidget(self.label, 1)
        lay.addWidget(self.bar)
        lay.addWidget(self.xbtn)

    def set_progress(self, sent, total):
        self.bar.setVisible(True)
        self.xbtn.setVisible(False)
        if total > 0:
            self.bar.setRange(0, total); self.bar.setValue(sent)
        else:
            self.bar.setRange(0, 0)

    def set_done(self, ok):
        self.bar.setVisible(False)
        self.label.setText(("✓ " if ok else "✗ ") + self.label.text())
        self.label.setStyleSheet("color:%s;font-size:13px;" % ("#30d158" if ok else "#ff5252"))


class QuickTransferDialog(QDialog):
    progress_sig = Signal(str, int, int)     # path, sent, total
    item_result_sig = Signal(str, bool, str)  # path, ok, err
    all_done_sig = Signal(str)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.chips = {}      # path -> Chip
        self.setWindowTitle("听晓快传")
        self.setMinimumSize(440, 560)
        self.setStyleSheet(XFER_QSS)
        self.setFont(ui_font(13))

        self._sending = False
        self.tabs = QTabWidget()
        self.tabs.addTab(self._send_tab(), "发送")
        self._hist_scroll = QScrollArea()
        self._hist_scroll.setWidgetResizable(True)
        self.tabs.addTab(self._hist_scroll, "发送记录")
        self._reload_history()
        self.tabs.currentChanged.connect(
            lambda i: self._reload_history() if i == 1 else None)
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.addWidget(self.tabs)

        self.progress_sig.connect(self._on_progress)
        self.item_result_sig.connect(self._on_item_result)
        self.all_done_sig.connect(self._on_all_done)

    # ---- 发送页 ----

    def _send_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 8, 0, 0)
        v.addWidget(QLabel("发送到"))
        self.peer_box = QComboBox()
        self.refresh_peers(self.service.peers())
        v.addWidget(self.peer_box)

        drop = DropZone()
        drop.dropped.connect(self._add_files)
        v.addWidget(drop)

        # 选择文件按钮:Windows 拖拽常因权限/环境失效,点这个一定能选
        pick = QPushButton("＋ 选择文件…")
        pick.clicked.connect(self._pick_files)
        v.addWidget(pick)

        # 暂存卡片区(可滚动)
        self.stage = QVBoxLayout()
        self.stage.setSpacing(6)
        self.stage.addStretch(1)
        holder = QWidget(); holder.setLayout(self.stage)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(holder)
        scroll.setMinimumHeight(140)
        v.addWidget(scroll, 1)

        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("也可以直接发一段文字")
        self.text.setMaximumHeight(70)
        v.addWidget(self.text)

        row = QHBoxLayout()
        row.addWidget(QLabel(f"本机:{self.service.name}"))
        row.addStretch(1)
        self.send_btn = QPushButton("发送")
        self.send_btn.setProperty("primary", True)
        self.send_btn.clicked.connect(self._send)
        row.addWidget(self.send_btn)
        v.addLayout(row)
        return w

    def refresh_peers(self, peers):
        cur = self.peer_box.currentData() if self.peer_box.count() else None
        self.peer_box.clear()
        if not peers:
            self.peer_box.addItem("(没发现设备 — 确认对方也开着听晓、在同一WiFi)", None)
        for p in peers:
            self.peer_box.addItem(f"{p['name']}  ({p['ip']})", p)
        if cur:
            for i in range(self.peer_box.count()):
                d = self.peer_box.itemData(i)
                if d and d.get("id") == cur.get("id"):
                    self.peer_box.setCurrentIndex(i); break

    def _pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择要发送的文件",
                                                os.path.expanduser("~"))
        files = [p for p in paths if p and os.path.isfile(p)]
        if files:
            self._add_files(files)

    def _add_files(self, paths):
        for p in paths:
            if p in self.chips:
                continue
            chip = Chip(p)
            chip.remove.connect(self._remove_file)
            self.chips[p] = chip
            self.stage.insertWidget(self.stage.count() - 1, chip)

    def _remove_file(self, path):
        chip = self.chips.pop(path, None)
        if chip:
            chip.setParent(None)

    def _send(self):
        import threading

        if self._sending:
            return
        peer = self.peer_box.currentData()
        if not peer:
            QMessageBox.information(self, "快传", "还没发现对方设备。确认对方也开着听晓、连同一 WiFi。")
            return
        files = list(self.chips.keys())    # 只发暂存区里还在的(发成功的会被移走)
        text = self.text.toPlainText().strip()
        if not files and not text:
            QMessageBox.information(self, "快传", "先拖入文件,或输入要发送的文字。")
            return
        self._sending = True
        self.send_btn.setEnabled(False)

        def work():
            try:
                if text:
                    ok, err = self.service.send_text(peer, text)
                    self.item_result_sig.emit("__text__", ok, err)
                for path in files:
                    ok, err = self.service.send_file(
                        peer, path,
                        progress=lambda s, t, p=path: self.progress_sig.emit(p, s, t))
                    self.item_result_sig.emit(path, ok, err)
            finally:
                self.all_done_sig.emit("")

        threading.Thread(target=work, daemon=True).start()

    def _on_progress(self, path, sent, total):
        c = self.chips.get(path)
        if c:
            c.set_progress(sent, total)

    def _on_item_result(self, path, ok, err):
        if path == "__text__":
            if ok:
                self.text.clear()
            elif err:
                QMessageBox.warning(self, "快传", f"文字发送失败:{err}")
            return
        if ok:
            # 发送成功:从暂存区移走,归档到发送记录
            self._remove_file(path)
        else:
            c = self.chips.get(path)
            if c:
                c.set_done(False)
                if err:
                    c.label.setText(c.label.text() + f"  ({err})")

    def _on_all_done(self, _):
        self._sending = False
        self.send_btn.setEnabled(True)
        self._reload_history()
        remaining = len(self.chips)
        self.setWindowTitle("听晓快传" + (f" — 还剩{remaining}个待发/失败" if remaining else " — 已发送 ✓"))

    # ---- 历史页 ----

    def _reload_history(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 8, 0, 0)
        recs = self.service.history("sent")
        if not recs:
            v.addWidget(QLabel("暂无发送记录"))
        for r in recs:
            ts = r.get("ts", "")
            when = f"{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}" if len(ts) >= 13 else ""
            to = r.get("to", "")
            desc = r.get("name") or r.get("text", "")
            lbl = QLabel(f"{when}  →{to}  {desc[:40]}")
            lbl.setStyleSheet("color:#c7c7cc;font-size:12px;padding:4px 2px;"
                              "border-bottom:1px solid #3a3a3c;")
            v.addWidget(lbl)
        v.addStretch(1)
        self._hist_scroll.setWidget(w)


class RecvRow(QFrame):
    """一个正在接收的文件:名字 + 进度条 + 百分比·速度。"""

    def __init__(self, ev):
        super().__init__()
        self.setObjectName("chip")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)
        self.title = QLabel()
        self.title.setStyleSheet("color:#f2f2f7;font-size:13px;")
        lay.addWidget(self.title)
        row = QHBoxLayout(); row.setSpacing(8)
        self.bar = QProgressBar(); self.bar.setTextVisible(False)
        row.addWidget(self.bar, 1)
        self.stat = QLabel()
        self.stat.setStyleSheet("color:#98989e;font-size:12px;")
        self.stat.setMinimumWidth(150)
        self.stat.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.stat)
        lay.addLayout(row)
        self.update_ev(ev)

    def update_ev(self, ev):
        name = ev.get("name", "文件")
        frm = ev.get("from", "")
        got, total = int(ev.get("got", 0)), int(ev.get("total", 0))
        self.title.setText(f"⬇ {name}   来自 {frm}")
        if total > 0:
            self.bar.setRange(0, total); self.bar.setValue(min(got, total))
            pct = int(got * 100 / total)
        else:
            self.bar.setRange(0, 0); pct = 0
        self.stat.setText(f"{pct}%  ·  {human_size(got)}/{human_size(total)}"
                          f"  ·  {speed_str(ev.get('speed', 0))}")


class InboxDialog(QDialog):
    """收件箱:实时接收进度/速度 + 接收记录 + 打开文件/所在文件夹。"""
    recv_sig = Signal(dict)   # 接收进度事件(主线程转发进来)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self._active = {}     # recv_id -> RecvRow
        self.setWindowTitle("听晓快传 · 收件箱")
        self.setMinimumSize(500, 580)
        self.setStyleSheet(XFER_QSS)
        self.setFont(ui_font(13))

        top = QHBoxLayout()
        top.addWidget(QLabel(f"收件夹:{service.save_dir}"))
        top.addStretch(1)
        openf = QPushButton("打开收件夹")
        openf.clicked.connect(lambda: open_path(service.save_dir))
        top.addWidget(openf)

        # 正在接收区(有活动接收时才显示)
        self._active_box = QWidget()
        abv = QVBoxLayout(self._active_box)
        abv.setContentsMargins(0, 4, 0, 4); abv.setSpacing(6)
        lbl = QLabel("正在接收")
        lbl.setStyleSheet("color:#98989e;font-size:12px;")
        abv.addWidget(lbl)
        self._active_lay = QVBoxLayout(); self._active_lay.setSpacing(6)
        abv.addLayout(self._active_lay)
        self._active_box.setVisible(False)

        self._hist_scroll = QScrollArea()
        self._hist_scroll.setWidgetResizable(True)
        self._reload_history()

        close = QPushButton("关闭"); close.clicked.connect(self.reject)
        bottom = QHBoxLayout(); bottom.addStretch(1); bottom.addWidget(close)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.addLayout(top)
        root.addWidget(self._active_box)
        root.addWidget(QLabel("接收记录"))
        root.addWidget(self._hist_scroll, 1)
        root.addLayout(bottom)

        self.recv_sig.connect(self._on_recv)

    # ---- 实时接收 ----

    def _on_recv(self, ev):
        phase = ev.get("phase")
        rid = ev.get("id")
        if phase in ("start", "progress"):
            row = self._active.get(rid)
            if not row:
                row = RecvRow(ev)
                self._active[rid] = row
                self._active_lay.insertWidget(0, row)
                self._active_box.setVisible(True)
            else:
                row.update_ev(ev)
        elif phase == "done":
            row = self._active.pop(rid, None)
            if row:
                row.setParent(None)
                row.deleteLater()
            if not self._active:
                self._active_box.setVisible(False)
            self._reload_history()

    # ---- 记录 ----

    def _reload_history(self):
        listw = QWidget()
        lv = QVBoxLayout(listw)
        lv.setContentsMargins(0, 6, 0, 0)
        recs = self.service.history("recv")
        if not recs:
            lv.addWidget(QLabel("还没有收到任何东西"))
        for r in recs:
            lv.addWidget(self._row(r))
        lv.addStretch(1)
        self._hist_scroll.setWidget(listw)

    def _row(self, r):
        f = QFrame(); f.setObjectName("chip")
        h = QHBoxLayout(f); h.setContentsMargins(10, 8, 8, 8)
        ts = r.get("ts", "")
        when = f"{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}" if len(ts) >= 13 else ""
        frm = r.get("from", "")
        if r.get("kind") == "file":
            name = r.get("name", "文件")
            extra = f" · 均速 {speed_str(r['avg_bps'])}" if r.get("avg_bps") else ""
            info = QLabel(f"{name}\n{when} · 来自 {frm} · {human_size(r.get('size',0))}{extra}")
        else:
            info = QLabel(f"[文字] {r.get('text','')[:30]}\n{when} · 来自 {frm}")
        info.setStyleSheet("color:#f2f2f7;font-size:13px;")
        h.addWidget(info, 1)
        path = r.get("path")
        if path:
            of = QPushButton("打开"); of.setFixedWidth(52)
            of.clicked.connect(lambda _=0, p=path: open_path(p))
            rv = QPushButton("所在文件夹"); rv.setFixedWidth(96)
            rv.clicked.connect(lambda _=0, p=path: reveal_path(p))
            h.addWidget(of); h.addWidget(rv)
        return f
