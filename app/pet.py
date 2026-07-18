"""听晓桌宠:置顶透明悬浮的小生物,随听写状态做动作。纯 QPainter 手绘,无需素材。

状态:idle(待命·呼吸+眨眼) / listening(在听·耳朵竖起+声波) /
     processing(思考·歪头+转圈) / happy(完成·蹦跳+爱心) / sleep(久置·睡觉Zzz)
形象:内置几套配色(奶黄/薄荷/樱粉/天蓝),config 里选;可拖动;点它有反应。
"""
import math
import random

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QRadialGradient, QFont, QRegion
from PySide6.QtWidgets import QWidget, QApplication

SKINS = {
    "奶黄": {"body": QColor(255, 214, 120), "belly": QColor(255, 236, 190),
            "ear": QColor(255, 196, 90), "cheek": QColor(255, 150, 140)},
    "薄荷": {"body": QColor(150, 226, 200), "belly": QColor(210, 245, 230),
            "ear": QColor(120, 205, 175), "cheek": QColor(255, 160, 150)},
    "樱粉": {"body": QColor(255, 190, 210), "belly": QColor(255, 224, 234),
            "ear": QColor(245, 160, 185), "cheek": QColor(255, 130, 140)},
    "天蓝": {"body": QColor(150, 200, 255), "belly": QColor(210, 230, 255),
            "ear": QColor(120, 175, 240), "cheek": QColor(255, 150, 150)},
}


