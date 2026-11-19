"""Seamless borderless hover pop-up card widget and shared Win11 UI helpers."""

from __future__ import annotations

import ctypes
import sys
from typing import Optional

from PyQt6.QtCore import (
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

# Seamless translucent frosted glass palette (zero borders)
SURFACE_COLOR = QColor(20, 23, 30, 195)
BAR_TRANSPARENT_SURFACE = QColor(22, 25, 32, 1)  # Invisible yet hit-testable on Win11
BAR_UNLOCKED_SURFACE = QColor(22, 25, 32, 110)
UNLOCKED_BORDER_COLOR = QColor("#f4b860")
TEXT_PRIMARY = "#ffffff"
TEXT_MUTED = "#9aa4b2"
ACCENT_AMBER = QColor("#f4b860")
ACCENT_CYAN = "#9be3ff"
ACCENT_CYAN_HOVER = "#b6ecff"
ACCENT_CYAN_PRESSED = "#80d6fa"
DARK_ICON_COLOR = QColor("#11141a")
LIGHT_ICON_COLOR = QColor("#eef2f8")
LIGHT_ICON_MUTED = QColor("#cbd3df")

# Windows 11 DWM attributes to round corners and completely strip the grey DWM border
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2
DWMWA_BORDER_COLOR = 34
DWMWA_COLOR_NONE = 0xFFFFFFFE


def apply_win11_rounded_corners(widget: QWidget) -> None:
    """Apply native Windows 11 rounded corners and remove the 1px DWM border."""
    if sys.platform != "win32":
        return
    try:
        hwnd = ctypes.c_void_p(int(widget.winId()))
        preference = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            ctypes.c_int(DWMWA_WINDOW_CORNER_PREFERENCE),
            ctypes.byref(preference),
            ctypes.sizeof(preference),
        )
        no_border = ctypes.c_uint(DWMWA_COLOR_NONE)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            ctypes.c_int(DWMWA_BORDER_COLOR),
            ctypes.byref(no_border),
            ctypes.sizeof(no_border),
        )
    except Exception:
        pass


