"""热词管理:手工添加「正确写法 ← 常被听成的样子」,本地即时替换(不需联网)。

托盘菜单和主界面都能打开。保存后 app._reload_corrector() 立即生效。
热词是确定性替换(apply_hotwords),比 LLM 猜更可靠;新加的英文正确词会顺带
进专有词表,防止 LLM 又把它改回别的相近词。
"""
import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from ui import QSS, ui_font


class VocabDialog(QDialog):
    """需要宿主 app 提供:cfg / _save_config() / _reload_corrector()。"""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWindowTitle("热词管理")
        self.setMinimumSize(500, 560)
        self.setStyleSheet(QSS)
        self.setFont(ui_font(13))
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        # 快照,取消不改动原配置
        self._hot = {k: list(v) for k, v in (app.cfg.get("hotwords") or {}).items()}

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        title = QLabel("热词管理")
        title.setStyleSheet("font-size:16px;font-weight:600;")
        root.addWidget(title)
        tip = QLabel(
            "你说的词老被听错?把「正确写法」和它「常被听成的样子」加进来,\n"
            "听晓出字时会本地即时替换——不用联网、瞬间生效、比 AI 猜更可靠。")
        tip.setStyleSheet("color:#98989e;font-size:12px;")
        tip.setWordWrap(True)
        root.addWidget(tip)

        # 添加区
        add = QHBoxLayout()
        self.right = QLineEdit()
        self.right.setPlaceholderText("正确写法,如 Chatcut")
        self.wrong = QLineEdit()
        self.wrong.setPlaceholderText("常被听成(可多个,逗号分隔),如 chat cut, chet cut")
        self.right.returnPressed.connect(self._add)
        self.wrong.returnPressed.connect(self._add)
        addbtn = QPushButton("＋ 添加")
        addbtn.setProperty("primary", True)
        addbtn.clicked.connect(self._add)
        add.addWidget(self.right, 2)
        add.addWidget(self.wrong, 3)
        add.addWidget(addbtn)
        root.addLayout(add)

        # 列表
        self._list = QVBoxLayout()
        self._list.setSpacing(6)
        holder = QWidget()
        holder.setLayout(self._list)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)
        root.addWidget(scroll, 1)

        # 底部按钮
        bot = QHBoxLayout()
        bot.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存")
        save.setProperty("primary", True)
        save.clicked.connect(self._save)
        bot.addWidget(cancel)
        bot.addWidget(save)
        root.addLayout(bot)

        self._render()

    def _render(self):
        while self._list.count():
            it = self._list.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if not self._hot:
            empty = QLabel("还没有热词。上面加一个试试(例如:Chatcut ← chat cut)。")
            empty.setStyleSheet("color:#98989e;")
            self._list.addWidget(empty)
        for right in list(self._hot.keys()):
            wrongs = self._hot[right]
            f = QFrame()
            f.setObjectName("chip")
            f.setStyleSheet("#chip{background:#f2f2f7;border-radius:8px;}")
            h = QHBoxLayout(f)
            h.setContentsMargins(10, 6, 6, 6)
            lbl = QLabel(f"<b>{right}</b>　←　{'、'.join(wrongs)}")
            lbl.setWordWrap(True)
            lbl.setTextFormat(Qt.RichText)
            h.addWidget(lbl, 1)
            dele = QPushButton("删除")
            dele.setFixedWidth(56)
            dele.clicked.connect(lambda _c=False, r=right: self._del(r))
            h.addWidget(dele)
            self._list.addWidget(f)
        self._list.addStretch(1)

    def _add(self):
        right = self.right.text().strip()
        wrong_raw = self.wrong.text().strip()
        if not right:
            QMessageBox.information(self, "添加热词", "请先填「正确写法」。")
            return
        wrongs = [w.strip() for w in wrong_raw.replace("，", ",").split(",") if w.strip()]
        if not wrongs:
            QMessageBox.information(self, "添加热词", "请填至少一个「常被听成」的形式。")
            return
        cur = self._hot.get(right, [])
        for w in wrongs:
            if w and w != right and w not in cur:
                cur.append(w)
        self._hot[right] = cur
        self.right.clear()
        self.wrong.clear()
        self.right.setFocus()
        self._render()

    def _del(self, right):
        self._hot.pop(right, None)
        self._render()

    def _save(self):
        self.app.cfg["hotwords"] = self._hot
        # 新加的英文正确词顺带进专有词表,防止 LLM 又把它改回别的相近词
        gl = list(self.app.cfg.get("glossary") or [])
        if not gl:
            try:
                from corrector import DEFAULT_GLOSSARY
                gl = list(DEFAULT_GLOSSARY)
            except Exception:
                gl = []
        for r in self._hot:
            if r and re.search(r"[A-Za-z]", r) and r not in gl:
                gl.append(r)
        self.app.cfg["glossary"] = gl
        try:
            self.app._save_config()
            self.app._reload_corrector()
        except Exception:
            import traceback
            traceback.print_exc()
        QMessageBox.information(self, "热词管理", "已保存,立即生效。")
        self.accept()
