"""声音记忆库设置/查看对话框:开关、主人名、按人统计、导出记忆包、备份到NAS、打开文件夹。"""
import os
import subprocess
import sys
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)


def _open_folder(path):
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


class MemoryBankDialog(QDialog):
    """需要宿主 app 提供:cfg / mem(MemoryBank 或 None)/ _save_config() / _reload_memory_bank()。"""

    backup_done = Signal(dict)   # 后台备份完成 → 主线程刷新

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWindowTitle("声音记忆库")
        self.setMinimumWidth(460)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        mem_cfg = dict(app.cfg.get("memory_bank") or {})

        lay = QVBoxLayout(self)
        title = QLabel("声音记忆库")
        title.setStyleSheet("font-size:16px;font-weight:600;")
        lay.addWidget(title)
        intro = QLabel(
            "把你每天说过的话——原始音质录音、文字、声纹——按人永久留存。\n"
            "为将来想留住一个人的声音和说话的样子,备一份底稿。")
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#666;")
        lay.addWidget(intro)

        # 开关 + 主人名
        row = QHBoxLayout()
        self.chk = QCheckBox("开启记忆库(本机说话由下面这个人归档)")
        self.chk.setChecked(bool(mem_cfg.get("enabled", False)))
        self.chk.stateChanged.connect(self._on_toggle)
        row.addWidget(self.chk)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("这台设备的主人:"))
        self.name = QLineEdit(mem_cfg.get("speaker_name", "我"))
        self.name.setPlaceholderText("例如:你自己的名字")
        self.name.editingFinished.connect(self._on_name)
        row2.addWidget(self.name)
        lay.addLayout(row2)

        # 统计区
        self.stats_box = QLabel()
        self.stats_box.setWordWrap(True)
        self.stats_box.setStyleSheet(
            "background:#f5f5f7;border-radius:8px;padding:10px;color:#333;")
        lay.addWidget(self.stats_box)

        # 库信息:位置 / 占用 / 上次备份(位置可选中复制)
        self.info_box = QLabel()
        self.info_box.setWordWrap(True)
        self.info_box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.info_box.setStyleSheet(
            "background:#eef3fb;border-radius:8px;padding:10px;color:#334;font-size:12px;")
        lay.addWidget(self.info_box)

        # 备份区:目标选择 + 可选释放本地音频
        self.chk_free = QCheckBox("备份校验通过后,删除本地已备份的录音(释放硬盘;文字和声纹永远保留)")
        self.chk_free.setChecked(False)
        lay.addWidget(self.chk_free)
        bkrow = QHBoxLayout()
        self.btn_backup = QPushButton("备份到 NAS / 文件夹…")
        self.btn_backup.clicked.connect(self._backup)
        bkrow.addWidget(self.btn_backup)
        bkrow.addStretch(1)
        lay.addLayout(bkrow)

        # 导出人选择
        exprow = QHBoxLayout()
        exprow.addWidget(QLabel("导出谁的记忆包:"))
        self.who = QComboBox()
        exprow.addWidget(self.who, 1)
        lay.addLayout(exprow)

        # 按钮
        btns = QHBoxLayout()
        self.btn_export = QPushButton("导出声音记忆包…")
        self.btn_export.clicked.connect(self._export)
        btns.addWidget(self.btn_export)
        b_open = QPushButton("打开记忆库文件夹")
        b_open.clicked.connect(self._open_dir)
        btns.addWidget(b_open)
        btns.addStretch(1)
        b_close = QPushButton("关闭")
        b_close.clicked.connect(self.accept)
        btns.addWidget(b_close)
        lay.addLayout(btns)

        note = QLabel(
            "隐私:全部只存在本机,绝不上传。你和家人各自的记忆各自掌管、随时可删。\n"
            "给家人开启前,请当面跟对方讲清楚并征得同意。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#999;font-size:11px;")
        lay.addWidget(note)

        self.backup_done.connect(self._on_backup_done)
        self._refresh()

    def _mem(self):
        return getattr(self.app, "mem", None)

    def _refresh(self):
        mem = self._mem()
        self.who.clear()
        if mem is None:
            self.stats_box.setText("记忆库尚未就绪(引擎还在加载或初始化失败)。")
            self.info_box.setText("")
            self.btn_export.setEnabled(False)
            self.btn_backup.setEnabled(False)
            return
        # 库信息:位置 / 磁盘占用 / 上次备份
        try:
            usage = mem.disk_usage_mb()
            st = mem.backup_state()
            last = (f"上次备份:{st.get('ts')} → {st.get('dest')}"
                    + (f"(释放了 {st['freed_mb']}MB)" if st.get("freed_mb") else "")
                    if st.get("ts") else "还没备份过。建议定期备份到 NAS,再勾选上面选项释放本地空间。")
            self.info_box.setText(
                f"📁 位置:{mem.root}\n"
                f"💾 本地占用:{usage:.0f} MB\n"
                f"🗄 {last}")
        except Exception:
            self.info_box.setText(f"📁 位置:{mem.root}")
        speakers = mem.list_speakers()
        if not speakers:
            self.stats_box.setText(
                "还没有任何记忆。开启后,你之后的每句听写都会自动归档到这里。")
            self.btn_export.setEnabled(False)
        else:
            lines = []
            for sp in speakers:
                s = mem.stats(sp)
                vp = "声纹已就绪" if s["voiceprint_ready"] else "声纹积累中"
                lines.append(
                    f"● {s['speaker']}:{s['count']} 句 / 约 {s['minutes']} 分钟 / "
                    f"{s['chars']} 字 / {s['size_mb']}MB / {vp}")
                self.who.addItem(sp)
            # 给一个"离克隆还差多少"的直觉:一般 10~30 分钟干净语音可出可用音色
            total_min = sum(mem.stats(sp)["minutes"] for sp in speakers)
            hint = ""
            if total_min < 10:
                hint = f"\n\n提示:目前共约 {total_min:.0f} 分钟。一般攒到 10~30 分钟干净语音," \
                       "就够克隆出可用的音色了——继续正常用听晓即可,零额外操作。"
            self.stats_box.setText("\n".join(lines) + hint)
            self.btn_export.setEnabled(True)

    def _on_toggle(self):
        on = self.chk.isChecked()
        cfg = self.app.cfg.setdefault("memory_bank", {})
        if on and not cfg.get("_consented"):
            ok = QMessageBox.question(
                self, "开启声音记忆库",
                "开启后,这台设备上你之后说的每一句话(录音+文字+声纹)都会永久留在本机,"
                "用于将来留住/复刻声音。\n\n数据只存本机、绝不上传,你随时能删。\n\n确定开启吗?",
                QMessageBox.Yes | QMessageBox.No)
            if ok != QMessageBox.Yes:
                self.chk.setChecked(False)
                return
            cfg["_consented"] = True
        cfg["enabled"] = on
        if "speaker_name" not in cfg:
            cfg["speaker_name"] = self.name.text().strip() or "我"
        self.app._save_config()
        self.app._reload_memory_bank()
        self._refresh()

    def _on_name(self):
        cfg = self.app.cfg.setdefault("memory_bank", {})
        nm = self.name.text().strip() or "我"
        if cfg.get("speaker_name") != nm:
            cfg["speaker_name"] = nm
            self.app._save_config()
            self.app._reload_memory_bank()
            self._refresh()

    def _open_dir(self):
        mem = self._mem()
        if mem is None:
            return
        os.makedirs(mem.root, exist_ok=True)
        _open_folder(mem.root)

    def _export(self):
        mem = self._mem()
        if mem is None or self.who.count() == 0:
            return
        speaker = self.who.currentText()
        out = QFileDialog.getExistingDirectory(
            self, "选择保存位置", os.path.expanduser("~/Desktop"))
        if not out:
            return
        try:
            ok, msg, pack = mem.export(speaker, out)
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))
            return
        if ok:
            QMessageBox.information(self, "导出完成", f"{msg}\n\n{pack}")
            _open_folder(pack)
        else:
            QMessageBox.warning(self, "导出失败", msg)

    # ---- 备份到 NAS / 文件夹 ----

    def _backup(self):
        mem = self._mem()
        if mem is None:
            return
        dest = QFileDialog.getExistingDirectory(
            self, "选择备份目标(NAS 挂载盘或任意文件夹)", os.path.expanduser("~"))
        if not dest:
            return
        free = self.chk_free.isChecked()
        if free:
            ret = QMessageBox.question(
                self, "释放本地空间",
                "备份校验通过后,将删除本地已备份的录音文件(.wav)。\n\n"
                "删除后,NAS 上的备份就是这些录音的唯一副本——\n"
                "请确认备份目标可靠(建议 NAS 自身也有冗余)。\n\n"
                "本地保留:文字清单、声纹、统计(不影响日常使用与继续积累)。\n\n确定继续吗?",
                QMessageBox.Yes | QMessageBox.No)
            if ret != QMessageBox.Yes:
                return
        self.btn_backup.setEnabled(False)
        self.btn_backup.setText("备份中…(大库首次备份会久一些)")

        def work():
            try:
                r = mem.backup(dest, free_audio=free)
            except Exception as e:
                r = {"ok": False, "err": str(e), "dest": dest,
                     "copied": 0, "freed_mb": 0.0, "total_mb": 0.0}
            self.backup_done.emit(r)

        threading.Thread(target=work, daemon=True).start()

    def _on_backup_done(self, r):
        self.btn_backup.setEnabled(True)
        self.btn_backup.setText("备份到 NAS / 文件夹…")
        if r.get("ok"):
            msg = (f"已同步 {r['copied']} 个新文件到:\n{r['dest']}\n\n"
                   f"库总量约 {r['total_mb']:.0f} MB")
            if r.get("freed_mb"):
                msg += f"\n本地已释放 {r['freed_mb']} MB(录音已安全转移)"
            QMessageBox.information(self, "备份完成", msg)
        else:
            QMessageBox.warning(self, "备份失败", r.get("err") or "未知错误")
        self._refresh()
