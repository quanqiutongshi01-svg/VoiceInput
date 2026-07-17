"""听晓快传窗口:拖文件/贴文字进来 → 选设备 → 发送。macOS 风格深色。"""
import os

from PySide6.QtCore import Qt, Signal, QMimeData
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QVBoxLayout, QWidget,
)

from ui import QSS, ui_font

DROP_QSS = QSS + """
#dropzone { background: #2c2c2e; border: 2px dashed #48484a; border-radius: 12px;
            color: #98989e; font-size: 14px; }
#dropzone[hot="true"] { border-color: #0a84ff; color: #0a84ff; background: #23324a; }
QPlainTextEdit { background: #2c2c2e; border: 1px solid #3a3a3c; border-radius: 8px;
                 color: #f2f2f7; padding: 6px; font-size: 13px; }
QProgressBar { background: #2c2c2e; border: none; border-radius: 4px; height: 6px; }
QProgressBar::chunk { background: #0a84ff; border-radius: 4px; }
"""


class DropZone(QLabel):
    dropped = Signal(list)

    def __init__(self):
        super().__init__("把文件拖到这里,或在下面贴文字")
        self.setObjectName("dropzone")
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(120)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self.setProperty("hot", True)
            self.style().unpolish(self)
            self.style().polish(self)

    def dragLeaveEvent(self, _e):
        self.setProperty("hot", False)
        self.style().unpolish(self)
        self.style().polish(self)

    def dropEvent(self, e: QDropEvent):
        self.setProperty("hot", False)
        self.style().unpolish(self)
        self.style().polish(self)
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.dropped.emit(paths)


class QuickTransferDialog(QDialog):
    """service: TransferService 实例。send 在后台线程,进度经信号回主线程。"""

    progress_sig = Signal(int, int)
    result_sig = Signal(bool, str)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.files = []
        self.setWindowTitle("听晓快传")
        self.setMinimumSize(420, 520)
        self.setStyleSheet(DROP_QSS)
        self.setFont(ui_font(13))

        title = QLabel("发送到")
        title.setStyleSheet("font-size:13px;color:#98989e;")
        self.peer_box = QComboBox()
        self.refresh_peers(service.peers())

        self.drop = DropZone()
        self.drop.dropped.connect(self._add_files)
        self.file_label = QLabel("")
        self.file_label.setProperty("dim", True)
        self.file_label.setWordWrap(True)

        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("要发送的文字(和文件二选一或都发)")
        self.text.setMaximumHeight(90)

        self.bar = QProgressBar()
        self.bar.setVisible(False)
        self.bar.setTextVisible(False)

        self.send_btn = QPushButton("发送")
        self.send_btn.setProperty("primary", True)
        self.send_btn.clicked.connect(self._send)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.reject)
        btns = QHBoxLayout()
        btns.addWidget(QLabel(f"本机:{service.name}"))
        btns.addStretch(1)
        btns.addWidget(close_btn)
        btns.addWidget(self.send_btn)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.addWidget(title)
        root.addWidget(self.peer_box)
        root.addSpacing(8)
        root.addWidget(self.drop)
        root.addWidget(self.file_label)
        root.addSpacing(6)
        root.addWidget(self.text, 1)
        root.addWidget(self.bar)
        root.addLayout(btns)

        self.progress_sig.connect(self._on_progress)
        self.result_sig.connect(self._on_result)

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
                    self.peer_box.setCurrentIndex(i)
                    break

    def _add_files(self, paths):
        self.files = list(paths)
        n = len(paths)
        names = ", ".join(os.path.basename(p) for p in paths[:3])
        self.file_label.setText(f"已选 {n} 个:{names}" + (" …" if n > 3 else ""))

    def _send(self):
        import threading

        peer = self.peer_box.currentData()
        if not peer:
            QMessageBox.information(self, "快传", "还没发现对方设备。请确认对方电脑也开着听晓,并连在同一个 WiFi。")
            return
        text = self.text.toPlainText().strip()
        if not self.files and not text:
            QMessageBox.information(self, "快传", "先拖入文件,或输入要发送的文字。")
            return
        self.send_btn.setEnabled(False)
        self.bar.setVisible(True)
        self.bar.setRange(0, 0)  # 忙碌态,发文件时切确定进度

        def work():
            try:
                if text:
                    ok, err = self.service.send_text(peer, text)
                    if not ok:
                        return self.result_sig.emit(False, f"文字发送失败:{err}")
                for i, path in enumerate(self.files):
                    self.bar_range_reset()
                    ok, err = self.service.send_file(
                        peer, path,
                        progress=lambda s, t: self.progress_sig.emit(s, t))
                    if not ok:
                        return self.result_sig.emit(
                            False, f"{os.path.basename(path)} 发送失败:{err}")
                self.result_sig.emit(True, "发送完成 ✓")
            except Exception as e:
                self.result_sig.emit(False, f"发送出错:{e}")

        threading.Thread(target=work, daemon=True).start()

    def bar_range_reset(self):
        self.progress_sig.emit(0, 0)

    def _on_progress(self, sent, total):
        if total <= 0:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, total)
            self.bar.setValue(sent)

    def _on_result(self, ok, msg):
        self.bar.setVisible(False)
        self.send_btn.setEnabled(True)
        if ok:
            self.text.clear()
            self.files = []
            self.file_label.setText("")
            self.setWindowTitle("听晓快传 — " + msg)
        else:
            QMessageBox.warning(self, "快传", msg)