def render_album_art_pixmap(
    art_bytes: bytes,
    size: int,
    radius: int,
    dpr: float = 1.0,
) -> QPixmap:
    """Render rounded album art or a crisp vector music-note fallback tile."""
    scale = max(1.0, float(dpr))
    pixel_size = int(round(size * scale))

    output = QPixmap(pixel_size, pixel_size)
    output.setDevicePixelRatio(scale)
    output.fill(Qt.GlobalColor.transparent)

    painter = QPainter(output)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    rect = QRectF(0.0, 0.0, float(size), float(size))
    clip_path = QPainterPath()
    clip_path.addRoundedRect(rect, float(radius), float(radius))
    painter.setClipPath(clip_path)

    loaded_image: Optional[QImage] = None
    if art_bytes:
        img = QImage()
        if img.loadFromData(art_bytes) and not img.isNull():
            loaded_image = img

    if loaded_image is not None:
        w = loaded_image.width()
        h = loaded_image.height()
        side = min(w, h)
        x_off = (w - side) // 2
        y_off = (h - side) // 2
        square = loaded_image.copy(x_off, y_off, side, side).scaled(
            pixel_size,
            pixel_size,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter.drawImage(rect, square)
    else:
        _paint_music_fallback(painter, rect)

    painter.end()
    return output


def _paint_music_fallback(painter: QPainter, rect: QRectF) -> None:
    """Paint a minimal dark slate tile with a centered vector beamed note."""
    grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
    grad.setColorAt(0.0, QColor(37, 43, 56, 210))
    grad.setColorAt(1.0, QColor(24, 28, 36, 210))
    painter.fillRect(rect, QBrush(grad))

    w = rect.width()
    h = rect.height()

    painter.setBrush(QBrush(ACCENT_AMBER))
    painter.setPen(Qt.PenStyle.NoPen)

    stem_w = max(1.6, w * 0.062)
    left_x = w * 0.36
    right_x = w * 0.66
    top_y_left = h * 0.30
    top_y_right = h * 0.24
    bottom_y_left = h * 0.68
    bottom_y_right = h * 0.62

    head_rx = w * 0.095
    head_ry = h * 0.072
    painter.drawEllipse(
        QPointF(left_x - head_rx * 0.45, bottom_y_left), head_rx, head_ry
    )
    painter.drawEllipse(
        QPointF(right_x - head_rx * 0.45, bottom_y_right), head_rx, head_ry
    )

    painter.drawRoundedRect(
        QRectF(left_x, top_y_left, stem_w, bottom_y_left - top_y_left),
        stem_w * 0.5,
        stem_w * 0.5,
    )
    painter.drawRoundedRect(
        QRectF(right_x, top_y_right, stem_w, bottom_y_right - top_y_right),
        stem_w * 0.5,
        stem_w * 0.5,
    )

    beam_h = max(2.4, h * 0.09)
    beam = QPolygonF(
        [
            QPointF(left_x, top_y_left),
            QPointF(right_x + stem_w, top_y_right),
            QPointF(right_x + stem_w, top_y_right + beam_h),
            QPointF(left_x, top_y_left + beam_h),
        ]
    )
    painter.drawPolygon(beam)


class TransportIconButton(QPushButton):
    """Crisp anti-aliased QPainter media button (prev / play / pause / next)."""

    def __init__(
        self,
        kind: str,
        size: QSize,
        icon_color: QColor = LIGHT_ICON_COLOR,
        hover_bg: QColor = QColor(255, 255, 255, 24),
        pressed_bg: QColor = QColor(255, 255, 255, 40),
        base_bg: QColor = QColor(0, 0, 0, 0),
        pill: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._kind = kind
        self._icon_color = QColor(icon_color)
        self._hover_bg = QColor(hover_bg)
        self._pressed_bg = QColor(pressed_bg)
        self._base_bg = QColor(base_bg)
        self._pill = pill
        self._hovered = False

        self.setFixedSize(size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFlat(True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def set_kind(self, kind: str) -> None:
        if self._kind != kind:
            self._kind = kind
            self.update()

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(self.rect())
        radius = rect.height() / 2.0 if self._pill else 6.0

        if self.isDown():
            bg = self._pressed_bg
        elif self._hovered:
            bg = self._hover_bg
        else:
            bg = self._base_bg

        if bg.alpha() > 0:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(bg))
            painter.drawRoundedRect(rect, radius, radius)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(self._icon_color))

        cx = rect.center().x()
        cy = rect.center().y()
        s = min(rect.width(), rect.height()) * 0.44

        if self._kind == "play":
            tri = QPolygonF(
                [
                    QPointF(cx - s * 0.36, cy - s * 0.54),
                    QPointF(cx + s * 0.56, cy),
                    QPointF(cx - s * 0.36, cy + s * 0.54),
                ]
            )
            painter.drawPolygon(tri)
        elif self._kind == "pause":
            bar_w = max(2.2, s * 0.28)
            bar_h = s * 1.04
            gap = max(2.2, s * 0.26)
            painter.drawRoundedRect(
                QRectF(cx - gap * 0.5 - bar_w, cy - bar_h * 0.5, bar_w, bar_h),
                1.0,
                1.0,
            )
            painter.drawRoundedRect(
                QRectF(cx + gap * 0.5, cy - bar_h * 0.5, bar_w, bar_h),
                1.0,
                1.0,
            )
        elif self._kind == "previous":
            bar_w = max(1.8, s * 0.19)
            bar_h = s * 0.92
            left_x = cx - s * 0.52
            painter.drawRoundedRect(
                QRectF(left_x, cy - bar_h * 0.5, bar_w, bar_h), 0.9, 0.9
            )
            tri = QPolygonF(
                [
                    QPointF(cx + s * 0.52, cy - bar_h * 0.5),
                    QPointF(left_x + bar_w + 1.0, cy),
                    QPointF(cx + s * 0.52, cy + bar_h * 0.5),
                ]
            )
            painter.drawPolygon(tri)
        elif self._kind == "next":
            bar_w = max(1.8, s * 0.19)
            bar_h = s * 0.92
            right_x = cx + s * 0.52 - bar_w
            painter.drawRoundedRect(
                QRectF(right_x, cy - bar_h * 0.5, bar_w, bar_h), 0.9, 0.9
            )
            tri = QPolygonF(
                [
                    QPointF(cx - s * 0.52, cy - bar_h * 0.5),
                    QPointF(right_x - 1.0, cy),
                    QPointF(cx - s * 0.52, cy + bar_h * 0.5),
                ]
            )
            painter.drawPolygon(tri)

        painter.end()


class ModeToggleButton(QPushButton):
    """Crisp QPainter Shuffle / Repeat toggle button with active amber & disabled dim states."""

    def __init__(
        self,
        mode_type: str,
        size: QSize,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._mode_type = mode_type
        self._active = False
        self._repeat_mode = "None"
        self._hovered = False

        self.setFixedSize(size)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFlat(True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.set_state(active=False, enabled=False, repeat_mode="None")

    def set_state(
        self,
        *,
        active: bool,
        enabled: bool,
        repeat_mode: str = "None",
    ) -> None:
        self._active = bool(active)
        self._repeat_mode = repeat_mode
        self.setEnabled(bool(enabled))

        if enabled:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            if self._mode_type == "shuffle":
                self.setToolTip("Shuffle: On" if self._active else "Shuffle: Off")
            else:
                tip_map = {
                    "None": "Repeat: Off",
                    "List": "Repeat: All",
                    "Track": "Repeat: Track (1)",
                }
                self.setToolTip(tip_map.get(self._repeat_mode, "Repeat: Off"))
        else:
            self._hovered = False
            self.unsetCursor()
            label = "Shuffle" if self._mode_type == "shuffle" else "Repeat"
            self.setToolTip(f"{label} (not supported by this app)")

        self.update()

    def enterEvent(self, event) -> None:
        if self.isEnabled():
            self._hovered = True
            self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(self.rect())
        enabled = self.isEnabled()

        if not enabled:
            painter.setOpacity(0.35)
        elif self.isDown():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(255, 255, 255, 40)))
            painter.drawRoundedRect(rect, 6.0, 6.0)
        elif self._hovered:
            hover_tint = (
                QColor(244, 184, 96, 28)
                if self._active
                else QColor(255, 255, 255, 22)
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(hover_tint))
            painter.drawRoundedRect(rect, 6.0, 6.0)

        icon_color = ACCENT_AMBER if (enabled and self._active) else LIGHT_ICON_MUTED

        if self._mode_type == "shuffle":
            self._paint_shuffle(painter, rect, icon_color)
        else:
            self._paint_repeat(painter, rect, icon_color)

        if enabled and self._active:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(ACCENT_AMBER))
            painter.drawEllipse(
                QPointF(rect.center().x(), rect.bottom() - 2.6), 1.35, 1.35
            )

        painter.end()

    @staticmethod
    def _paint_shuffle(painter: QPainter, rect: QRectF, color: QColor) -> None:
        cx = rect.center().x()
        cy = rect.center().y() - 0.6

        left = cx - 6.0
        right = cx + 5.2
        top = cy - 3.6
        bottom = cy + 3.6

        pen = QPen(color, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        path1 = QPainterPath()
        path1.moveTo(left, top)
        path1.cubicTo(cx - 1.2, top, cx + 0.2, bottom, right, bottom)
        painter.drawPath(path1)

        path2 = QPainterPath()
        path2.moveTo(left, bottom)
        path2.cubicTo(cx - 1.2, bottom, cx + 0.2, top, right, top)
        painter.drawPath(path2)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(color))
        for tip_y in (top, bottom):
            arrow = QPolygonF(
                [
                    QPointF(right + 2.2, tip_y),
                    QPointF(right - 1.0, tip_y - 2.3),
                    QPointF(right - 1.0, tip_y + 2.3),
                ]
            )
            painter.drawPolygon(arrow)

    def _paint_repeat(self, painter: QPainter, rect: QRectF, color: QColor) -> None:
        cx = rect.center().x()
        cy = rect.center().y() - 0.6

        left = cx - 6.2
        right = cx + 6.2
        top = cy - 3.8
        bottom = cy + 3.8
        r = 2.6

        pen = QPen(color, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        top_path = QPainterPath()
        top_path.moveTo(left, cy + 0.4)
        top_path.lineTo(left, top + r)
        top_path.quadTo(left, top, left + r, top)
        top_path.lineTo(right - 2.2, top)
        painter.drawPath(top_path)

        bottom_path = QPainterPath()
        bottom_path.moveTo(right, cy - 0.4)
        bottom_path.lineTo(right, bottom - r)
        bottom_path.quadTo(right, bottom, right - r, bottom)
        bottom_path.lineTo(left + 2.2, bottom)
        painter.drawPath(bottom_path)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(color))

        top_arrow = QPolygonF(
            [
                QPointF(right + 0.4, top),
                QPointF(right - 2.9, top - 2.3),
                QPointF(right - 2.9, top + 2.3),
            ]
        )
        painter.drawPolygon(top_arrow)

        bottom_arrow = QPolygonF(
            [
                QPointF(left - 0.4, bottom),
                QPointF(left + 2.9, bottom - 2.3),
                QPointF(left + 2.9, bottom + 2.3),
            ]
        )
        painter.drawPolygon(bottom_arrow)

        if self._repeat_mode == "Track":
            font = QFont("Segoe UI")
            font.setPixelSize(8)
            font.setWeight(QFont.Weight.Bold)
            painter.setFont(font)
            painter.setPen(QPen(color))
            painter.drawText(
                QRectF(left, top, right - left, bottom - top + 0.4),
                Qt.AlignmentFlag.AlignCenter,
                "1",
            )


class PopupCard(QWidget):
    """Borderless floating hover pop-up card displayed directly above the taskbar bar."""

    WIDTH = 290
    HEIGHT = 115
    ART_SIZE = 76

    hovered_changed = pyqtSignal(bool)
    shuffle_clicked = pyqtSignal()
    previous_clicked = pyqtSignal()
    play_pause_clicked = pyqtSignal()
    next_clicked = pyqtSignal()
    repeat_clicked = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._is_hovered = False
        self._title_full = ""
        self._artist_full = ""
        self._art_bytes = b""
        self._is_playing = False
        self._source_app_name = ""

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setWindowOpacity(0.0)

        self._fade_anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_anim.finished.connect(self._on_fade_finished)
        self._hiding = False

        self._build_ui()
        apply_win11_rounded_corners(self)

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        self._art_label = QLabel(self)
        self._art_label.setFixedSize(self.ART_SIZE, self.ART_SIZE)
        self._art_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._art_label, 0, Qt.AlignmentFlag.AlignVCenter)

        right_col = QVBoxLayout()
        right_col.setContentsMargins(0, 1, 0, 1)
        right_col.setSpacing(2)

        badge_row = QHBoxLayout()
        badge_row.setContentsMargins(0, 0, 0, 2)
        badge_row.setSpacing(0)

        self._source_badge = QLabel("Media", self)
        self._source_badge.setStyleSheet(
            "QLabel {"
            "  background-color: rgba(255, 255, 255, 18);"
            "  color: #cbd5e1;"
            "  border: none;"
            "  border-radius: 7px;"
            "  padding: 1px 7px;"
            "  font-family: 'Segoe UI';"
            "  font-size: 9px;"
            "  font-weight: 600;"
            "}"
        )
        self._source_badge.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed
        )
        badge_row.addWidget(self._source_badge, 0, Qt.AlignmentFlag.AlignLeft)
        badge_row.addStretch(1)
        right_col.addLayout(badge_row)

        self._title_label = QLabel(self)
        title_font = QFont("Segoe UI", 10)
        title_font.setPixelSize(13)
        title_font.setWeight(QFont.Weight.Bold)
        self._title_label.setFont(title_font)
        self._title_label.setStyleSheet(f"color: {TEXT_PRIMARY}; background: transparent;")
        self._title_label.setFixedWidth(176)
        right_col.addWidget(self._title_label)

        self._artist_label = QLabel(self)
        artist_font = QFont("Segoe UI", 9)
        artist_font.setPixelSize(11)
        self._artist_label.setFont(artist_font)
        self._artist_label.setStyleSheet(f"color: {TEXT_MUTED}; background: transparent;")
        self._artist_label.setFixedWidth(176)
        right_col.addWidget(self._artist_label)

        right_col.addStretch(1)

        controls_row = QHBoxLayout()
        controls_row.setContentsMargins(0, 4, 0, 0)
        controls_row.setSpacing(5)

        self._shuffle_btn = ModeToggleButton(
            mode_type="shuffle",
            size=QSize(26, 26),
            parent=self,
        )
        self._shuffle_btn.clicked.connect(self.shuffle_clicked.emit)

        self._prev_btn = TransportIconButton(
            kind="previous",
            size=QSize(26, 26),
            icon_color=LIGHT_ICON_MUTED,
            hover_bg=QColor(255, 255, 255, 24),
            pressed_bg=QColor(255, 255, 255, 42),
            parent=self,
        )
        self._prev_btn.setToolTip("Previous")
        self._prev_btn.clicked.connect(self.previous_clicked.emit)

        self._play_pause_btn = TransportIconButton(
            kind="play",
            size=QSize(50, 26),
            icon_color=DARK_ICON_COLOR,
            base_bg=QColor(ACCENT_CYAN),
            hover_bg=QColor(ACCENT_CYAN_HOVER),
            pressed_bg=QColor(ACCENT_CYAN_PRESSED),
            pill=True,
            parent=self,
        )
        self._play_pause_btn.setToolTip("Play / Pause")
        self._play_pause_btn.clicked.connect(self.play_pause_clicked.emit)

        self._next_btn = TransportIconButton(
            kind="next",
            size=QSize(26, 26),
            icon_color=LIGHT_ICON_MUTED,
            hover_bg=QColor(255, 255, 255, 24),
            pressed_bg=QColor(255, 255, 255, 42),
            parent=self,
        )
        self._next_btn.setToolTip("Next")
        self._next_btn.clicked.connect(self.next_clicked.emit)

        self._repeat_btn = ModeToggleButton(
            mode_type="repeat",
            size=QSize(26, 26),
            parent=self,
        )
        self._repeat_btn.clicked.connect(self.repeat_clicked.emit)

        controls_row.addWidget(self._shuffle_btn)
        controls_row.addWidget(self._prev_btn)
        controls_row.addWidget(self._play_pause_btn)
        controls_row.addWidget(self._next_btn)
        controls_row.addWidget(self._repeat_btn)
        controls_row.addStretch(1)
        right_col.addLayout(controls_row)

        root.addLayout(right_col, 1)
        self._refresh_art_pixmap()

    @property
    def is_hovered(self) -> bool:
        return self._is_hovered

    def update_track(self, title: str, artist: str) -> None:
        self._title_full = title or "No Media"
        self._artist_full = artist or ""

        title_metrics = QFontMetrics(self._title_label.font())
        elided_title = title_metrics.elidedText(
            self._title_full,
            Qt.TextElideMode.ElideRight,
            self._title_label.width(),
        )
        self._title_label.setText(elided_title)
        self._title_label.setToolTip(self._title_full)

        artist_metrics = QFontMetrics(self._artist_label.font())
        elided_artist = artist_metrics.elidedText(
            self._artist_full,
            Qt.TextElideMode.ElideRight,
            self._artist_label.width(),
        )
        self._artist_label.setText(elided_artist)
        self._artist_label.setToolTip(self._artist_full)

    def update_album_art(self, art_bytes: bytes) -> None:
        self._art_bytes = art_bytes or b""
        self._refresh_art_pixmap()

    def update_source_app(self, display_name: str) -> None:
        self._source_app_name = (display_name or "").strip()
        if self._source_app_name:
            self._source_badge.setText(self._source_app_name)
            self._source_badge.setVisible(True)
        else:
            self._source_badge.setVisible(False)

    def update_playback_status(self, is_playing: bool) -> None:
        self._is_playing = bool(is_playing)
        self._play_pause_btn.set_kind("pause" if self._is_playing else "play")

    def update_shuffle_state(self, is_active: bool, enabled: bool) -> None:
        self._shuffle_btn.set_state(active=is_active, enabled=enabled)

    def update_repeat_state(self, mode: str, enabled: bool) -> None:
        normalized = mode if mode in ("None", "List", "Track") else "None"
        self._repeat_btn.set_state(
            active=(normalized != "None"),
            enabled=enabled,
            repeat_mode=normalized,
        )

    def show_smooth(self, x: int, y: int) -> None:
        self.move(x, y)
        self._hiding = False
        self._fade_anim.stop()

        if not self.isVisible():
            self.setWindowOpacity(0.0)
            self.show()
            apply_win11_rounded_corners(self)

        self.raise_()
        self._fade_anim.setDuration(150)
        self._fade_anim.setStartValue(self.windowOpacity())
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.start()

    def hide_smooth(self) -> None:
        if not self.isVisible():
            return
        self._hiding = True
        self._fade_anim.stop()
        self._fade_anim.setDuration(120)
        self._fade_anim.setStartValue(self.windowOpacity())
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.start()

    def hide_immediately(self) -> None:
        self._fade_anim.stop()
        self._hiding = False
        self._is_hovered = False
        self.setWindowOpacity(0.0)
        self.hide()

    def _on_fade_finished(self) -> None:
        if self._hiding:
            self._hiding = False
            self._is_hovered = False
            self.hide()

    def _refresh_art_pixmap(self) -> None:
        dpr = self.devicePixelRatioF() if hasattr(self, "devicePixelRatioF") else 1.0
        pixmap = render_album_art_pixmap(
            self._art_bytes, self.ART_SIZE, radius=10, dpr=dpr
        )
        self._art_label.setPixmap(pixmap)

    def enterEvent(self, event) -> None:
        self._is_hovered = True
        self.hovered_changed.emit(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._is_hovered = False
        self.hovered_changed.emit(False)
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(self.rect())
        painter.setBrush(QBrush(SURFACE_COLOR))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, 10.0, 10.0)
        painter.end()