def draw_pet(p, size, skin, state, phase, level=0.0, blink=1.0, look=(0.0, 0.0)):
    """把桌宠画到 QPainter p(size×size)。phase 递增用于动画。"""
    c = SKINS.get(skin, SKINS["奶黄"])
    p.setRenderHint(QPainter.Antialiasing)
    cx = size / 2
    bob = math.sin(phase * 0.12) * size * 0.02        # 呼吸上下浮动
    if state == "happy":
        bob -= abs(math.sin(phase * 0.5)) * size * 0.10  # 蹦跳
    baseY = size * 0.60 + bob
    bw = size * 0.42                                   # 身体半宽
    bh = size * 0.36                                   # 身体半高
    squash = 1.0
    if state == "happy":
        squash = 1.0 + math.sin(phase * 0.5) * 0.06

    # 影子
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0, 0, 0, 40))
    p.drawEllipse(QRectF(cx - bw * 0.7, size * 0.86, bw * 1.4, size * 0.06))

    # 耳朵(listening 时竖起)
    ear_up = 1.0 if state == "listening" else 0.35
    p.setBrush(c["ear"])
    for sgn in (-1, 1):
        ex = cx + sgn * bw * 0.55
        ey = baseY - bh * (0.7 + 0.5 * ear_up)
        path = QPainterPath()
        path.moveTo(ex, baseY - bh * 0.4)
        path.quadTo(ex + sgn * size * 0.05, ey, ex + sgn * size * 0.01, ey - size * 0.02)
        path.quadTo(ex - sgn * size * 0.04, ey + size * 0.04, ex - sgn * size * 0.02, baseY - bh * 0.5)
        path.closeSubpath()
        p.drawPath(path)

    # 身体
    grad = QRadialGradient(cx, baseY - bh * 0.3, bw * 1.3)
    grad.setColorAt(0, c["body"].lighter(108))
    grad.setColorAt(1, c["body"])
    p.setBrush(grad)
    p.drawEllipse(QRectF(cx - bw * squash, baseY - bh / squash, 2 * bw * squash, 2 * bh / squash))
    # 肚皮
    p.setBrush(c["belly"])
    p.drawEllipse(QRectF(cx - bw * 0.6, baseY - bh * 0.1, bw * 1.2, bh * 1.2))

    # 腮红
    p.setBrush(QColor(c["cheek"].red(), c["cheek"].green(), c["cheek"].blue(), 150))
    for sgn in (-1, 1):
        p.drawEllipse(QRectF(cx + sgn * bw * 0.5 - size * 0.03, baseY + bh * 0.15,
                             size * 0.09, size * 0.06))

    # 眼睛(眨眼 blink: 1开 0闭;look 视线偏移)
    eyeY = baseY - bh * 0.25
    ex_off = bw * 0.34
    lx, ly = look[0] * size * 0.02, look[1] * size * 0.02
    for sgn in (-1, 1):
        exx = cx + sgn * ex_off
        p.setBrush(QColor(40, 35, 45))
        if state == "sleep" or blink < 0.15:
            p.setPen(QColor(40, 35, 45))
            p.drawArc(QRectF(exx - size * 0.035, eyeY - size * 0.01, size * 0.07, size * 0.04),
                      0, 180 * 16)
            p.setPen(Qt.NoPen)
        else:
            eh = size * 0.055 * blink
            p.drawEllipse(QRectF(exx - size * 0.028 + lx, eyeY - eh / 2 + ly, size * 0.056, eh))
            p.setBrush(Qt.white)  # 高光
            p.drawEllipse(QRectF(exx - size * 0.006 + lx, eyeY - eh * 0.32 + ly,
                                 size * 0.018, size * 0.018))
            p.setBrush(QColor(40, 35, 45))

    # 嘴巴(状态不同)
    p.setPen(Qt.NoPen)
    my = baseY + bh * 0.02
    if state == "listening":
        # 张口(随音量大小)
        mh = size * (0.03 + min(0.06, level * 0.5))
        p.setBrush(QColor(200, 90, 90))
        p.drawEllipse(QRectF(cx - size * 0.03, my, size * 0.06, mh))
    elif state == "happy":
        p.setPen(QColor(150, 60, 60))
        pth = QPainterPath(); pth.moveTo(cx - size * 0.05, my)
        pth.quadTo(cx, my + size * 0.05, cx + size * 0.05, my)
        p.drawPath(pth); p.setPen(Qt.NoPen)
    else:
        p.setPen(QColor(150, 90, 90))
        p.drawArc(QRectF(cx - size * 0.03, my - size * 0.01, size * 0.06, size * 0.03), 200 * 16, 140 * 16)
        p.setPen(Qt.NoPen)

    # 状态装饰
    if state == "listening":
        # 头顶声波
        p.setPen(Qt.NoPen)
        for i in range(4):
            a = 90 - i * 40
            if a <= 0:
                break
            col = QColor(10, 132, 255, a)
            p.setBrush(col)
            rr = size * (0.06 + i * 0.04) + math.sin(phase * 0.3 + i) * size * 0.01
            p.drawEllipse(QRectF(cx + bw * 0.75, baseY - bh * 1.3 - i * size * 0.02,
                                 rr * 0.4, rr * 0.4))
    elif state == "processing":
        # 头顶转圈三点
        for i in range(3):
            ang = phase * 0.25 + i * 2.1
            dx = math.cos(ang) * size * 0.05
            dy = math.sin(ang) * size * 0.02
            p.setBrush(QColor(255, 200, 60, 220))
            p.drawEllipse(QRectF(cx + dx - size * 0.012, baseY - bh * 1.5 + dy,
                                 size * 0.024, size * 0.024))
    elif state == "happy":
        # 爱心/星星飘
        for i in range(3):
            t = (phase * 0.06 + i * 0.4) % 1.0
            hx = cx + (i - 1) * size * 0.16
            hy = baseY - bh * 1.2 - t * size * 0.25
            p.setBrush(QColor(255, 120, 150, int(255 * (1 - t))))
            _heart(p, hx, hy, size * 0.05)
    elif state == "sleep":
        f = QFont(); f.setPointSizeF(size * 0.09); p.setFont(f)
        p.setPen(QColor(150, 170, 210))
        for i in range(3):
            t = (phase * 0.03 + i * 0.33) % 1.0
            p.drawText(QPointF(cx + bw * 0.6 + i * size * 0.04,
                               baseY - bh * 1.1 - t * size * 0.2), "Z")
        p.setPen(Qt.NoPen)


def _heart(p, x, y, s):
    path = QPainterPath()
    path.moveTo(x, y + s * 0.3)
    path.cubicTo(x - s, y - s * 0.5, x - s * 0.5, y - s, x, y - s * 0.2)
    path.cubicTo(x + s * 0.5, y - s, x + s, y - s * 0.5, x, y + s * 0.3)
    p.drawPath(path)


