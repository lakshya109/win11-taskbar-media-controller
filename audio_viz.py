"""Real-time Windows WASAPI loopback FFT audio analyzer.

Captures system output audio via PyAudioWPatch in callback mode so silent or
paused audio never blocks shutdown, computes 12 logarithmic frequency bands
with numpy.fft.rfft at ~30 FPS, and emits normalized [0.0, 1.0] bar heights.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

try:
    import pyaudiowpatch as pyaudio

    PYAUDIO_AVAILABLE = True
except ImportError:  # pragma: no cover - non-Windows or missing PyAudioWPatch
    pyaudio = None  # type: ignore[assignment]
    PYAUDIO_AVAILABLE = False

logger = logging.getLogger(__name__)

NUM_BANDS = 12
TARGET_FPS = 30.0
FRAME_INTERVAL = 1.0 / TARGET_FPS
IDLE_LEVEL = 0.10
FFT_SIZE = 2048
SILENCE_TIMEOUT_SECONDS = 0.20
DEVICE_RETRY_INTERVAL_SECONDS = 1.50


class AudioVisualizerWorker(QThread):
    """Background QThread performing 12-band logarithmic FFT on WASAPI loopback.

    Signals:
        levels_updated(list[float]): Emits 12 normalized floats in [0.0, 1.0].
    """

    levels_updated = pyqtSignal(list)

    def __init__(self, num_bands: int = NUM_BANDS, parent=None) -> None:
        super().__init__(parent)
        self._num_bands = num_bands
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._buffer_lock = threading.Lock()

        # Playback activity gate driven by MediaBackend.
        self._is_active = False

        # Ring buffer written by PyAudio callback, read by the 30 FPS FFT loop.
        self._ring_buffer = np.zeros(FFT_SIZE, dtype=np.float32)
        self._last_packet_time = 0.0
        self._sample_rate = 48000
        self._channels = 2

        # Smoothing and adaptive gain state.
        self._smoothed = np.full(self._num_bands, IDLE_LEVEL, dtype=np.float32)
        self._running_peak = 0.25
        self._window = np.hanning(FFT_SIZE).astype(np.float32)
        self._band_slices: list[tuple[int, int]] = []

    # ------------------------------------------------------------------
    # Public thread-safe API
    # ------------------------------------------------------------------

    def set_active(self, active: bool) -> None:
        """Enable or pause active FFT visualization (falls back to idle bars)."""
        with self._state_lock:
            self._is_active = bool(active)

    def stop(self, timeout_ms: int = 2500) -> None:
        """Signal the audio thread to close PyAudio cleanly and exit."""
        self._stop_event.set()
        if self.isRunning():
            finished = self.wait(timeout_ms)
            if not finished:
                logger.warning(
                    "AudioVisualizerWorker did not exit within %d ms.",
                    timeout_ms,
                )

    # ------------------------------------------------------------------
    # QThread lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main worker loop managing PyAudio lifecycle and 30 FPS FFT ticks."""
        self._stop_event.clear()
        self._emit_idle_immediately()

        if not PYAUDIO_AVAILABLE:
            logger.warning(
                "PyAudioWPatch is unavailable; keeping flat idle visualizer bars."
            )
            self._run_idle_only_loop()
            return

        while not self._stop_event.is_set():
            pa = None
            stream = None
            try:
                pa = pyaudio.PyAudio()
                loopback = self._find_default_loopback_device(pa)
                if loopback is None:
                    self._close_audio(stream, pa)
                    pa = None
                    self._decay_to_idle_step()
                    self._stop_event.wait(DEVICE_RETRY_INTERVAL_SECONDS)
                    continue

                self._sample_rate = max(8000, int(loopback["defaultSampleRate"]))
                self._channels = max(1, int(loopback["maxInputChannels"]))
                self._band_slices = self._build_log_band_slices(
                    self._sample_rate, FFT_SIZE, self._num_bands
                )

                with self._buffer_lock:
                    self._ring_buffer.fill(0.0)
                    self._last_packet_time = 0.0

                frames_per_buffer = max(256, int(self._sample_rate / TARGET_FPS))
                stream = pa.open(
                    format=pyaudio.paFloat32,
                    channels=self._channels,
                    rate=self._sample_rate,
                    input=True,
                    input_device_index=int(loopback["index"]),
                    frames_per_buffer=frames_per_buffer,
                    stream_callback=self._audio_callback,
                )
                stream.start_stream()
                self._process_loop(stream)
            except Exception:
                logger.debug(
                    "WASAPI loopback stream interrupted; falling back to idle.",
                    exc_info=True,
                )
            finally:
                self._close_audio(stream, pa)
                stream = None
                pa = None

            if not self._stop_event.is_set():
                self._decay_to_idle_step()
                self._stop_event.wait(DEVICE_RETRY_INTERVAL_SECONDS)

    def _run_idle_only_loop(self) -> None:
        """Fallback loop when PyAudioWPatch is not installed."""
        while not self._stop_event.is_set():
            self._stop_event.wait(0.25)

    def _process_loop(self, stream) -> None:
        """Sample the audio ring buffer at ~30 FPS and emit normalized bands."""
        idle_emitted = False

        while not self._stop_event.is_set():
            tick_start = time.monotonic()

            try:
                if not stream.is_active():
                    break
            except Exception:
                break

            with self._state_lock:
                is_active = self._is_active

            if not is_active:
                settled = self._decay_to_idle_step()
                if not settled or not idle_emitted:
                    self.levels_updated.emit(self._smoothed.tolist())
                    idle_emitted = settled
            else:
                idle_emitted = False
                levels = self._compute_frame_levels(tick_start)
                self.levels_updated.emit(levels)

            elapsed = time.monotonic() - tick_start
            sleep_time = max(0.002, FRAME_INTERVAL - elapsed)
            self._stop_event.wait(sleep_time)

    # ------------------------------------------------------------------
    # PyAudio callback & device discovery
    # ------------------------------------------------------------------

    def _audio_callback(self, in_data, frame_count, _time_info, _status):
        """Non-blocking PortAudio callback copying mono PCM into ring buffer."""
        if self._stop_event.is_set():
            return (None, pyaudio.paComplete)

        try:
            samples = np.frombuffer(in_data, dtype=np.float32)
            if samples.size == 0:
                return (None, pyaudio.paContinue)

            if self._channels > 1:
                usable = (samples.size // self._channels) * self._channels
                if usable == 0:
                    return (None, pyaudio.paContinue)
                mono = samples[:usable].reshape(-1, self._channels).mean(axis=1)
            else:
                mono = samples

            n = mono.size
            with self._buffer_lock:
                if n >= FFT_SIZE:
                    self._ring_buffer[:] = mono[-FFT_SIZE:]
                else:
                    self._ring_buffer[:-n] = self._ring_buffer[n:]
                    self._ring_buffer[-n:] = mono
                self._last_packet_time = time.monotonic()
        except Exception:
            pass

        return (None, pyaudio.paContinue)

    @staticmethod
    def _find_default_loopback_device(pa) -> Optional[dict]:
        """Locate the WASAPI loopback device matching the default speakers."""
        try:
            wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        except Exception:
            return None

        default_out_idx = wasapi_info.get("defaultOutputDevice", -1)
        if default_out_idx is None or default_out_idx < 0:
            return None

        try:
            default_speakers = pa.get_device_info_by_index(default_out_idx)
        except Exception:
            return None

        if default_speakers.get("isLoopbackDevice", False):
            return default_speakers

        target_name = str(default_speakers.get("name", ""))
        fallback_device = None
        try:
            for loopback in pa.get_loopback_device_info_generator():
                if target_name and target_name in str(loopback.get("name", "")):
                    return loopback
                if fallback_device is None:
                    fallback_device = loopback
        except Exception:
            return None

        return fallback_device

    @staticmethod
    def _close_audio(stream, pa) -> None:
        """Stop and close PyAudio stream and instance without raising errors."""
        if stream is not None:
            try:
                if stream.is_active():
                    stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # FFT & logarithmic band calculation
    # ------------------------------------------------------------------

    @staticmethod
    def _build_log_band_slices(
        sample_rate: int, fft_size: int, num_bands: int
    ) -> list[tuple[int, int]]:
        """Precompute FFT bin ranges for logarithmically spaced frequency bands."""
        freqs = np.fft.rfftfreq(fft_size, d=1.0 / sample_rate)
        min_freq = 50.0
        max_freq = min(14000.0, sample_rate * 0.45)
        edges = np.geomspace(min_freq, max_freq, num_bands + 1)

        slices: list[tuple[int, int]] = []
        max_bin = len(freqs) - 1
        prev_end = 1

        for idx in range(num_bands):
            start = int(np.searchsorted(freqs, edges[idx], side="left"))
            end = int(np.searchsorted(freqs, edges[idx + 1], side="right"))
            start = max(1, min(start, max_bin))
            if idx > 0 and start < prev_end:
                start = prev_end
            end = max(start + 1, min(end, max_bin + 1))
            if start >= max_bin:
                start = max_bin
                end = max_bin + 1
            slices.append((start, end))
            prev_end = end

        return slices

    def _compute_frame_levels(self, now: float) -> list[float]:
        """Run Hann-windowed rfft across 12 log bands and smooth into [0.0, 1.0]."""
        with self._buffer_lock:
            last_packet = self._last_packet_time
            if now - last_packet > SILENCE_TIMEOUT_SECONDS:
                self._decay_to_idle_step()
                return self._smoothed.tolist()
            frame = self._ring_buffer.copy()

        # Remove any DC offset before applying the Hann window.
        frame -= float(np.mean(frame))
        rms = float(np.sqrt(np.mean(frame * frame)))
        if rms < 1e-5:
            self._decay_to_idle_step()
            return self._smoothed.tolist()

        spectrum = np.abs(np.fft.rfft(frame * self._window)) / (FFT_SIZE * 0.5)
        raw_bands = np.zeros(self._num_bands, dtype=np.float32)

        for i, (start, end) in enumerate(self._band_slices):
            segment = spectrum[start:end]
            if segment.size == 0:
                continue
            # Blend RMS and peak energy so beats pop while treble remains lively.
            band_energy = 0.65 * float(np.sqrt(np.mean(segment * segment))) + (
                0.35 * float(np.max(segment))
            )
            # Gentle perceptual treble tilt so higher log bands stay balanced.
            tilt = 1.0 + (i / max(1, self._num_bands - 1)) * 1.4
            raw_bands[i] = float(np.log1p(band_energy * tilt * 24.0))

        frame_peak = float(np.max(raw_bands))
        if frame_peak > self._running_peak:
            self._running_peak = 0.70 * self._running_peak + 0.30 * frame_peak
        else:
            self._running_peak = max(0.18, self._running_peak * 0.985)

        normalized = np.clip(raw_bands / max(self._running_peak, 1e-4), 0.0, 1.0)
        # Keep subtle visual presence floor while playing.
        target = np.clip(IDLE_LEVEL + normalized * (1.0 - IDLE_LEVEL), 0.0, 1.0)

        # Snappy rise, smooth fall for liquid bar animation.
        for i in range(self._num_bands):
            curr = float(self._smoothed[i])
            nxt = float(target[i])
            alpha = 0.58 if nxt > curr else 0.24
            self._smoothed[i] = curr + (nxt - curr) * alpha

        return np.clip(self._smoothed, 0.0, 1.0).tolist()

    def _decay_to_idle_step(self) -> bool:
        """Smoothly relax all bars toward IDLE_LEVEL; return True when settled."""
        diff = IDLE_LEVEL - self._smoothed
        self._smoothed += diff * 0.30
        if float(np.max(np.abs(IDLE_LEVEL - self._smoothed))) < 0.004:
            self._smoothed.fill(IDLE_LEVEL)
            return True
        return False

    def _emit_idle_immediately(self) -> None:
        """Emit an immediate flat idle baseline at startup."""
        self._smoothed.fill(IDLE_LEVEL)
        self.levels_updated.emit(self._smoothed.tolist())