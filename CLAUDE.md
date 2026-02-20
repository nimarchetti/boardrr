# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Display

The app must be run from the repo root (not from `src/`) because `config.json` is loaded with a relative path.

**On hardware (SSD1322 over SPI):**
```bash
./run.sh
# equivalent to:
.venv/bin/python3 ./src/main.py --display ssd1322 --width 256 --height 64 --interface spi --mode 1 --rotate 2
```

**With the pygame emulator (desktop development):**
```bash
.venv/bin/python3 ./src/main.py --display pygame --width 256 --height 64
```

**Install dependencies into the venv:**
```bash
.venv/bin/pip install -r requirements.txt
```

There are no tests.

## Configuration

Copy `config.sample.json` to `config.json`. The active data source is set by `"apiMethod"`: `"transport"`, `"rtt"`, or `"describrr"`.

## Architecture

The app is a single-threaded rendering loop (plus an optional background WebSocket thread) built on luma.core's `viewport`/`snapshot` model.

### Source files

| File | Role |
|---|---|
| `src/main.py` | Entry point. Loads config, initialises the luma device, runs the main loop, and owns all rendering logic (`drawSignage`, `drawBlankSignage`, `drawLivePassSignage`). |
| `src/trains.py` | All data-fetching logic. Three separate families of functions for Transport API, RTT, and Describrr. Also contains `startLivePassListener` which spawns a daemon thread that connects to the Describrr WebSocket feed and pushes pass events onto a `queue.Queue`. |
| `src/helpers.py` | Thin wrapper around `luma.core.cmdline` that parses CLI arguments and creates the luma device. |
| `src/open.py` | Single helper `isRun(start_hour, end_hour)` used to gate API calls to operating hours. |

### Data flow

1. On startup and every `refreshTime` seconds, one of `loadData` / `loadDataRTT` / `loadDataDescribrr` is called. Each returns `(departures, calling_at_for_first_service, station_name)`.
2. If `departures` is falsy (outside hours or no services), `drawBlankSignage` is used; otherwise `drawSignage`.
3. `drawSignage` lays out up to three departure rows plus a scrolling "Calling at:" row for the first service, all as luma `snapshot` hotspots on a `viewport`.
4. When `apiMethod == "describrr"`, `startLivePassListener` runs in a daemon thread. The main loop checks the queue each iteration; a PASS event triggers `drawLivePassSignage` which scrolls the message twice before returning to the normal board.

### Normalised departure dict

All three data sources produce the same dict shape consumed by `main.py`:

```python
{
    'rid': str,                   # service identifier
    'destination_name': str,
    'aimed_departure_time': str,  # "HH:MM"
    'expected_departure_time': str,
    'status': str,                # e.g. "CANCELLED", "ON TIME", "scheduled"
    'mode': str,                  # "train", "bus", or "pass"
    'platform': str,
}
```

### Key dependencies

- **luma.core / luma.oled / luma.emulator** — display abstraction and rendering primitives
- **rpi-lgpio** — replaces RPi.GPIO for Pi 5 / Pi 500 (BCM2712) compatibility
- **websocket-client** — optional; used only for the Describrr live pass WebSocket feed
- **Pillow ≥ 10** — text sizing uses `font.getbbox()` (not the removed `draw.textsize()`)
