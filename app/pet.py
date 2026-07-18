"""听晓桌宠：戴耳机的声波猫。

纯 QPainter 分层绘制，不依赖外部素材。公开接口兼容旧版：
``PetWidget`` / ``SKINS`` / ``clicked`` / ``set_state`` / ``say`` /
``reset_position`` / ``skin``。
"""
from __future__ import annotations

import math
import random
import time

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygon,
    QRadialGradient,
    QRegion,
)
from PySide6.QtWidgets import QApplication, QMenu, QWidget


# 保留旧皮肤名，已有 config 无需迁移。每套皮肤都包含完整材质层。
SKINS = {
    "奶黄": {
        "body": QColor("#F6C968"), "body_hi": QColor("#FFF0BC"),
        "muzzle": QColor("#FFF3D5"), "stripe": QColor("#E99A38"),
        "ear": QColor("#F27F67"), "cheek": QColor("#F48C82"),
        "phones": QColor("#20283A"), "rim": QColor("#111827"),
    },
    "薄荷": {
        "body": QColor("#8FD9C2"), "body_hi": QColor("#DDF7EC"),
        "muzzle": QColor("#E9FFF8"), "stripe": QColor("#3FA78B"),
        "ear": QColor("#F0998D"), "cheek": QColor("#F58F86"),
        "phones": QColor("#25313B"), "rim": QColor("#111827"),
    },
    "樱粉": {
        "body": QColor("#F4B4C7"), "body_hi": QColor("#FFE7EF"),
        "muzzle": QColor("#FFF2F6"), "stripe": QColor("#DB7896"),
        "ear": QColor("#EB7E91"), "cheek": QColor("#EF7182"),
        "phones": QColor("#302738"), "rim": QColor("#171421"),
    },
    "天蓝": {
        "body": QColor("#8BC4F4"), "body_hi": QColor("#E0F1FF"),
        "muzzle": QColor("#EDF8FF"), "stripe": QColor("#4B8ED3"),
        "ear": QColor("#F09188"), "cheek": QColor("#EE817B"),
        "phones": QColor("#202C43"), "rim": QColor("#101727"),
    },
    "曜石": {
        "body": QColor("#465064"), "body_hi": QColor("#9CA8BA"),
        "muzzle": QColor("#D9DEE7"), "stripe": QColor("#20283A"),
        "ear": QColor("#DD7D72"), "cheek": QColor("#E07972"),
        "phones": QColor("#121722"), "rim": QColor("#080B12"),
    },
}

_INK = QColor("#252434")
_ORANGE = QColor("#FF8A3D")
_BLUE = QColor("#249BFF")


def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def _ease(v):
    v = _clamp(v)
    return v * v * (3.0 - 2.0 * v)


def _path_poly(points):
    path = QPainterPath()
    path.moveTo(*points[0])
    for pt in points[1:]:
        path.lineTo(*pt)
    path.closeSubpath()
    return path


def _wave_badge(p, rect, glow=0.0):
    """耳罩上的听晓橙蓝声波标识。"""
    p.save()
    p.setPen(Qt.NoPen)
    if glow:
        halo = QColor(_BLUE)
        halo.setAlpha(int(55 * glow))
        p.setBrush(halo)
        p.drawEllipse(rect.adjusted(-3, -3, 3, 3))
    bars = (0.38, 0.66, 1.0, 0.72, 0.46)
    bw = rect.width() / 10.0
    gap = bw * 0.72
    total = len(bars) * bw + (len(bars) - 1) * gap
    x = rect.center().x() - total / 2
    for i, h in enumerate(bars):
        col = _ORANGE if i < 3 else _BLUE
        p.setBrush(col)
        hh = rect.height() * h
        p.drawRoundedRect(QRectF(x, rect.center().y() - hh / 2, bw, hh), bw / 2, bw / 2)
        x += bw + gap
    p.restore()


