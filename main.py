"""Seamless borderless Windows 11 taskbar media controller with progress bar."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QPainter,
    QPen,
)
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from audio_viz import AudioVisualizerWorker, IDLE_LEVEL, NUM_BANDS
from media_backend import MediaBackend, MediaState
from popup_card import (
    ACCENT_AMBER,
    BAR_UNLOCKED_SURFACE,
    LIGHT_ICON_COLOR,
    PopupCard,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TransportIconButton,
    UNLOCKED_BORDER_COLOR,
    render_album_art_pixmap,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

BAR_HEIGHT = 44
BAR_BOTTOM_OFFSET = 46
POPUP_VERTICAL_OFFSET = 123
COLLAPSED_WIDTH = 44
TEXT_MAX_WIDTH = 140
ART_SIZE = 32
HOVER_HIDE_DELAY_MS = 250

# Pure black 1/255 alpha keeps hover/click detection working with zero visible tint
BAR_TRANSPARENT_SURFACE = QColor(0, 0, 0, 1)

HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010

QUNS_BUSY = 2
QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4


def _is_fullscreen_active(screen_geo: QRect, own_hwnds: tuple[int, ...]) -> bool:
    """Detect if a fullscreen app (F11 browser video, game, presentation) is active."""
    if sys.platform != "win32":
        return False
    try:
        state = ctypes.c_int(0)
        hr = ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
        if hr == 0 and state.value in (
            QUNS_BUSY,
            QUNS_RUNNING_D3D_FULL_SCREEN,
            QUNS_PRESENTATION_MODE,
        ):
            return True

        fg_hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not fg_hwnd or int(fg_hwnd) in own_hwnds:
            return False

        # Ignore desktop / explorer shell windows
        class_buf = ctypes.create_unicode_buffer(64)
        ctypes.windll.user32.GetClassNameW(fg_hwnd, class_buf, 64)
        if class_buf.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            return False

        rect = wintypes.RECT()
        if ctypes.windll.user32.GetWindowRect(fg_hwnd, ctypes.byref(rect)):
            if (
                rect.left <= screen_geo.x()
                and rect.top <= screen_geo.y()
                and rect.right >= screen_geo.x() + screen_geo.width()
                and rect.bottom >= screen_geo.y() + screen_geo.height()
            ):
                return True
    except Exception:
        pass
    return False


def _ensure_win32_topmost(widget: QWidget) -> None:
    if sys.platform != "win32" or not widget.isVisible():
        return
    try:
        hwnd = ctypes.c_void_p(int(widget.winId()))
        ctypes.windll.user32.SetWindowPos(
            hwnd,
            ctypes.c_void_p(HWND_TOPMOST),
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
    except Exception:
        pass


class AudioBarsWidget(QWidget):
    """12-bar vertical pill audio visualizer painted smoothly with QPainter."""

    def __init__(
        self, num_bands: int = NUM_BANDS, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self._num_bands = num_bands
        self._levels: list[float] = [IDLE_LEVEL] * num_bands
        self.setFixedSize(58, 24)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def set_levels(self, levels: list[float]) -> None:
        if len(levels) == self._num_bands:
            self._levels = [max(0.0, min(1.0, float(v))) for v in levels]
            self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)

        total_w = float(self.width())
        total_h = float(self.height())
        gap = 2.0
        bar_w = max(
            2.4, (total_w - gap * (self._num_bands - 1)) / self._num_bands
        )
        max_bar_h = total_h - 2.0
        min_bar_h = 2.6

        for i, level in enumerate(self._levels):
            x = i * (bar_w + gap)
            bar_h = max(min_bar_h, level * max_bar_h)
            y = total_h - bar_h - 1.0

            alpha = int(140 + 115 * max(0.0, min(1.0, level)))
            color = QColor(ACCENT_AMBER)
            color.setAlpha(alpha)
            painter.setBrush(QBrush(color))

            radius = bar_w / 2.0
            painter.drawRoundedRect(
                QRectF(x, y, bar_w, bar_h), radius, radius
            )

        painter.end()


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class TaskbarProgressBar(QWidget):
    """Slim interactive music progress bar anchored to the bottom of the bar."""

    seek_requested = pyqtSignal(float)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._position = 0.0
        self._duration = 0.0
        self._hovered = False
        self._scrubbing = False
        self._scrub_ratio = 0.0

        self.setFixedHeight(7)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_progress(self, position_seconds: float, duration_seconds: float) -> None:
        self._duration = max(0.0, float(duration_seconds))
        self._position = max(
            0.0,
            min(float(position_seconds), self._duration) if self._duration > 0.0 else 0.0,
        )
        if self._duration > 0.0:
            self.setToolTip(
                f"{_format_duration(self._position)} / {_format_duration(self._duration)}"
            )
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.setToolTip("")
            self.unsetCursor()
        if not self._scrubbing:
            self.update()

    def _ratio_for_x(self, x: float) -> float:
        margin = 6.0
        usable = max(1.0, float(self.width()) - margin * 2.0)
        return max(0.0, min(1.0, (x - margin) / usable))

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._duration > 0.0:
            self._scrubbing = True
            self._scrub_ratio = self._ratio_for_x(event.position().x())
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._scrubbing and self._duration > 0.0:
            self._scrub_ratio = self._ratio_for_x(event.position().x())
            target = self._scrub_ratio * self._duration
            self.setToolTip(
                f"{_format_duration(target)} / {_format_duration(self._duration)}"
            )
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._scrubbing
            and self._duration > 0.0
        ):
            self._scrubbing = False
            ratio = self._ratio_for_x(event.position().x())
            target = ratio * self._duration
            self._position = target
            self.seek_requested.emit(target)
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)

        margin = 6.0
        track_w = max(1.0, float(self.width()) - margin * 2.0)
        active = self._hovered or self._scrubbing
        track_h = 3.2 if active and self._duration > 0.0 else 2.2
        y = (float(self.height()) - track_h) * 0.5
        radius = track_h * 0.5

        painter.setBrush(QBrush(QColor(255, 255, 255, 42)))
        painter.drawRoundedRect(QRectF(margin, y, track_w, track_h), radius, radius)

        if self._duration > 0.0:
            ratio = (
                self._scrub_ratio
                if self._scrubbing
                else max(0.0, min(1.0, self._position / self._duration))
            )
            fill_w = track_w * ratio
            if fill_w > 0.5:
                painter.setBrush(QBrush(ACCENT_AMBER))
                painter.drawRoundedRect(
                    QRectF(margin, y, max(track_h, fill_w), track_h),
                    radius,
                    radius,
                )
            if active:
                knob_x = margin + fill_w
                knob_y = y + track_h * 0.5
                painter.setBrush(QBrush(QColor("#ffffff")))
                painter.drawEllipse(QPointF(knob_x, knob_y), 2.8, 2.8)

        painter.end()


class TaskbarMediaBar(QWidget):
    """Seamless borderless Windows 11 taskbar media overlay bar."""

    def __init__(self) -> None:
        super().__init__(None)

        self._has_session = False
        self._is_playing = False
        self._art_bytes = b""
        self._title_full = ""
        self._artist_full = ""
        self._source_app_name = ""

        self._timeline_position = 0.0
        self._timeline_duration = 0.0
        self._timeline_updated_mono = 0.0

        self._locked = True
        self._dragging = False
        self._drag_offset_x = 0
        self._saved_x: Optional[int] = None
        self._bar_hovered = False
        self._menu_open = False
        self._hidden_for_fullscreen = False
        self._shutting_down = False

        self._load_config()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedHeight(BAR_HEIGHT)

        self._popup = PopupCard()
        self._popup.hovered_changed.connect(self._on_popup_hovered_changed)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(HOVER_HIDE_DELAY_MS)
        self._hide_timer.timeout.connect(self._on_hide_timer_timeout)

        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(250)
        self._progress_timer.timeout.connect(self._tick_progress_bar)

        # Fast 100ms Z-order & fullscreen visibility guard
        self._topmost_timer = QTimer(self)
        self._topmost_timer.setInterval(100)
        self._topmost_timer.timeout.connect(self._keep_above_taskbar)

        self._build_ui()

        self._media_backend = MediaBackend(self)
        self._audio_viz = AudioVisualizerWorker(parent=self)
        self._wire_signals()

        self._apply_session_mode(has_session=False)
        if not self._locked:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            self._progress_bar.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
            )
        self._position_on_taskbar()

        screen = QApplication.primaryScreen()
        if screen is not None:
            screen.geometryChanged.connect(self._on_screen_geometry_changed)

    def _build_ui(self) -> None:
        self._outer_layout = QVBoxLayout(self)
        self._outer_layout.setContentsMargins(0, 0, 0, 0)
        self._outer_layout.setSpacing(0)

        self._content_row = QWidget(self)
        self._content_row.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True
        )
        self._root_layout = QHBoxLayout(self._content_row)
        self._root_layout.setContentsMargins(6, 4, 8, 1)
        self._root_layout.setSpacing(8)

        self._art_label = QLabel(self._content_row)
        self._art_label.setFixedSize(ART_SIZE, ART_SIZE)
        self._art_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._art_label.setToolTip("Windows Media Controller")
        self._root_layout.addWidget(self._art_label, 0, Qt.AlignmentFlag.AlignVCenter)

        self._active_container = QWidget(self._content_row)
        self._active_container.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True
        )
        active_layout = QHBoxLayout(self._active_container)
        active_layout.setContentsMargins(0, 0, 0, 0)
        active_layout.setSpacing(8)

        text_widget = QWidget(self._active_container)
        text_widget.setFixedWidth(TEXT_MAX_WIDTH)
        text_layout = QVBoxLayout(text_widget)
        text_layout.setContentsMargins(0, 1, 0, 1)
        text_layout.setSpacing(0)

        self._title_label = QLabel(text_widget)
        title_font = QFont("Segoe UI")
        title_font.setPixelSize(12)
        title_font.setWeight(QFont.Weight.Bold)
        self._title_label.setFont(title_font)
        self._title_label.setStyleSheet(
            f"color: {TEXT_PRIMARY}; background: transparent;"
        )
        self._title_label.setFixedWidth(TEXT_MAX_WIDTH)
        text_layout.addWidget(self._title_label)

        self._artist_label = QLabel(text_widget)
        artist_font = QFont("Segoe UI")
        artist_font.setPixelSize(10)
        self._artist_label.setFont(artist_font)
        self._artist_label.setStyleSheet(
            f"color: {TEXT_MUTED}; background: transparent;"
        )
        self._artist_label.setFixedWidth(TEXT_MAX_WIDTH)
        text_layout.addWidget(self._artist_label)

        active_layout.addWidget(text_widget, 0, Qt.AlignmentFlag.AlignVCenter)

        controls_layout = QHBoxLayout()
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(2)

        self._prev_btn = TransportIconButton(
            kind="previous",
            size=QSize(24, 24),
            icon_color=LIGHT_ICON_COLOR,
            parent=self._active_container,
        )
        self._prev_btn.setToolTip("Previous")

        self._play_pause_btn = TransportIconButton(
            kind="play",
            size=QSize(26, 26),
            icon_color=LIGHT_ICON_COLOR,
            parent=self._active_container,
        )
        self._play_pause_btn.setToolTip("Play / Pause")

        self._next_btn = TransportIconButton(
            kind="next",
            size=QSize(24, 24),
            icon_color=LIGHT_ICON_COLOR,
            parent=self._active_container,
        )
        self._next_btn.setToolTip("Next")

        controls_layout.addWidget(self._prev_btn)
        controls_layout.addWidget(self._play_pause_btn)
        controls_layout.addWidget(self._next_btn)
        active_layout.addLayout(controls_layout)

        self._visualizer = AudioBarsWidget(
            num_bands=NUM_BANDS, parent=self._active_container
        )
        active_layout.addWidget(
            self._visualizer, 0, Qt.AlignmentFlag.AlignVCenter
        )

        self._root_layout.addWidget(self._active_container)
        self._outer_layout.addWidget(self._content_row, 1)

        self._progress_bar = TaskbarProgressBar(self)
        self._outer_layout.addWidget(self._progress_bar, 0)

        self._refresh_art_pixmap()

    def _wire_signals(self) -> None:
        self._media_backend.session_active_changed.connect(
            self._on_session_active_changed
        )
        self._media_backend.track_changed.connect(self._on_track_changed)
        self._media_backend.album_art_changed.connect(self._on_album_art_changed)
        self._media_backend.source_app_changed.connect(
            self._on_source_app_changed
        )
        self._media_backend.playback_status_changed.connect(
            self._on_playback_status_changed
        )
        self._media_backend.shuffle_changed.connect(
            self._popup.update_shuffle_state
        )
        self._media_backend.repeat_changed.connect(
            self._popup.update_repeat_state
        )
        self._media_backend.timeline_changed.connect(self._on_timeline_changed)
        self._media_backend.media_state_changed.connect(
            self._on_media_state_changed
        )

        self._audio_viz.levels_updated.connect(self._visualizer.set_levels)
        self._progress_bar.seek_requested.connect(self._on_seek_requested)

        self._prev_btn.clicked.connect(self._media_backend.previous_track)
        self._play_pause_btn.clicked.connect(
            self._media_backend.toggle_play_pause
        )
        self._next_btn.clicked.connect(self._media_backend.next_track)

        self._popup.shuffle_clicked.connect(self._media_backend.toggle_shuffle)
        self._popup.previous_clicked.connect(self._media_backend.previous_track)
        self._popup.play_pause_clicked.connect(
            self._media_backend.toggle_play_pause
        )
        self._popup.next_clicked.connect(self._media_backend.next_track)
        self._popup.repeat_clicked.connect(
            self._media_backend.cycle_repeat_mode
        )

    def start_workers(self) -> None:
        self._media_backend.start()
        self._audio_viz.start()
        self._topmost_timer.start()

    def _on_session_active_changed(self, has_session: bool) -> None:
        self._apply_session_mode(has_session)
        if not has_session:
            self._audio_viz.set_active(False)

    def _on_track_changed(self, title: str, artist: str) -> None:
        self._title_full = title
        self._artist_full = artist

        title_metrics = QFontMetrics(self._title_label.font())
        self._title_label.setText(
            title_metrics.elidedText(
                title or "No Media",
                Qt.TextElideMode.ElideRight,
                TEXT_MAX_WIDTH,
            )
        )

        artist_metrics = QFontMetrics(self._artist_label.font())
        self._artist_label.setText(
            artist_metrics.elidedText(
                artist or "",
                Qt.TextElideMode.ElideRight,
                TEXT_MAX_WIDTH,
            )
        )
        self._popup.update_track(title, artist)

    def _on_album_art_changed(self, art_bytes: bytes) -> None:
        self._art_bytes = art_bytes or b""
        self._refresh_art_pixmap()
        self._popup.update_album_art(self._art_bytes)

    def _on_source_app_changed(
        self, _raw_app_id: str, display_name: str
    ) -> None:
        self._source_app_name = display_name
        self._popup.update_source_app(display_name)

    def _on_playback_status_changed(self, is_playing: bool) -> None:
        self._is_playing = bool(is_playing)
        self._play_pause_btn.set_kind("pause" if self._is_playing else "play")
        self._popup.update_playback_status(self._is_playing)
        self._audio_viz.set_active(self._has_session and self._is_playing)
        self._sync_progress_timer()

    def _on_timeline_changed(
        self, position_sec: float, duration_sec: float, updated_mono: float
    ) -> None:
        self._timeline_position = max(0.0, float(position_sec))
        self._timeline_duration = max(0.0, float(duration_sec))
        self._timeline_updated_mono = float(updated_mono) or time.monotonic()
        self._tick_progress_bar()
        self._sync_progress_timer()

    def _on_seek_requested(self, target_seconds: float) -> None:
        self._timeline_position = max(
            0.0, min(float(target_seconds), self._timeline_duration)
        )
        self._timeline_updated_mono = time.monotonic()
        self._progress_bar.set_progress(
            self._timeline_position, self._timeline_duration
        )
        self._media_backend.seek_to(self._timeline_position)

    def _sync_progress_timer(self) -> None:
        should_tick = (
            self._has_session
            and self._is_playing
            and self._timeline_duration > 0.0
            and not self._shutting_down
        )
        if should_tick and not self._progress_timer.isActive():
            self._progress_timer.start()
        elif not should_tick and self._progress_timer.isActive():
            self._progress_timer.stop()

    def _tick_progress_bar(self) -> None:
        if not self._has_session or self._timeline_duration <= 0.0:
            self._progress_bar.set_progress(0.0, 0.0)
            return
        pos = self._timeline_position
        if self._is_playing and self._timeline_updated_mono > 0.0:
            elapsed = max(0.0, time.monotonic() - self._timeline_updated_mono)
            pos = min(self._timeline_duration, pos + elapsed)
        self._progress_bar.set_progress(pos, self._timeline_duration)

    def _on_media_state_changed(self, state: MediaState) -> None:
        if state.has_session:
            tip = state.title
            if state.artist:
                tip = f"{state.title} — {state.artist}"
            self._art_label.setToolTip(tip)
        else:
            self._art_label.setToolTip("No active media (right-click for menu)")

    def _apply_session_mode(self, has_session: bool) -> None:
        self._has_session = bool(has_session)

        if self._has_session:
            self._root_layout.setContentsMargins(6, 4, 8, 1)
            self._active_container.setVisible(True)
            self._progress_bar.setVisible(True)
            self.setFixedSize(self._expanded_width(), BAR_HEIGHT)
        else:
            self._hide_timer.stop()
            self._progress_timer.stop()
            self._popup.hide_immediately()
            self._active_container.setVisible(False)
            self._progress_bar.setVisible(False)
            self._progress_bar.set_progress(0.0, 0.0)
            self._timeline_position = 0.0
            self._timeline_duration = 0.0
            self._root_layout.setContentsMargins(6, 6, 6, 6)
            self.setFixedSize(COLLAPSED_WIDTH, BAR_HEIGHT)
            self._art_bytes = b""
            self._refresh_art_pixmap()

        self._sync_progress_timer()
        self._position_on_taskbar()
        if not self._hidden_for_fullscreen:
            _ensure_win32_topmost(self)

    def _expanded_width(self) -> int:
        return 346

    def _refresh_art_pixmap(self) -> None:
        dpr = self.devicePixelRatioF() if hasattr(self, "devicePixelRatioF") else 1.0
        art = self._art_bytes if self._has_session else b""
        pixmap = render_album_art_pixmap(art, ART_SIZE, radius=7, dpr=dpr)
        self._art_label.setPixmap(pixmap)

    def _screen_geometry(self) -> QRect:
        screen = QApplication.primaryScreen()
        if screen is None:
            return QRect(0, 0, 1920, 1080)
        return screen.geometry()

    def _taskbar_y(self) -> int:
        geo = self._screen_geometry()
        return geo.y() + geo.height() - BAR_BOTTOM_OFFSET

    def _clamp_x(self, x: int) -> int:
        geo = self._screen_geometry()
        min_x = geo.x() + 4
        max_x = geo.x() + geo.width() - self.width() - 4
        return max(min_x, min(x, max_x))

    def _position_on_taskbar(self) -> None:
        geo = self._screen_geometry()
        y = self._taskbar_y()

        if self._saved_x is None:
            default_x = geo.x() + geo.width() - self._expanded_width() - 260
            x = self._clamp_x(default_x)
            self._saved_x = x
        else:
            x = self._clamp_x(self._saved_x)

        self.move(x, y)

    def _on_screen_geometry_changed(self, _geo: QRect) -> None:
        self._position_on_taskbar()
        if self._popup.isVisible():
            px, py = self._popup_position()
            self._popup.move(px, py)

    def _popup_position(self) -> tuple[int, int]:
        geo = self._screen_geometry()
        taskbar_y = self._taskbar_y()
        popup_y = taskbar_y - POPUP_VERTICAL_OFFSET

        bar_center_x = self.x() + (self.width() // 2)
        popup_x = bar_center_x - (PopupCard.WIDTH // 2)
        min_x = geo.x() + 8
        max_x = geo.x() + geo.width() - PopupCard.WIDTH - 8
        popup_x = max(min_x, min(popup_x, max_x))
        return popup_x, popup_y

    def _load_config(self) -> None:
        if not CONFIG_PATH.exists():
            return
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if isinstance(data.get("x"), (int, float)):
                    self._saved_x = int(data["x"])
                if isinstance(data.get("locked"), bool):
                    self._locked = data["locked"]
        except Exception:
            logger.debug("Could not read config.json; using defaults.", exc_info=True)

    def _save_config(self) -> None:
        payload = {
            "x": int(self._saved_x if self._saved_x is not None else self.x()),
            "locked": bool(self._locked),
        }
        try:
            CONFIG_PATH.write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
        except Exception:
            logger.debug("Could not persist config.json.", exc_info=True)

    def _keep_above_taskbar(self) -> None:
        if self._menu_open or self._shutting_down:
            return

        own_hwnds = (int(self.winId()), int(self._popup.winId()))
        is_fs = _is_fullscreen_active(self._screen_geometry(), own_hwnds)

        if is_fs:
            if not self._hidden_for_fullscreen:
                self._hidden_for_fullscreen = True
                self._hide_timer.stop()
                self._popup.hide_immediately()
                self.hide()
            return

        if self._hidden_for_fullscreen:
            self._hidden_for_fullscreen = False
            self.show()

        _ensure_win32_topmost(self)
        if self._popup.isVisible():
            _ensure_win32_topmost(self._popup)

    def _show_popup_if_ready(self) -> None:
        if (
            not self._has_session
            or not self._locked
            or self._dragging
            or self._menu_open
            or self._hidden_for_fullscreen
            or self._shutting_down
        ):
            return
        self._hide_timer.stop()
        px, py = self._popup_position()
        self._popup.show_smooth(px, py)
        _ensure_win32_topmost(self._popup)

    def _schedule_popup_hide(self) -> None:
        if not self._popup.isVisible():
            return
        if not self._bar_hovered and not self._popup.is_hovered:
            self._hide_timer.start(HOVER_HIDE_DELAY_MS)

    def _on_popup_hovered_changed(self, hovered: bool) -> None:
        if hovered:
            self._hide_timer.stop()
        else:
            self._schedule_popup_hide()

    def _on_hide_timer_timeout(self) -> None:
        cursor_pos = QCursor.pos()
        over_bar = self.geometry().contains(cursor_pos)
        over_popup = self._popup.isVisible() and self._popup.geometry().contains(
            cursor_pos
        )
        if over_bar or over_popup:
            return
        self._popup.hide_smooth()

    def contextMenuEvent(self, event) -> None:
        self._menu_open = True
        self._hide_timer.stop()
        self._popup.hide_immediately()

        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu {"
            "  background-color: #161920;"
            "  color: #ffffff;"
            "  border: 1px solid #2a303c;"
            "  border-radius: 8px;"
            "  padding: 4px;"
            "  font-family: 'Segoe UI';"
            "  font-size: 12px;"
            "}"
            "QMenu::item {"
            "  padding: 6px 16px;"
            "  border-radius: 5px;"
            "}"
            "QMenu::item:selected {"
            "  background-color: #262c38;"
            "}"
            "QMenu::separator {"
            "  height: 1px;"
            "  background: #2a303c;"
            "  margin: 4px 8px;"
            "}"
        )

        lock_label = "Unlock Position" if self._locked else "Lock Position"
        toggle_lock_action = QAction(lock_label, menu)
        toggle_lock_action.triggered.connect(self._toggle_lock_position)
        menu.addAction(toggle_lock_action)

        menu.addSeparator()

        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self.shutdown_and_quit)
        menu.addAction(quit_action)

        menu.exec(event.globalPos())
        self._menu_open = False

    def _toggle_lock_position(self) -> None:
        self._locked = not self._locked
        self._progress_bar.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, not self._locked
        )
        if self._locked:
            self._dragging = False
            self.unsetCursor()
            self._saved_x = self.x()
        else:
            self._popup.hide_immediately()
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        self._save_config()
        self.update()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if not self._shutting_down and not self._hidden_for_fullscreen:
            _ensure_win32_topmost(self)

    def enterEvent(self, event) -> None:
        self._bar_hovered = True
        self._show_popup_if_ready()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._bar_hovered = False
        self._schedule_popup_hide()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._locked:
                self._dragging = True
                global_pos = event.globalPosition().toPoint()
                self._drag_offset_x = global_pos.x() - self.x()
                event.accept()
                return
            if not self._has_session:
                self._media_backend.request_refresh()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._dragging and not self._locked:
            global_pos: QPoint = event.globalPosition().toPoint()
            new_x = self._clamp_x(global_pos.x() - self._drag_offset_x)
            self.move(new_x, self._taskbar_y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._dragging
            and not self._locked
        ):
            self._dragging = False
            self._saved_x = self.x()
            self._save_config()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if not self._locked:
            painter.setBrush(QBrush(BAR_UNLOCKED_SURFACE))
            painter.setPen(QPen(UNLOCKED_BORDER_COLOR, 1.0))
        else:
            painter.setBrush(QBrush(BAR_TRANSPARENT_SURFACE))
            painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, 8.0, 8.0)
        painter.end()

    def shutdown_and_quit(self) -> None:
        self.cleanup_threads()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def cleanup_threads(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True

        self._hide_timer.stop()
        self._progress_timer.stop()
        self._topmost_timer.stop()
        self._popup.hide_immediately()
        self._popup.close()

        self._audio_viz.stop(timeout_ms=2500)
        self._media_backend.stop(timeout_ms=3000)

    def closeEvent(self, event) -> None:
        self.cleanup_threads()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Win11 Taskbar Media Controller")
    app.setQuitOnLastWindowClosed(False)

    bar = TaskbarMediaBar()
    app.aboutToQuit.connect(bar.cleanup_threads)

    signal.signal(signal.SIGINT, lambda *_args: bar.shutdown_and_quit())
    sigint_pump = QTimer()
    sigint_pump.setInterval(250)
    sigint_pump.timeout.connect(lambda: None)
    sigint_pump.start()

    bar.show()
    _ensure_win32_topmost(bar)
    bar.start_workers()

    exit_code = app.exec()
    sigint_pump.stop()
    bar.cleanup_threads()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())