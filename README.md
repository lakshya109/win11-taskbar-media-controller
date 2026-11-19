# Win11 Taskbar Media Controller

A lightweight, borderless Windows 11 taskbar media controller built with Python, PyQt6, and WinRT GSMTC. Blends seamlessly onto the native taskbar while offering a real-time 12-band audio visualizer, interactive timeline scrubbing, and a frosted-glass hover card.

![Demo](demo.png)

## Features
- **Seamless Borderless Overlay:** Strips native Windows 11 DWM borders (`DWMWA_BORDER_COLOR`) and uses 1/255 alpha hit-testing so controls float directly on the taskbar without a background box.
- **Universal Media Control (WinRT GSMTC):** Displays live track title, artist, album artwork, source app badge, and playback controls for Spotify, Chrome, Edge, Windows Media Player, and more.
- **Interactive Taskbar Progress Bar:** Slim `2.2px` warm-amber (`#f4b860`) progress bar along the bottom edge with `250ms` smooth interpolation, hover timestamp tooltips (`M:SS / M:SS`), and click-or-drag scrubbing.
- **Real-Time 12-Band Visualizer:** Captures system loopback audio via WASAPI (`PyAudioWPatch`) and renders smooth FFT frequency bars.
- **Frosted Glass Hover Card:** Persistent pop-up card above the taskbar featuring album artwork, source app badge, Shuffle & Repeat toggles, and transport controls.
- **Smart State & Fullscreen Handling:** Collapses into a compact music note icon when idle, hides automatically during fullscreen videos or games, maintains Z-order above the taskbar without stealing focus, and persists custom horizontal positioning (`config.json`).

## Project Structure
- `main.py` — PyQt6 taskbar overlay window, interactive progress bar, fullscreen/Z-order manager, and drag-to-reposition logic.
- `media_backend.py` — Background `QThread` running an isolated `asyncio` event loop for WinRT Global System Media Transport Controls (GSMTC).
- `audio_viz.py` — Background WASAPI loopback audio capture and FFT spectrum analyzer.
- `popup_card.py` — Frosted-glass floating hover card and custom `QPainter` vector icons.
- `launch_silent.vbs` — Silent background launcher for Windows Startup (`shell:startup`).

## Installation & Usage
1. **Clone the repository:**
```bash
git clone https://github.com/lakshya109/win11-taskbar-media-controller.git
cd win11-taskbar-media-controller
```
2. **Install dependencies:**
```bash
pip install -r requirements.txt
```
3. **Run the widget:**
```bash
python main.py
```
- **Move widget:** Right-click the widget → **Unlock Position**, drag horizontally along the taskbar, then right-click → **Lock Position** to save.
- **Auto-start on boot (optional):** Drop a shortcut to `launch_silent.vbs` into `Win + R` → `shell:startup`.