class PetWidget(QWidget):
    """桌宠悬浮窗:置顶、透明、可拖动、点它有反应。set_state 切状态。"""
    clicked = Signal()

    SIZE = 150

    def __init__(self, cfg_ui, get_level=None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        if hasattr(Qt, "WA_MacAlwaysShowToolWindow"):
            self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        self.setWindowFlag(Qt.WindowDoesNotAcceptFocus, True)
        self.resize(self.SIZE, self.SIZE)

        self._cfg_ui = cfg_ui or {}
        self._get_level = get_level or (lambda: 0.0)
        self.skin = self._cfg_ui.get("pet_skin", "奶黄")
        self._state = "idle"
        self._phase = 0.0
        self._blink = 1.0
        self._blink_t = 0
        self._idle_secs = 0.0
        self._drag = None
        self._bubble = ""
        self._bubble_until = 0

        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._apply_mask()
        self._place()

    def _apply_mask(self):
        """只让画到桌宠身上的区域接收点击,四周透明处的点击穿透到下面的窗口。
        遮罩=身体/耳朵大椭圆 ∪ 顶部气泡带,足够宽松不裁剪画面。"""
        s = self.SIZE
        body = QRegion(int(s * 0.02), int(s * 0.10), int(s * 0.96), int(s * 0.88),
                       QRegion.Ellipse)
        bubble = QRegion(int(s * 0.02), 0, int(s * 0.96), int(s * 0.28))
        self.setMask(body.united(bubble))

    def reset_position(self):
        """把桌宠拉回默认角落(位置丢到屏幕外时用)。"""
        self._cfg_ui.pop("pet_pos", None)
        self._place()

    def set_state(self, state):
        if state != self._state:
            self._state = state
            self._idle_secs = 0.0

    def say(self, text, secs=3.0):
        self._bubble = text
        self._bubble_until = self._phase + secs / 0.04

    def _place(self):
        pos = self._cfg_ui.get("pet_pos")
        # 只有当保存的位置落在某个当前连接的屏幕上才恢复,否则回默认角落
        # (换了显示器/拔了外接屏时,旧坐标可能在屏幕外)
        if pos and isinstance(pos, list) and len(pos) == 2:
            for scr in QApplication.screens():
                if scr.availableGeometry().adjusted(-40, -40, 40, 40).contains(
                        int(pos[0]), int(pos[1])):
                    self.move(int(pos[0]), int(pos[1]))
                    return
        scr = QApplication.primaryScreen().availableGeometry()
        self.move(scr.right() - self.SIZE - 40, scr.bottom() - self.SIZE - 40)

    def _tick(self):
        self._phase += 1
        # 眨眼
        self._blink_t += 1
        if self._blink_t > random.randint(60, 120):
            self._blink_t = 0
        self._blink = 0.0 if self._blink_t < 3 else 1.0
        # 久置进入睡眠
        if self._state == "idle":
            self._idle_secs += 0.04
            if self._idle_secs > 90:
                self._state = "sleep"
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        lv = 0.0
        try:
            lv = min(1.0, float(self._get_level()) * 8)
        except Exception:
            pass
        draw_pet(p, self.SIZE, self.skin, self._state, self._phase, lv, self._blink)
        # 说话气泡
        if self._bubble and self._phase < self._bubble_until:
            self._draw_bubble(p, self._bubble)

    def _draw_bubble(self, p, text):
        p.setFont(QFont("", 10))
        fm = p.fontMetrics()
        w = min(self.SIZE - 10, fm.horizontalAdvance(text) + 20)
        rect = QRectF((self.SIZE - w) / 2, 4, w, 26)
        p.setBrush(QColor(255, 255, 255, 240))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(rect, 10, 10)
        p.setPen(QColor(40, 40, 50))
        p.drawText(rect, Qt.AlignCenter, fm.elidedText(text, Qt.ElideRight, int(w - 12)))

    # 拖动 + 点击
    def mousePressEvent(self, e):
        self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        self._press_pos = e.globalPosition().toPoint()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        moved = (e.globalPosition().toPoint() - self._press_pos).manhattanLength()
        if moved < 4:
            self.clicked.emit()
        else:
            self._cfg_ui["pet_pos"] = [self.x(), self.y()]
        self._drag = None