def _star(p, x, y, r, color):
    path = QPainterPath()
    for i in range(10):
        a = -math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else r * 0.42
        pt = QPointF(x + math.cos(a) * rr, y + math.sin(a) * rr)
        path.moveTo(pt) if i == 0 else path.lineTo(pt)
    path.closeSubpath()
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawPath(path)


def draw_pet(p, size, skin, state, phase, level=0.0, blink=1.0, look=(0.0, 0.0),
             hover=0.0, dragging=0.0, reaction=0.0, transition=1.0):
    """绘制声波猫；保留旧版参数，并增加可选交互动画参数。"""
    c = SKINS.get(skin, SKINS["奶黄"])
    p.setRenderHint(QPainter.Antialiasing)
    t = phase * 0.04
    state_k = _ease(transition)
    level = _clamp(level)
    hover = _ease(hover)
    dragging = _ease(dragging)
    reaction = _clamp(reaction)

    # 身体整体运动：待机呼吸、完成蹦跳、拖动悬垂、落地回弹。
    breathe = math.sin(t * 2.05) * 1.4
    y = 109.0 + breathe
    rot = math.sin(t * 2.8) * 1.2 * hover
    squash_x, squash_y = 1.0, 1.0
    if state == "happy":
        jump = abs(math.sin(t * 7.0))
        y -= 14.0 * jump * state_k
        squash_x += (1.0 - jump) * 0.055
        squash_y -= (1.0 - jump) * 0.05
    elif state == "listening":
        y -= (3.0 + 3.0 * level) * state_k
        squash_y += 0.025 * level
    elif state == "sleep":
        y += 8.0 * state_k
        squash_x += 0.08 * state_k
        squash_y -= 0.08 * state_k
    elif state == "processing":
        rot += math.sin(t * 4.2) * 3.0 * state_k
    if dragging:
        y -= 8.0 * dragging
        squash_x -= 0.06 * dragging
        squash_y += 0.10 * dragging
        rot += math.sin(t * 12.0) * 5.0 * dragging
    if reaction:
        pulse = math.sin(reaction * math.pi)
        squash_x += pulse * 0.08
        squash_y -= pulse * 0.06

    cx = size / 2.0
    scale = size / 180.0
    p.save()
    p.translate(cx, y * scale)
    p.rotate(rot)
    p.scale(scale * squash_x, scale * squash_y)
    p.translate(-90, -109)

    # 地面影子。
    p.setPen(Qt.NoPen)
    shadow = QColor(22, 28, 43, 38 if not dragging else 22)
    p.setBrush(shadow)
    p.drawEllipse(QRectF(43, 159 + dragging * 5, 94, 10 - dragging * 2))

    # 尾巴：粗壮蓬松、橙色尾尖，单独摆动，拖动时甩得更快。
    tail_a = math.sin(t * (3.0 + dragging * 7.0)) * (10 + 14 * hover + 16 * dragging)
    if state == "happy":
        tail_a = math.sin(t * 13.0) * 28
    p.save()
    p.translate(127, 126)
    p.rotate(tail_a)
    tail = QPainterPath()
    tail.moveTo(0, 2)
    tail.cubicTo(36, -8, 42, 26, 18, 35)
    tail.cubicTo(4, 40, -1, 28, 11, 23)
    tail.cubicTo(25, 17, 21, 8, 0, 11)
    tail.closeSubpath()
    tg = QLinearGradient(0, 0, 30, 34)
    tg.setColorAt(0.0, c["body_hi"])
    tg.setColorAt(0.45, c["body"])
    tg.setColorAt(1.0, c["ear"])       # 尾尖偏橙
    p.setBrush(tg)
    p.setPen(Qt.NoPen)
    p.drawPath(tail)
    p.restore()

    # 后腿与身体，明确头身层次。
    p.setBrush(c["body"].darker(105))
    p.drawEllipse(QRectF(48, 118, 35, 38))
    p.drawEllipse(QRectF(98, 118, 35, 38))
    body_g = QRadialGradient(78, 112, 66)
    body_g.setColorAt(0, c["body_hi"])
    body_g.setColorAt(0.62, c["body"])
    body_g.setColorAt(1, c["body"].darker(108))
    p.setBrush(body_g)
    p.drawRoundedRect(QRectF(49, 92, 82, 61), 32, 32)

    # 小爪子，拖动时自然下垂。
    paw_y = 137 + dragging * 8
    for px in (63, 103):
        p.setBrush(c["body_hi"])
        p.drawRoundedRect(QRectF(px, paw_y, 20, 19), 9, 9)
        p.setPen(QPen(c["stripe"], 1.2))
        p.drawLine(QPointF(px + 7, paw_y + 13), QPointF(px + 7, paw_y + 17))
        p.drawLine(QPointF(px + 13, paw_y + 13), QPointF(px + 13, paw_y + 17))
        p.setPen(Qt.NoPen)

    # 猫耳（先于头部），listening 时竖起；拖动时稍向外折。
    ear_lift = (7.0 * state_k if state == "listening" else 0.0) + level * 3.0
    ear_flap = math.sin(t * 6.0) * (1.2 + level * 2.5)
    if state == "idle":
        gt = t % 6.5                       # 每约6.5秒抖一下耳朵,待机不呆板
        if gt < 0.55:
            ear_flap += math.sin(gt / 0.55 * math.pi) * 5.5
    for sgn in (-1, 1):
        p.save()
        p.translate(90 + sgn * 35, 72)
        p.rotate(sgn * (ear_flap - dragging * 8))
        outer = _path_poly([(0, 16), (sgn * 3, -26 - ear_lift), (sgn * 23, 7)])
        p.setBrush(c["body"])
        p.drawPath(outer)
        inner = _path_poly([(sgn * 4, 8), (sgn * 5, -17 - ear_lift), (sgn * 17, 4)])
        p.setBrush(c["ear"])
        p.drawPath(inner)
        p.restore()

    # 头部。
    head_g = QRadialGradient(70, 62, 72)
    head_g.setColorAt(0, c["body_hi"])
    head_g.setColorAt(0.58, c["body"])
    head_g.setColorAt(1, c["body"].darker(108))
    p.setBrush(head_g)
    p.drawRoundedRect(QRectF(37, 48, 106, 91), 47, 43)

    # 额头虎斑，形成稳定识别特征。
    p.setBrush(c["stripe"])
    for x, angle in ((76, -12), (89, 0), (102, 12)):
        p.save()
        p.translate(x, 53)
        p.rotate(angle)
        p.drawRoundedRect(QRectF(-3, 0, 6, 15), 3, 3)
        p.restore()

    # 耳机头梁：加粗有厚度，顶部一抹高光=软垫质感。
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(c["rim"], 11, Qt.SolidLine, Qt.RoundCap))
    p.drawArc(QRectF(52, 33, 76, 64), 16 * 16, 148 * 16)
    p.setPen(QPen(c["phones"].lighter(130), 4, Qt.SolidLine, Qt.RoundCap))
    p.drawArc(QRectF(53, 34, 74, 60), 30 * 16, 120 * 16)
    p.setPen(QPen(QColor(255, 255, 255, 70), 2, Qt.SolidLine, Qt.RoundCap))
    p.drawArc(QRectF(55, 35, 70, 56), 45 * 16, 90 * 16)
    p.setPen(Qt.NoPen)
    # 耳罩：圆润外壳 + 径向渐变内芯 + 声波徽标 + 左上高光。
    phone_y = 84 - ear_lift * 0.2
    phone_glow = level if state == "listening" else 0.25 + hover * 0.25
    for sgn in (-1, 1):
        r = QRectF(24 if sgn < 0 else 125, phone_y - 23, 33, 47)
        p.setBrush(c["rim"])
        p.drawRoundedRect(r, 15, 16)
        inner = r.adjusted(4, 4, -4, -4)
        cup_g = QRadialGradient(inner.center().x() - 3, inner.center().y() - 6,
                                inner.height() * 0.9)
        cup_g.setColorAt(0.0, c["phones"].lighter(145))
        cup_g.setColorAt(1.0, c["phones"])
        p.setBrush(cup_g)
        p.drawRoundedRect(inner, 11, 12)
        _wave_badge(p, inner.adjusted(4, 8, -4, -8), phone_glow)
        p.setPen(QPen(QColor(255, 255, 255, 95), 2.4, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        p.drawArc(r.adjusted(5, 5, -9, -20), 55 * 16, 95 * 16)
        p.setPen(Qt.NoPen)

    # 口鼻区域。
    p.setBrush(c["muzzle"])
    p.drawEllipse(QRectF(65, 91, 31, 28))
    p.drawEllipse(QRectF(84, 91, 31, 28))

    # 眼睛跟随光标；睡眠眯眼，happy 弯眼。
    lx = _clamp(look[0], -1, 1) * 2.8
    ly = _clamp(look[1], -1, 1) * 2.0
    eye_y = 83 + ly
    eye_h = 15 * blink
    p.setPen(QPen(_INK, 2.2, Qt.SolidLine, Qt.RoundCap))
    for ex in (70, 110):
        if state == "sleep":
            p.drawArc(QRectF(ex - 7, eye_y - 1, 14, 8), 195 * 16, 150 * 16)
        elif state == "happy":
            p.drawArc(QRectF(ex - 7, eye_y - 2, 14, 10), 20 * 16, 140 * 16)
        elif blink < 0.18:
            p.drawLine(QPointF(ex - 6, eye_y), QPointF(ex + 6, eye_y))
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(_INK)
            p.drawEllipse(QRectF(ex - 6 + lx, eye_y - eye_h / 2, 12, max(1.5, eye_h)))
            p.setBrush(QColor(255, 255, 255, 240))
            p.drawEllipse(QRectF(ex - 2 + lx, eye_y - eye_h * 0.30, 4.2, 4.2))
            p.setBrush(QColor(255, 255, 255, 165))
            p.drawEllipse(QRectF(ex + 2.2 + lx, eye_y + eye_h * 0.12, 2.2, 2.2))
            p.setPen(QPen(_INK, 2.2, Qt.SolidLine, Qt.RoundCap))

    # 腮红、鼻子和按状态变化的嘴。
    p.setPen(Qt.NoPen)
    cheek = QColor(c["cheek"])
    cheek.setAlpha(135)
    p.setBrush(cheek)
    p.drawEllipse(QRectF(49, 99, 16, 8))
    p.drawEllipse(QRectF(115, 99, 16, 8))
    p.setBrush(c["stripe"].darker(118))
    nose = QPainterPath()
    nose.moveTo(86, 99); nose.lineTo(94, 99); nose.lineTo(90, 104); nose.closeSubpath()
    p.drawPath(nose)
    p.setPen(QPen(_INK, 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    if state == "listening":
        mh = 5 + level * 10
        p.setPen(Qt.NoPen)
        p.setBrush(c["ear"].darker(120))
        p.drawEllipse(QRectF(85, 106, 10, mh))
        p.setBrush(QColor("#FFB0A3"))
        p.drawEllipse(QRectF(87, 108 + mh * 0.35, 6, max(2, mh * 0.30)))
    elif state == "happy":
        mouth = QPainterPath()
        mouth.moveTo(82, 106); mouth.quadTo(90, 118, 98, 106)
        p.drawPath(mouth)
    else:
        p.drawArc(QRectF(81, 102, 9, 10), 205 * 16, 130 * 16)
        p.drawArc(QRectF(90, 102, 9, 10), 205 * 16, 130 * 16)

    # 胡须随动作轻摆。
    p.setPen(QPen(_INK, 1.15, Qt.SolidLine, Qt.RoundCap))
    whisk = math.sin(t * 4.0) * 1.5
    for sgn in (-1, 1):
        x0 = 55 if sgn < 0 else 125
        p.drawLine(QPointF(x0, 106), QPointF(x0 + sgn * 17, 102 + whisk))
        p.drawLine(QPointF(x0, 112), QPointF(x0 + sgn * 18, 114 - whisk))
    p.restore()

    # 状态装饰留在整体变换之外，保证读图清楚。
    ox, oy = cx, y * scale
    if state == "processing":
        for i, col in enumerate((_ORANGE, QColor("#FFC648"), _BLUE)):
            a = t * 3.4 + i * math.tau / 3
            rr = 18 * scale
            p.setPen(Qt.NoPen); p.setBrush(col)
            p.drawEllipse(QRectF(ox + math.cos(a) * rr - 3, oy - 61 * scale + math.sin(a) * 6 - 3, 6, 6))
    elif state == "happy":
        for i, col in enumerate((_ORANGE, QColor("#FFD052"), _BLUE)):
            a = t * 2.1 + i * 2.1
            _star(p, ox + math.cos(a) * 57 * scale, oy - (48 + (i % 2) * 11) * scale,
                  5.5 * scale, col)
    elif state == "sleep":
        p.setFont(QFont("", max(8, int(size * 0.075)), QFont.DemiBold))
        p.setPen(QColor(72, 128, 198, 190))
        for i in range(3):
            rise = (t * 0.25 + i / 3) % 1.0
            p.drawText(QPointF(ox + (36 + i * 9) * scale, oy - (48 + rise * 28) * scale), "Z")


class PetWidget(QWidget):
    """置顶、透明、不抢焦点的交互式桌宠窗口。"""

    clicked = Signal()
    double_clicked = Signal()
    hide_requested = Signal()
    settings_requested = Signal()
    skin_changed = Signal(str)
    reset_requested = Signal()

    SIZE = 180
    VALID_STATES = {"idle", "listening", "processing", "happy", "sleep"}

    def __init__(self, cfg_ui: dict, get_level=None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        if hasattr(Qt, "WA_MacAlwaysShowToolWindow"):
            self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        self.setWindowFlag(Qt.WindowDoesNotAcceptFocus, True)
        self.setMouseTracking(True)
        self.resize(self.SIZE, self.SIZE)

        self._cfg_ui = cfg_ui if isinstance(cfg_ui, dict) else {}
        self._get_level = get_level or (lambda: 0.0)
        self.skin = self._cfg_ui.get("pet_skin", "奶黄")
        if self.skin not in SKINS:
            self.skin = "奶黄"
        self._state = "idle"
        self._phase = 0.0
        self._state_changed_at = time.monotonic()
        self._idle_secs = 0.0
        self._blink = 1.0
        self._next_blink = random.uniform(2.4, 5.2)
        self._blink_clock = 0.0
        self._look = (0.0, 0.0)
        self._hover = 0.0
        self._hover_target = 0.0
        self._drag = None
        self._dragging = 0.0
        self._drag_target = 0.0
        self._press_pos = None
        self._ignore_release_once = False
        self._bubble = ""
        self._bubble_until = 0.0
        self._reaction_until = 0.0
        self._drop_until = 0.0
        self._single_click = QTimer(self)
        self._single_click.setSingleShot(True)
        # 单击去抖必须 ≥ 系统双击间隔,否则慢速双击会先误触发单击(说话)再触发双击(开记忆库)
        self._single_click.setInterval(max(250, min(600, QApplication.doubleClickInterval() + 40)))
        self._single_click.timeout.connect(self.clicked.emit)

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.CoarseTimer)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._apply_mask()
        self._place()

    def _apply_mask(self):
        """遮罩贴合猫、尾巴、状态装饰；仅气泡显示时开放顶部区域。"""
        s = self.SIZE / 180.0
        region = QRegion(int(31*s), int(44*s), int(118*s), int(116*s), QRegion.Ellipse)
        region = region.united(QRegion(int(20*s), int(56*s), int(140*s), int(72*s)))
        region = region.united(QRegion(int(116*s), int(116*s), int(62*s), int(56*s), QRegion.Ellipse))
        left = QPolygon([QPoint(int(45*s), int(75*s)), QPoint(int(57*s), int(21*s)), QPoint(int(85*s), int(68*s))])
        right = QPolygon([QPoint(int(95*s), int(68*s)), QPoint(int(123*s), int(21*s)), QPoint(int(137*s), int(75*s))])
        region = region.united(QRegion(left)).united(QRegion(right))
        # 头顶盖板:耳机头梁弧顶(所有状态都在头部椭圆之上)+ 完成/睡眠/识别态的
        # 漂浮装饰(星星/Zzz/转圈点),必须无条件并进遮罩,否则头梁会被裁平、装饰被裁掉。
        region = region.united(QRegion(int(24*s), int(26*s), int(132*s), int(48*s)))
        # 地面柔和影子在身体下方,并进遮罩避免硬切边。
        region = region.united(QRegion(int(41*s), int(158*s), int(98*s), int(16*s), QRegion.Ellipse))
        if self._bubble and time.monotonic() < self._bubble_until:
            region = region.united(QRegion(int(4*s), 0, int(172*s), int(41*s)))
        self.setMask(region)

    def reset_position(self):
        self._cfg_ui.pop("pet_pos", None)
        self._place()

    def set_state(self, state):
        state = state if state in self.VALID_STATES else "idle"
        if state != self._state:
            self._state = state
            self._state_changed_at = time.monotonic()
            self._idle_secs = 0.0
            if state != "sleep":
                self._reaction_until = max(self._reaction_until, time.monotonic() + 0.32)
            self._apply_mask()

    def say(self, text, secs=3.0):
        self._bubble = str(text or "").strip()
        self._bubble_until = time.monotonic() + max(0.2, float(secs))
        self._apply_mask()
        self.update()

    def _place(self):
        pos = self._cfg_ui.get("pet_pos")
        if isinstance(pos, list) and len(pos) == 2:
            try:
                x, y = int(pos[0]), int(pos[1])
                for screen in QApplication.screens():
                    if screen.availableGeometry().adjusted(-40, -40, 40, 40).contains(x, y):
                        self.move(x, y)
                        return
            except (TypeError, ValueError):
                pass
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.SIZE - 40, screen.bottom() - self.SIZE - 40)

    def _tick(self):
        now = time.monotonic()
        self._phase += 1.0
        self._hover += (self._hover_target - self._hover) * 0.18
        self._dragging += (self._drag_target - self._dragging) * 0.24

        self._blink_clock += 0.04
        if self._blink_clock >= self._next_blink:
            dt = self._blink_clock - self._next_blink
            self._blink = _clamp(abs(dt - 0.09) / 0.09)
            if dt > 0.18:
                self._blink_clock = 0.0
                self._next_blink = random.uniform(2.4, 5.6)
                self._blink = 1.0
        else:
            self._blink = 1.0

        if self._state == "idle":
            self._idle_secs += 0.04
            if self._idle_secs > 90:
                self.set_state("sleep")
        if self._bubble and now >= self._bubble_until:
            self._bubble = ""
            self._apply_mask()
        self.update()

    def paintEvent(self, _event):
        now = time.monotonic()
        p = QPainter(self)
        try:
            level = _clamp(float(self._get_level()) * 8.0)
        except Exception:
            level = 0.0
        transition = _clamp((now - self._state_changed_at) / 0.28)
        reaction = _clamp((self._reaction_until - now) / 0.36)
        if now < self._drop_until:
            reaction = max(reaction, _clamp((self._drop_until - now) / 0.42))
        draw_pet(p, self.SIZE, self.skin, self._state, self._phase, level,
                 self._blink, self._look, self._hover, self._dragging,
                 reaction, transition)
        if self._bubble and now < self._bubble_until:
            self._draw_bubble(p, self._bubble)

    def _draw_bubble(self, p, text):
        p.setFont(QFont("", 10, QFont.Medium))
        fm = p.fontMetrics()
        w = min(self.SIZE - 10, fm.horizontalAdvance(text) + 24)
        rect = QRectF((self.SIZE - w) / 2, 3, w, 31)
        shadow = rect.translated(0, 2)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(20, 28, 43, 38))
        p.drawRoundedRect(shadow, 13, 13)
        p.setBrush(QColor(255, 255, 255, 247))
        p.drawRoundedRect(rect, 13, 13)
        tail = QPainterPath()
        tail.moveTo(self.SIZE / 2 - 5, 33); tail.lineTo(self.SIZE / 2, 40); tail.lineTo(self.SIZE / 2 + 6, 33)
        tail.closeSubpath(); p.drawPath(tail)
        p.setPen(QColor("#252434"))
        shown = fm.elidedText(text, Qt.ElideRight, int(w - 16))
        p.drawText(rect, Qt.AlignCenter, shown)

    def enterEvent(self, event):
        self._hover_target = 1.0
        if self._state == "sleep":
            self.set_state("idle")
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover_target = 0.0
        self._look = (0.0, 0.0)
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.position()
        self._look = (_clamp((pos.x() - self.SIZE / 2) / (self.SIZE / 2), -1, 1),
                      _clamp((pos.y() - self.SIZE / 2) / (self.SIZE / 2), -1, 1))
        if self._drag is not None:
            self._drag_target = 1.0
            self.move(event.globalPosition().toPoint() - self._drag)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._ignore_release_once = False   # 清掉上一次双击可能残留的标志,别吞掉这次点击/拖动
            self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._press_pos = event.globalPosition().toPoint()
            self._reaction_until = time.monotonic() + 0.32
            event.accept()
        else:
            event.ignore()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or self._press_pos is None:
            return
        if self._ignore_release_once:
            self._ignore_release_once = False
            self._drag = None
            self._press_pos = None
            self._drag_target = 0.0
            event.accept()
            return
        moved = (event.globalPosition().toPoint() - self._press_pos).manhattanLength()
        if moved < 5:
            self._single_click.start()
        else:
            self._cfg_ui["pet_pos"] = [self.x(), self.y()]
            self._drop_until = time.monotonic() + 0.42
        self._drag = None
        self._press_pos = None
        self._drag_target = 0.0
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._single_click.stop()
            self._ignore_release_once = True
            self._reaction_until = time.monotonic() + 0.55
            self.double_clicked.emit()
            event.accept()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        skins = menu.addMenu("换装")
        for name in SKINS:
            act = QAction(name, skins)
            act.setCheckable(True)
            act.setChecked(name == self.skin)
            act.triggered.connect(lambda _checked=False, n=name: self._choose_skin(n))
            skins.addAction(act)
        menu.addSeparator()
        settings = menu.addAction("打开听晓设置…")
        reset = menu.addAction("重置位置")
        hide = menu.addAction("隐藏桌宠")
        chosen = menu.exec(event.globalPos())
        if chosen is settings:
            self.settings_requested.emit()
        elif chosen is reset:
            self.reset_requested.emit()
        elif chosen is hide:
            self.hide_requested.emit()
        menu.deleteLater()   # QMenu(self) 挂在 widget 上不会自动释放,反复右键会累积泄漏

    def _choose_skin(self, name):
        if name not in SKINS:
            return
        self.skin = name
        self._cfg_ui["pet_skin"] = name
        self.skin_changed.emit(name)
        self._reaction_until = time.monotonic() + 0.5
        self.update()
