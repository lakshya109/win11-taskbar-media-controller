"""Windows 11 GSMTC media session reader and controller backend.

Runs inside a dedicated QThread owning an isolated asyncio event loop so all
WinRT GlobalSystemMediaTransportControls calls execute safely off the PyQt6
GUI thread and shut down cleanly without dangling COM/WinRT callbacks.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSession as MediaSession,
        GlobalSystemMediaTransportControlsSessionManager as MediaSessionManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
    )
    from winrt.windows.storage.streams import DataReader

    try:
        from winrt.windows.media import MediaPlaybackAutoRepeatMode
    except ImportError:
        MediaPlaybackAutoRepeatMode = None  # type: ignore[assignment]

    WINRT_AVAILABLE = True
except ImportError:
    MediaSession = None  # type: ignore[assignment]
    MediaSessionManager = None  # type: ignore[assignment]
    PlaybackStatus = None  # type: ignore[assignment]
    MediaPlaybackAutoRepeatMode = None  # type: ignore[assignment]
    DataReader = None  # type: ignore[assignment]
    WINRT_AVAILABLE = False

logger = logging.getLogger(__name__)

MAX_THUMBNAIL_BYTES = 15 * 1024 * 1024
THUMBNAIL_CHUNK_SIZE = 65536

EVENT_COALESCE_SECONDS = 0.06
RECONCILE_INTERVAL_SECONDS = 2.0

_KNOWN_APP_NAMES: tuple[tuple[str, str], ...] = (
    ("spotify", "Spotify"),
    ("chrome", "Chrome"),
    ("msedge", "Edge"),
    ("firefox", "Firefox"),
    ("brave", "Brave"),
    ("opera", "Opera"),
    ("vivaldi", "Vivaldi"),
    ("applemusic", "Apple Music"),
    ("itunes", "iTunes"),
    ("zunemusic", "Media Player"),
    ("zunevideo", "Movies & TV"),
    ("vlc", "VLC"),
    ("tidal", "TIDAL"),
    ("amazonmusic", "Amazon Music"),
    ("musicbee", "MusicBee"),
    ("foobar2000", "foobar2000"),
    ("winamp", "Winamp"),
    ("aimp", "AIMP"),
    ("potplayer", "PotPlayer"),
    ("mpv", "mpv"),
    ("discord", "Discord"),
    ("ytmdesktop", "YouTube Music"),
    ("youtubemusic", "YouTube Music"),
    ("soundcloud", "SoundCloud"),
    ("deezer", "Deezer"),
    ("qobuz", "Qobuz"),
    ("plex", "Plex"),
)


def format_source_app_name(app_id: str) -> str:
    """Convert a Windows AppUserModelID or executable name into a clean badge label."""
    if not app_id:
        return ""

    normalized = app_id.strip()
    lower = normalized.lower()
    for token, display_name in _KNOWN_APP_NAMES:
        if token in lower:
            return display_name

    if "!" in normalized:
        prefix, entry = normalized.split("!", 1)
        candidate = entry if entry.lower() not in {"app", "application", "main"} else prefix
        if "_" in candidate:
            candidate = candidate.split("_", 1)[0]
        if "." in candidate:
            candidate = candidate.rsplit(".", 1)[-1]
        cleaned = candidate.strip()
        if cleaned:
            return cleaned

    base = os.path.basename(normalized.replace("\\", "/"))
    stem, _ = os.path.splitext(base)
    if "-" in stem and len(stem.rsplit("-", 1)[-1]) >= 8:
        stem = stem.rsplit("-", 1)[0]
    cleaned = stem.strip()
    return cleaned or normalized


def _timespan_to_seconds(value) -> float:
    """Convert a Python timedelta or WinRT TimeSpan (100ns ticks) to seconds."""
    if value is None:
        return 0.0
    if isinstance(value, timedelta):
        return max(0.0, float(value.total_seconds()))
    duration = getattr(value, "duration", None)
    if duration is not None:
        try:
            return max(0.0, float(duration) / 10_000_000.0)
        except Exception:
            return 0.0
    try:
        return max(0.0, float(value))
    except Exception:
        return 0.0


@dataclass(frozen=True)
class MediaState:
    """Immutable snapshot of the active Windows media session."""

    has_session: bool = False
    title: str = ""
    artist: str = ""
    album_art: bytes = b""
    source_app_id: str = ""
    source_app_name: str = ""
    is_playing: bool = False
    playback_status: str = "Closed"
    is_shuffle_active: bool = False
    shuffle_enabled: bool = False
    repeat_mode: str = "None"
    repeat_enabled: bool = False
    position_seconds: float = 0.0
    duration_seconds: float = 0.0
    timeline_updated_monotonic: float = 0.0


class MediaBackend(QThread):
    """Background QThread reading Windows GSMTC media sessions via WinRT asyncio."""

    track_changed = pyqtSignal(str, str)
    album_art_changed = pyqtSignal(bytes)
    source_app_changed = pyqtSignal(str, str)
    playback_status_changed = pyqtSignal(bool)
    shuffle_changed = pyqtSignal(bool, bool)
    repeat_changed = pyqtSignal(str, bool)
    timeline_changed = pyqtSignal(float, float, float)
    session_active_changed = pyqtSignal(bool)
    media_state_changed = pyqtSignal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_lock = threading.Lock()
        self._stop_requested = False

        self._shutdown_event: Optional[asyncio.Event] = None
        self._refresh_event: Optional[asyncio.Event] = None
        self._session_rebind_needed = False

        self._manager: Optional[MediaSessionManager] = None
        self._manager_token = None
        self._session: Optional[MediaSession] = None
        self._media_props_token = None
        self._playback_info_token = None
        self._timeline_token = None
        self._timeline_start_seconds: float = 0.0

        self._has_emitted_initial = False
        self._last_state = MediaState()

    def toggle_play_pause(self) -> None:
        self._schedule_command("toggle_play_pause")

    def play(self) -> None:
        self._schedule_command("play")

    def pause(self) -> None:
        self._schedule_command("pause")

    def next_track(self) -> None:
        self._schedule_command("next")

    def previous_track(self) -> None:
        self._schedule_command("previous")

    def toggle_shuffle(self) -> None:
        self._schedule_command("toggle_shuffle")

    def cycle_repeat_mode(self) -> None:
        self._schedule_command("cycle_repeat")

    def seek_to(self, position_seconds: float) -> None:
        if self._stop_requested:
            return
        with self._loop_lock:
            loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._execute_seek(max(0.0, float(position_seconds))), loop
            )
            future.add_done_callback(self._log_command_error)
        except RuntimeError:
            pass

    def request_refresh(self) -> None:
        self._trigger_refresh(rebind_session=False)

    def stop(self, timeout_ms: int = 3000) -> None:
        self._stop_requested = True

        with self._loop_lock:
            loop = self._loop
            shutdown_event = self._shutdown_event

        if loop is not None and not loop.is_closed() and shutdown_event is not None:
            try:
                loop.call_soon_threadsafe(shutdown_event.set)
            except RuntimeError:
                pass

        if self.isRunning():
            finished = self.wait(timeout_ms)
            if not finished:
                logger.warning(
                    "MediaBackend thread did not exit within %d ms.", timeout_ms
                )

    def run(self) -> None:
        self._stop_requested = False

        if not WINRT_AVAILABLE:
            logger.warning(
                "WinRT media packages are unavailable; emitting idle media state."
            )
            self._emit_state(MediaState(), force_initial=True)
            return

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._loop_lock:
            self._loop = loop
            self._shutdown_event = asyncio.Event()
            self._refresh_event = asyncio.Event()

        try:
            loop.run_until_complete(self._async_main())
        except Exception:
            logger.exception("Unexpected error in MediaBackend asyncio loop.")
        finally:
            self._teardown_loop(loop)
            with self._loop_lock:
                self._loop = None
                self._shutdown_event = None
                self._refresh_event = None

    def _teardown_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        try:
            pending = [
                task for task in asyncio.all_tasks(loop) if not task.done()
            ]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            logger.debug("Error while draining MediaBackend tasks.", exc_info=True)
        finally:
            try:
                asyncio.set_event_loop(None)
            except Exception:
                pass
            if not loop.is_closed():
                loop.close()

    async def _async_main(self) -> None:
        assert self._shutdown_event is not None
        assert self._refresh_event is not None

        try:
            self._manager = await MediaSessionManager.request_async()
        except Exception:
            logger.exception("Failed to request Windows GSMTC SessionManager.")
            self._emit_state(MediaState(), force_initial=True)
            return

        if self._stop_requested:
            return

        try:
            self._manager_token = self._manager.add_current_session_changed(
                self._on_current_session_changed
            )
        except Exception:
            logger.exception("Failed to subscribe to current_session_changed.")

        self._rebind_current_session()
        await self._sync_current_state(force_initial=True)

        refresh_task = asyncio.create_task(self._refresh_worker())
        reconcile_task = asyncio.create_task(self._reconcile_worker())

        try:
            await self._shutdown_event.wait()
        finally:
            for task in (refresh_task, reconcile_task):
                task.cancel()
            await asyncio.gather(
                refresh_task, reconcile_task, return_exceptions=True
            )
            self._detach_session()
            self._detach_manager()

    async def _refresh_worker(self) -> None:
        assert self._refresh_event is not None
        while not self._stop_requested:
            await self._refresh_event.wait()
            if self._stop_requested:
                break

            await asyncio.sleep(EVENT_COALESCE_SECONDS)
            if self._stop_requested:
                break

            self._refresh_event.clear()
            if self._session_rebind_needed:
                self._session_rebind_needed = False
                self._rebind_current_session()

            await self._sync_current_state(force_initial=False)

    async def _reconcile_worker(self) -> None:
        assert self._shutdown_event is not None
        while not self._stop_requested:
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=RECONCILE_INTERVAL_SECONDS,
                )
                break
            except asyncio.TimeoutError:
                if self._stop_requested:
                    break
                if self._manager is not None:
                    try:
                        current = self._manager.get_current_session()
                    except Exception:
                        current = None

                    current_id = self._safe_app_id(current)
                    bound_id = self._safe_app_id(self._session)
                    if (current is None) != (self._session is None) or (
                        current_id != bound_id
                    ):
                        self._rebind_current_session()
                    await self._sync_current_state(force_initial=False)

    def _on_current_session_changed(self, _sender, _args) -> None:
        if self._stop_requested:
            return
        self._trigger_refresh(rebind_session=True)

    def _on_media_properties_changed(self, _sender, _args) -> None:
        if self._stop_requested:
            return
        self._trigger_refresh(rebind_session=False)

    def _on_playback_info_changed(self, _sender, _args) -> None:
        if self._stop_requested:
            return
        self._trigger_refresh(rebind_session=False)

    def _on_timeline_properties_changed(self, _sender, _args) -> None:
        if self._stop_requested:
            return
        self._trigger_refresh(rebind_session=False)

    def _trigger_refresh(self, *, rebind_session: bool) -> None:
        if self._stop_requested:
            return
        with self._loop_lock:
            loop = self._loop
            refresh_event = self._refresh_event

        if loop is None or loop.is_closed() or refresh_event is None:
            return

        def _wake() -> None:
            if rebind_session:
                self._session_rebind_needed = True
            refresh_event.set()

        try:
            loop.call_soon_threadsafe(_wake)
        except RuntimeError:
            pass

    def _rebind_current_session(self) -> None:
        self._detach_session()
        if self._manager is None or self._stop_requested:
            return

        try:
            session = self._manager.get_current_session()
        except Exception:
            logger.debug("Failed to retrieve current media session.", exc_info=True)
            session = None

        if session is None:
            return

        self._session = session
        try:
            self._media_props_token = session.add_media_properties_changed(
                self._on_media_properties_changed
            )
        except Exception:
            logger.debug("Could not attach media_properties_changed.", exc_info=True)
            self._media_props_token = None

        try:
            self._playback_info_token = session.add_playback_info_changed(
                self._on_playback_info_changed
            )
        except Exception:
            logger.debug("Could not attach playback_info_changed.", exc_info=True)
            self._playback_info_token = None

        try:
            self._timeline_token = session.add_timeline_properties_changed(
                self._on_timeline_properties_changed
            )
        except Exception:
            logger.debug("Could not attach timeline_properties_changed.", exc_info=True)
            self._timeline_token = None

    def _detach_session(self) -> None:
        session = self._session
        self._session = None
        self._timeline_start_seconds = 0.0

        if session is None:
            self._media_props_token = None
            self._playback_info_token = None
            self._timeline_token = None
            return

        if self._media_props_token is not None:
            try:
                session.remove_media_properties_changed(self._media_props_token)
            except Exception:
                pass
            self._media_props_token = None

        if self._playback_info_token is not None:
            try:
                session.remove_playback_info_changed(self._playback_info_token)
            except Exception:
                pass
            self._playback_info_token = None

        if self._timeline_token is not None:
            try:
                session.remove_timeline_properties_changed(self._timeline_token)
            except Exception:
                pass
            self._timeline_token = None

    def _detach_manager(self) -> None:
        manager = self._manager
        self._manager = None

        if manager is not None and self._manager_token is not None:
            try:
                manager.remove_current_session_changed(self._manager_token)
            except Exception:
                pass
        self._manager_token = None

    async def _sync_current_state(self, *, force_initial: bool) -> None:
        if self._stop_requested:
            return

        session = self._session
        if session is None:
            self._emit_state(MediaState(), force_initial=force_initial)
            return

        source_app_id = self._safe_app_id(session)
        source_app_name = format_source_app_name(source_app_id)
        (
            is_playing,
            status_name,
            is_shuffle_active,
            shuffle_enabled,
            repeat_mode,
            repeat_enabled,
        ) = self._read_playback_details(session)

        title = ""
        artist = ""
        album_art = b""

        try:
            props = await session.try_get_media_properties_async()
        except Exception:
            logger.debug("try_get_media_properties_async failed.", exc_info=True)
            props = None
            if self._manager is not None:
                try:
                    if self._manager.get_current_session() is None:
                        self._detach_session()
                        self._emit_state(MediaState(), force_initial=force_initial)
                        return
                except Exception:
                    pass

        if self._stop_requested:
            return

        if props is not None:
            title = (props.title or "").strip()
            artist = (props.artist or "").strip()
            if not artist:
                artist = (
                    getattr(props, "album_artist", "")
                    or getattr(props, "subtitle", "")
                    or ""
                ).strip()

            thumbnail_ref = getattr(props, "thumbnail", None)
            if thumbnail_ref is not None:
                album_art = await self._read_thumbnail_bytes(thumbnail_ref)

        if self._stop_requested:
            return

        has_session = bool(
            session is not None
            and (status_name != "Closed" or bool(title) or is_playing)
        )

        if not has_session:
            self._timeline_start_seconds = 0.0
            new_state = MediaState()
        else:
            pos_sec, dur_sec, start_sec, updated_mono = self._read_timeline(
                session, is_playing
            )
            self._timeline_start_seconds = start_sec
            new_state = MediaState(
                has_session=True,
                title=title or source_app_name or "Unknown Track",
                artist=artist,
                album_art=album_art,
                source_app_id=source_app_id,
                source_app_name=source_app_name,
                is_playing=is_playing,
                playback_status=status_name,
                is_shuffle_active=is_shuffle_active,
                shuffle_enabled=shuffle_enabled,
                repeat_mode=repeat_mode,
                repeat_enabled=repeat_enabled,
                position_seconds=pos_sec,
                duration_seconds=dur_sec,
                timeline_updated_monotonic=updated_mono,
            )

        self._emit_state(new_state, force_initial=force_initial)

    @staticmethod
    def _safe_app_id(session: Optional[MediaSession]) -> str:
        if session is None:
            return ""
        try:
            return str(session.source_app_user_model_id or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _parse_repeat_mode(raw_mode) -> str:
        if raw_mode is None:
            return "None"
        if MediaPlaybackAutoRepeatMode is not None:
            if raw_mode == MediaPlaybackAutoRepeatMode.TRACK:
                return "Track"
            if raw_mode == MediaPlaybackAutoRepeatMode.LIST:
                return "List"
            if raw_mode == MediaPlaybackAutoRepeatMode.NONE:
                return "None"
        try:
            int_val = int(raw_mode)
            if int_val == 1:
                return "Track"
            if int_val == 2:
                return "List"
        except Exception:
            pass
        name = str(getattr(raw_mode, "name", raw_mode)).lower()
        if "track" in name:
            return "Track"
        if "list" in name:
            return "List"
        return "None"

    @classmethod
    def _read_playback_details(
        cls, session: MediaSession
    ) -> tuple[bool, str, bool, bool, str, bool]:
        try:
            info = session.get_playback_info()
            if info is None:
                return False, "Closed", False, False, "None", False
            status = info.playback_status
        except Exception:
            return False, "Closed", False, False, "None", False

        if PlaybackStatus is None:
            return False, "Closed", False, False, "None", False

        status_map = {
            PlaybackStatus.CLOSED: "Closed",
            PlaybackStatus.OPENED: "Opened",
            PlaybackStatus.CHANGING: "Changing",
            PlaybackStatus.STOPPED: "Stopped",
            PlaybackStatus.PLAYING: "Playing",
            PlaybackStatus.PAUSED: "Paused",
        }
        label = status_map.get(status, "Paused")
        is_playing = status == PlaybackStatus.PLAYING

        controls = getattr(info, "controls", None)
        shuffle_enabled = bool(getattr(controls, "is_shuffle_enabled", False))
        repeat_enabled = bool(getattr(controls, "is_repeat_enabled", False))

        raw_shuffle = getattr(info, "is_shuffle_active", None)
        is_shuffle_active = bool(raw_shuffle) if raw_shuffle is not None else False

        raw_repeat = getattr(info, "auto_repeat_mode", None)
        repeat_mode = cls._parse_repeat_mode(raw_repeat)

        return (
            is_playing,
            label,
            is_shuffle_active,
            shuffle_enabled,
            repeat_mode,
            repeat_enabled,
        )

    @staticmethod
    def _read_timeline(
        session: MediaSession, is_playing: bool
    ) -> tuple[float, float, float, float]:
        now_mono = time.monotonic()
        try:
            timeline = session.get_timeline_properties()
            if timeline is None:
                return 0.0, 0.0, 0.0, now_mono

            start_sec = _timespan_to_seconds(getattr(timeline, "start_time", None))
            end_sec = _timespan_to_seconds(getattr(timeline, "end_time", None))
            raw_pos = _timespan_to_seconds(getattr(timeline, "position", None))
            duration_sec = max(0.0, end_sec - start_sec)
            pos_sec = max(0.0, raw_pos - start_sec)

            if duration_sec > 0.0:
                if is_playing:
                    last_updated = getattr(timeline, "last_updated_time", None)
                    if isinstance(last_updated, datetime) and last_updated.year >= 2000:
                        if last_updated.tzinfo is None:
                            last_updated = last_updated.replace(tzinfo=timezone.utc)
                        elapsed = (
                            datetime.now(timezone.utc) - last_updated
                        ).total_seconds()
                        if 0.0 < elapsed < duration_sec + 5.0:
                            pos_sec += elapsed
                pos_sec = max(0.0, min(pos_sec, duration_sec))
            else:
                pos_sec = 0.0

            return pos_sec, duration_sec, start_sec, now_mono
        except Exception:
            logger.debug("Could not read timeline properties.", exc_info=True)
            return 0.0, 0.0, 0.0, now_mono

    async def _read_thumbnail_bytes(self, thumbnail_ref) -> bytes:
        stream = None
        reader = None
        try:
            stream = await thumbnail_ref.open_read_async()
            if stream is None:
                return b""

            size = int(stream.size)
            if size <= 0 or size > MAX_THUMBNAIL_BYTES:
                return b""

            try:
                stream.seek(0)
            except Exception:
                pass

            reader = DataReader(stream)
            output = bytearray()
            remaining = size

            while remaining > 0 and not self._stop_requested:
                chunk_size = min(remaining, THUMBNAIL_CHUNK_SIZE)
                loaded = int(await reader.load_async(chunk_size))
                if loaded <= 0:
                    break

                buf = bytearray(loaded)
                reader.read_bytes(buf)
                output.extend(buf)
                remaining -= loaded

            return bytes(output) if not self._stop_requested else b""
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Failed to read album art thumbnail stream.", exc_info=True)
            return b""
        finally:
            if reader is not None:
                try:
                    reader.detach_stream()
                except Exception:
                    pass
                try:
                    reader.close()
                except Exception:
                    pass
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

    def _emit_state(self, state: MediaState, *, force_initial: bool) -> None:
        if self._stop_requested:
            return

        prev = self._last_state
        initial = force_initial and not self._has_emitted_initial
        any_changed = initial

        if initial or state.has_session != prev.has_session:
            self.session_active_changed.emit(state.has_session)
            any_changed = True

        if initial or state.title != prev.title or state.artist != prev.artist:
            self.track_changed.emit(state.title, state.artist)
            any_changed = True

        if initial or state.album_art != prev.album_art:
            self.album_art_changed.emit(state.album_art)
            any_changed = True

        if initial or state.source_app_id != prev.source_app_id:
            self.source_app_changed.emit(
                state.source_app_id, state.source_app_name
            )
            any_changed = True

        if initial or state.is_playing != prev.is_playing:
            self.playback_status_changed.emit(state.is_playing)
            any_changed = True

        if (
            initial
            or state.is_shuffle_active != prev.is_shuffle_active
            or state.shuffle_enabled != prev.shuffle_enabled
        ):
            self.shuffle_changed.emit(
                state.is_shuffle_active, state.shuffle_enabled
            )
            any_changed = True

        if (
            initial
            or state.repeat_mode != prev.repeat_mode
            or state.repeat_enabled != prev.repeat_enabled
        ):
            self.repeat_changed.emit(state.repeat_mode, state.repeat_enabled)
            any_changed = True

        timeline_changed = (
            initial
            or abs(state.duration_seconds - prev.duration_seconds) > 0.25
            or abs(state.position_seconds - prev.position_seconds) > 0.35
            or state.has_session != prev.has_session
            or state.is_playing != prev.is_playing
            or state.title != prev.title
        )
        if timeline_changed:
            self.timeline_changed.emit(
                state.position_seconds,
                state.duration_seconds,
                state.timeline_updated_monotonic,
            )
            any_changed = True

        if any_changed or state.playback_status != prev.playback_status:
            self._last_state = state
            self._has_emitted_initial = True
            self.media_state_changed.emit(state)

    def _schedule_command(self, command: str) -> None:
        if self._stop_requested:
            return

        with self._loop_lock:
            loop = self._loop

        if loop is None or loop.is_closed():
            return

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._execute_command(command), loop
            )
            future.add_done_callback(self._log_command_error)
        except RuntimeError:
            pass

    @staticmethod
    def _log_command_error(future) -> None:
        if future.cancelled():
            return
        exc = future.exception()
        if exc is not None:
            logger.debug("Media transport command failed: %s", exc)

    async def _execute_command(self, command: str) -> None:
        if self._stop_requested:
            return

        session = self._session
        if session is None and self._manager is not None:
            self._rebind_current_session()
            session = self._session

        if session is None:
            return

        try:
            if command == "toggle_play_pause":
                await session.try_toggle_play_pause_async()
            elif command == "play":
                await session.try_play_async()
            elif command == "pause":
                await session.try_pause_async()
            elif command == "next":
                await session.try_skip_next_async()
            elif command == "previous":
                await session.try_skip_previous_async()
            elif command == "toggle_shuffle":
                await self._execute_toggle_shuffle(session)
            elif command == "cycle_repeat":
                await self._execute_cycle_repeat(session)
        except Exception:
            logger.debug("WinRT command '%s' failed.", command, exc_info=True)
        finally:
            self._trigger_refresh(rebind_session=False)

    async def _execute_toggle_shuffle(self, session: MediaSession) -> None:
        info = session.get_playback_info()
        if info is None:
            return
        controls = getattr(info, "controls", None)
        if not getattr(controls, "is_shuffle_enabled", False):
            return
        current = bool(getattr(info, "is_shuffle_active", False))
        await session.try_change_shuffle_active_async(not current)

    async def _execute_cycle_repeat(self, session: MediaSession) -> None:
        info = session.get_playback_info()
        if info is None:
            return
        controls = getattr(info, "controls", None)
        if not getattr(controls, "is_repeat_enabled", False):
            return

        raw_current = getattr(info, "auto_repeat_mode", None)
        current_label = self._parse_repeat_mode(raw_current)
        next_map = {
            "None": ("List", 2),
            "List": ("Track", 1),
            "Track": ("None", 0),
        }
        next_label, next_int = next_map.get(current_label, ("List", 2))

        if MediaPlaybackAutoRepeatMode is not None:
            enum_map = {
                "None": MediaPlaybackAutoRepeatMode.NONE,
                "Track": MediaPlaybackAutoRepeatMode.TRACK,
                "List": MediaPlaybackAutoRepeatMode.LIST,
            }
            target_mode = enum_map[next_label]
        elif raw_current is not None:
            try:
                target_mode = type(raw_current)(next_int)
            except Exception:
                target_mode = next_int
        else:
            target_mode = next_int

        await session.try_change_auto_repeat_mode_async(target_mode)

    async def _execute_seek(self, position_seconds: float) -> None:
        if self._stop_requested:
            return

        session = self._session
        if session is None and self._manager is not None:
            self._rebind_current_session()
            session = self._session

        if session is None:
            return

        try:
            target_ticks = int(
                round((self._timeline_start_seconds + position_seconds) * 10_000_000)
            )
            await session.try_change_playback_position_async(target_ticks)
        except Exception:
            logger.debug("WinRT seek to %.2fs failed.", position_seconds, exc_info=True)
        finally:
            self._trigger_refresh(rebind_session=False)