import os
import sys
import time
import json
import queue
import logging
import threading
import functools
from collections import namedtuple

# Disable PIL threading to reduce CPU overhead
os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'

from datetime import datetime

logger = logging.getLogger(__name__)
from PIL import ImageFont, Image, ImageDraw
from output import create_output
from trains import (loadDeparturesForStation, loadDestinationsForDeparture,
                    loadDeparturesForStationRTT, loadDestinationsForDepartureRTT,
                    loadServicesForStationDescribrr, loadDestinationsForServiceDescribrr,
                    startLivePassListener, loadCorridors, loadCorridorDetail)
from luma.core.virtual import viewport, snapshot
from open import isRun

def loadConfig():
    with open('config.json', 'r') as jsonConfig:
        data = json.load(jsonConfig)
        return data

def makeFont(name, size):
    font_path = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            'fonts',
            name
        )
    )
    return ImageFont.truetype(font_path, size)


def _textsize(text, font):
    bbox = font.getbbox(text)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


# ---------------------------------------------------------------------------
# Responsive layout
# ---------------------------------------------------------------------------

NATIVE_W, NATIVE_H = 256, 64
BORDER = 1  # blank pixel margin on all four edges

LayoutParams = namedtuple(
    'LayoutParams',
    ['font_size', 'font_large_size', 'row_pitch', 'clock_height',
     'num_trains', 'wide_mode', 'extra_col_width', 'font_scale'],
)

_layout = None  # set in startup block


@functools.lru_cache(maxsize=None)
def _get_font(name, size):
    return makeFont(name, size)


def _parse_display_sizes():
    """Return list of (width, height) for this mode from DISPLAY_REGISTRY."""
    raw = os.environ.get("DISPLAY_REGISTRY", "").strip()
    mode_name = os.environ.get("MODE_NAME", "uk_tdd")
    if raw:
        try:
            registry = json.loads(raw)
            sizes, seen = [], set()
            for entry in registry:
                if any(m.get("mode_name") == mode_name for m in entry.get("modes", [])):
                    wh = (int(entry["width"]), int(entry["height"]))
                    if wh not in seen:
                        seen.add(wh)
                        sizes.append(wh)
            if sizes:
                return sizes
        except (json.JSONDecodeError, ValueError):
            pass
    return [(int(os.environ.get("DISPLAY_WIDTH", "256")),
             int(os.environ.get("DISPLAY_HEIGHT", "64")))]


def _compute_layout(width, height):
    """Determine responsive layout parameters for the given display dimensions."""
    w_scale = width  / NATIVE_W
    h_scale = height / NATIVE_H
    if abs(w_scale - h_scale) < 0.15:
        # Aspect ratio preserved → scale everything
        font_scale = h_scale
        wide_mode  = False
    elif h_scale > w_scale:
        # Proportionally taller → more rows, native font
        font_scale = 1.0
        wide_mode  = False
    else:
        # Proportionally wider → extra columns
        font_scale = h_scale
        wide_mode  = True
    font_size       = max(10, round(10 * font_scale))
    font_large_size = max(20, round(20 * font_scale))
    row_pitch       = max(12, round(12 * font_scale))
    clock_height    = max(14, round(14 * font_scale))
    num_trains      = max(1, int((height - clock_height) / row_pitch) - 1)
    extra_col_width = max(0, width - NATIVE_W) if wide_mode else 0
    return LayoutParams(
        font_size=font_size, font_large_size=font_large_size,
        row_pitch=row_pitch, clock_height=clock_height,
        num_trains=num_trains, wide_mode=wide_mode,
        extra_col_width=extra_col_width, font_scale=font_scale,
    )


def renderDestination(departure, font):
    departureTime = departure["aimed_departure_time"]
    destinationName = departure["destination_name"]

    def drawText(draw, width, height):
        train = f"{departureTime}  {destinationName}"
        draw.text((0, 0), text=train, font=font, fill="yellow")

    return drawText


def renderServiceStatus(departure):
    def drawText(draw, width, height):
        train = ""

        if departure.get('atd'):
            # Platform column shows "Dep HH:MM" — nothing needed here.
            train = ""
        elif departure.get('ata'):
            train = "Arrived"
        elif departure["status"] == "CANCELLED" or departure["status"] == "CANCELLED_CALL" or departure["status"] == "CANCELLED_PASS":
            train = "Cancelled"
        else:
            if isinstance(departure["expected_departure_time"], str):
                train = 'Exp '+departure["expected_departure_time"]

            if departure["aimed_departure_time"] == departure["expected_departure_time"]:
                train = "On time"

        w, h = _textsize(train, font)
        draw.text((width-w,0), text=train, font=font, fill="yellow")
    return drawText

def renderPlatform(departure):
    def drawText(draw, width, height):
        if departure["mode"] == "bus":
            draw.text((0, 0), text="BUS", font=font, fill="yellow")
        elif departure["mode"] == "pass":
            draw.text((0, 0), text="PASS", font=font, fill="yellow")
        else:
            atd = departure.get('atd')
            if atd:
                draw.text((0, 0), text="Dep " + atd[:5], font=font, fill="yellow")
            elif isinstance(departure["platform"], str) and departure["platform"]:
                draw.text((0, 0), text="Plat " + departure["platform"], font=font, fill="yellow")
    return drawText

def renderCallingAt(draw, width, height):
    stations = "Calling at:"
    draw.text((0, 0), text=stations, font=font, fill="yellow")


def renderStations(stations):
    def drawText(draw, width, height):
        global stationRenderCount, pauseCount

        if(len(stations) == stationRenderCount - 5):
            stationRenderCount = 0

        draw.text(
            (0, 0), text=stations[stationRenderCount:], width=width, font=font, fill="yellow")

        if font.getlength(stations) <= width:
            stationRenderCount = 0
            pauseCount = 0
        elif stationRenderCount == 0 and pauseCount < 8:
            pauseCount += 1
        else:
            pauseCount = 0
            stationRenderCount += 1

    return drawText

def renderStationsPixel(stations, scroll_speed):
    """scroll_speed: pixels per second (float)."""
    text_width = int(font.getlength(stations))
    _strip = [None]
    _start = [None]
    PAUSE_SECS = 0.4  # hold at position 0 before scrolling

    def drawText(draw, width, height):
        global stationRenderCount
        if text_width <= width:
            draw.text((0, 0), text=stations, font=font, fill="yellow")
            stationRenderCount = 0
            return
        if _strip[0] is None:
            s = Image.new("RGB", (text_width, height), "black")
            ImageDraw.Draw(s).text((0, 0), text=stations, font=font, fill="yellow")
            _strip[0] = s
        if _start[0] is None:
            _start[0] = time.perf_counter()
        elapsed = time.perf_counter() - _start[0]
        if elapsed < PAUSE_SECS:
            offset = 0
        else:
            offset = int((elapsed - PAUSE_SECS) * scroll_speed)
            if offset >= text_width + 30:
                _start[0] = time.perf_counter()
                offset = 0
        stationRenderCount = offset
        if offset < text_width:
            draw._image.paste(_strip[0].crop((offset, 0, min(offset + width, text_width), height)), (0, 0))
    return drawText


_clock_seconds_size = None  # cached once; ":00" width never changes

def renderTime(draw, width, height):
    global _clock_seconds_size
    if _clock_seconds_size is None:
        _clock_seconds_size = _textsize(":00", fontBoldTall)
    w2, h2 = _clock_seconds_size

    rawTime = datetime.now().time()
    hour, minute, second = str(rawTime).split('.')[0].split(':')

    w1, h1 = _textsize("{}:{}".format(hour, minute), fontBoldLarge)

    draw.text(((width - w1 - w2) / 2, 0), text="{}:{}".format(hour, minute),
              font=fontBoldLarge, fill="yellow")
    draw.text((((width - w1 - w2) / 2) + w1, round(5 * _layout.font_scale)), text=":{}".format(second),
              font=fontBoldTall, fill="yellow")


def renderWelcomeTo(xOffset):
    def drawText(draw, width, height):
        text = "Welcome to"
        draw.text((int(xOffset), 0), text=text, font=fontBold, fill="yellow")

    return drawText


def renderDepartureStation(departureStation, xOffset):
    def draw(draw, width, height):
        text = departureStation
        draw.text((int(xOffset), 0), text=text, font=fontBold, fill="yellow")

    return draw


def renderDots(draw, width, height):
    text = ".  .  ."
    draw.text((0, 0), text=text, font=fontBold, fill="yellow")


def loadData(apiConfig, journeyConfig):
    runHours = [int(x) for x in apiConfig['operatingHours'].split('-')]
    if isRun(runHours[0], runHours[1]) == False:
        return False, False, journeyConfig['outOfHoursName']

    departures, stationName = loadDeparturesForStation(
        journeyConfig, apiConfig["appId"], apiConfig["apiKey"])

    if len(departures) == 0:
        return False, False, stationName

    firstDepartureDestinations = loadDestinationsForDeparture(
        journeyConfig, departures[0]["service_timetable"]["id"])

    return departures, firstDepartureDestinations, stationName

def loadDataRTT(apiConfig, journeyConfig):
    runHours = [int(x) for x in apiConfig['operatingHours'].split('-')]
    if isRun(runHours[0], runHours[1]) == False:
        return False, False, journeyConfig['outOfHoursName']

    departures, stationName = loadDeparturesForStationRTT(
        journeyConfig, apiConfig["username"], apiConfig["password"])

    if len(departures) == 0:
        return False, False, journeyConfig['outOfHoursName']

    firstDepartureDestinations = loadDestinationsForDepartureRTT(
        journeyConfig, apiConfig["username"], apiConfig["password"], departures[0]["time_table_url"])    

    #return False, False, journeyConfig['outOfHoursName']
    return departures, firstDepartureDestinations, stationName

def loadDataDescribrr(apiConfig, journeyConfig):
    runHours = [int(x) for x in apiConfig['operatingHours'].split('-')]
    if isRun(runHours[0], runHours[1]) == False:
        return False, False, journeyConfig['outOfHoursName']

    departures, station_name = loadServicesForStationDescribrr(journeyConfig, apiConfig)

    if not departures:
        return False, False, station_name or journeyConfig['outOfHoursName']

    for dep in departures[:5]:
        try:
            calling_at, dest_name = loadDestinationsForServiceDescribrr(journeyConfig, apiConfig, dep['rid'])
            dep['destination_name'] = dest_name
            dep['_calling_at'] = calling_at
        except Exception:
            dep['destination_name'] = dep.get('destination_name', '')
            dep['_calling_at'] = []

    first_calling_at = departures[0].get('_calling_at', [])
    return departures, first_calling_at, station_name


livePassOffset = 0
livePassLaps = 0


def renderLivePassText(message):
    def drawText(draw, width, height):
        global livePassOffset, livePassLaps
        padded = message + "     "
        if livePassOffset >= len(padded):
            livePassOffset = 0
            livePassLaps += 1
        draw.text((0, 0), text=padded[livePassOffset:], font=fontBoldLarge, fill="yellow")
        livePassOffset += 1
    return drawText


def renderLivePassTextPixel(message, scroll_speed):
    """scroll_speed: pixels per second (float)."""
    padded = message + "     "
    text_width = int(fontBoldLarge.getlength(padded))
    _strip = [None]
    _start = [None]

    def drawText(draw, width, height):
        global livePassOffset, livePassLaps
        if _strip[0] is None:
            s = Image.new("RGB", (text_width, height), "black")
            ImageDraw.Draw(s).text((0, 0), text=padded, font=fontBoldLarge, fill="yellow")
            _strip[0] = s
        if _start[0] is None:
            _start[0] = time.perf_counter()
        total_px = int((time.perf_counter() - _start[0]) * scroll_speed)
        livePassLaps = total_px // text_width
        offset = total_px % text_width
        livePassOffset = offset
        strip = _strip[0]
        canvas = draw._image
        end = offset + width
        if end <= text_width:
            canvas.paste(strip.crop((offset, 0, end, height)), (0, 0))
        else:
            first = text_width - offset
            if first > 0:
                canvas.paste(strip.crop((offset, 0, text_width, height)), (0, 0))
            canvas.paste(strip.crop((0, 0, width - first, height)), (first, 0))
    return drawText


def _get_scroll_config(cfg_key, default_mode, default_interval):
    """Read scrolling config for a given key ('callingPoints' or 'livePass')."""
    cfg = config.get('scrolling', {}).get(cfg_key, {})
    mode = cfg.get('mode', default_mode)
    interval = float(cfg.get('interval', default_interval))
    pixels_per_step = int(cfg.get('pixelsPerStep', 1))
    return mode, interval, pixels_per_step


def _render_static_text(text):
    """Render non-scrolling text, clipped at snapshot width."""
    def drawText(draw, width, height):
        if text:
            draw.text((0, 0), text=text, font=font, fill="yellow")
    return drawText


def drawLivePassSignage(device, width, height, message):
    global livePassOffset, livePassLaps

    virtualViewport = viewport(device, width=width, height=height)

    scroll_h = _layout.font_large_size
    clock_h  = _layout.clock_height
    inner_w  = width - 2 * BORDER
    y_scroll = BORDER + max(0, (height - clock_h - scroll_h) // 2)

    lp_mode, lp_interval, lp_pps = _get_scroll_config('livePass', 'character', 0.05)
    lp_render = renderLivePassTextPixel(message, lp_pps / lp_interval) if lp_mode == 'pixel' else renderLivePassText(message)
    rowScroll = snapshot(inner_w, scroll_h, lp_render, interval=lp_interval)
    rowTime   = snapshot(inner_w, clock_h, renderTime, interval=1)

    if len(virtualViewport._hotspots) > 0:
        for hotspot, xy in virtualViewport._hotspots:
            virtualViewport.remove_hotspot(hotspot, xy)

    virtualViewport.add_hotspot(rowScroll, (BORDER, y_scroll))
    virtualViewport.add_hotspot(rowTime,   (BORDER, height - clock_h - BORDER))

    return virtualViewport


def drawSignageWithLivePass(device, width, height, data, message):
    global stationRenderCount, pauseCount

    virtualViewport = viewport(device, width=width, height=height)

    row_h    = _layout.font_size
    pitch    = _layout.row_pitch
    clock_h  = _layout.clock_height
    scroll_h = _layout.font_large_size

    status = "Exp 00:00"
    callingAt = "Calling at:"

    departures, firstDepartureDestinations, departureStation = data

    w, h = _textsize(callingAt, font)
    callingWidth = w
    width   = virtualViewport.width
    inner_w = width - 2 * BORDER

    w, h = _textsize(status, font)
    pw, ph = _textsize("At Plat 88", font)

    rowOneA = snapshot(inner_w - w - pw, row_h, renderDestination(departures[0], fontBold), interval=10)
    rowOneB = snapshot(w, row_h, renderServiceStatus(departures[0]), interval=1.3)
    rowOneC = snapshot(pw, row_h, renderPlatform(departures[0]), interval=10)
    cp_mode, cp_interval, cp_pps = _get_scroll_config('callingPoints', 'character', 0.1)
    lp_mode, lp_interval, lp_pps = _get_scroll_config('livePass', 'character', 0.05)
    stations_str = ", ".join(firstDepartureDestinations)
    cp_render = renderStationsPixel(stations_str, cp_pps / cp_interval) if cp_mode == 'pixel' else renderStations(stations_str)
    lp_render = renderLivePassTextPixel(message, lp_pps / lp_interval) if lp_mode == 'pixel' else renderLivePassText(message)
    rowTwoA     = snapshot(callingWidth, row_h, renderCallingAt, interval=100)
    rowTwoB     = snapshot(inner_w - callingWidth, row_h, cp_render, interval=cp_interval)
    rowLivePass = snapshot(inner_w, scroll_h, lp_render, interval=lp_interval)
    rowTime     = snapshot(inner_w, clock_h, renderTime, interval=1)

    if len(virtualViewport._hotspots) > 0:
        for hotspot, xy in virtualViewport._hotspots:
            virtualViewport.remove_hotspot(hotspot, xy)

    virtualViewport.add_hotspot(rowOneA,    (BORDER, BORDER))
    virtualViewport.add_hotspot(rowOneB,    (BORDER + inner_w - w, BORDER))
    virtualViewport.add_hotspot(rowOneC,    (BORDER + inner_w - w - pw, BORDER))
    virtualViewport.add_hotspot(rowTwoA,    (BORDER, pitch + BORDER))
    virtualViewport.add_hotspot(rowTwoB,    (BORDER + callingWidth, pitch + BORDER))
    virtualViewport.add_hotspot(rowLivePass,(BORDER, 2 * pitch + BORDER))
    virtualViewport.add_hotspot(rowTime,    (BORDER, height - clock_h - BORDER))

    return virtualViewport


def drawBlankSignage(device, width, height, departureStation):
    global stationRenderCount, pauseCount

    row_h   = _layout.font_size
    pitch   = _layout.row_pitch
    clock_h = _layout.clock_height
    inner_w = width - 2 * BORDER

    welcomeSize = _textsize("Welcome to", fontBold)
    stationSize = _textsize(departureStation, fontBold)

    virtualViewport = viewport(device, width=width, height=height)

    rowOne = snapshot(inner_w, row_h, renderWelcomeTo(
        (inner_w - welcomeSize[0]) / 2), interval=10)
    rowTwo = snapshot(inner_w, row_h, renderDepartureStation(
        departureStation, (inner_w - stationSize[0]) / 2), interval=10)
    rowThree = snapshot(inner_w, row_h, renderDots, interval=10)
    rowTime  = snapshot(inner_w, clock_h, renderTime, interval=1)

    if len(virtualViewport._hotspots) > 0:
        for hotspot, xy in virtualViewport._hotspots:
            virtualViewport.remove_hotspot(hotspot, xy)

    virtualViewport.add_hotspot(rowOne,   (BORDER, BORDER))
    virtualViewport.add_hotspot(rowTwo,   (BORDER, pitch + BORDER))
    virtualViewport.add_hotspot(rowThree, (BORDER, 2 * pitch + BORDER))
    virtualViewport.add_hotspot(rowTime,  (BORDER, height - clock_h - BORDER))

    return virtualViewport


def drawSignage(device, width, height, data):
    global stationRenderCount, pauseCount

    layout  = _layout
    row_h   = layout.font_size
    pitch   = layout.row_pitch
    clock_h = layout.clock_height

    virtualViewport = viewport(device, width=width, height=height)
    vp_width = virtualViewport.width
    inner_w  = vp_width - 2 * BORDER

    departures, firstDepartureDestinations, departureStation = data

    status_w, _ = _textsize("Exp 00:00", font)
    plat_w,   _ = _textsize("At Plat 88", font)

    num_trains = min(layout.num_trains, len(departures))

    if len(virtualViewport._hotspots) > 0:
        for hotspot, xy in virtualViewport._hotspots:
            virtualViewport.remove_hotspot(hotspot, xy)

    stationRenderCount = 0
    pauseCount = 0

    # Clock flush to bottom
    rowTime = snapshot(inner_w, clock_h, renderTime, interval=1)
    virtualViewport.add_hotspot(rowTime, (BORDER, height - clock_h - BORDER))

    if layout.wide_mode:
        # Wide mode: standard columns in first NATIVE_W px; calling-at inline
        # after that.  No dedicated calling-at row → equal pitch per train.
        native_dest_w = NATIVE_W - status_w - plat_w

        for i in range(num_trains):
            y        = i * pitch
            dep_font = fontBold if i == 0 else font

            rowA = snapshot(native_dest_w, row_h,
                            renderDestination(departures[i], dep_font), interval=10)
            rowC = snapshot(plat_w, row_h,
                            renderPlatform(departures[i]), interval=10)
            rowB = snapshot(status_w, row_h,
                            renderServiceStatus(departures[i]), interval=1.3)
            virtualViewport.add_hotspot(rowA, (BORDER, y + BORDER))
            virtualViewport.add_hotspot(rowC, (BORDER + native_dest_w, y + BORDER))
            virtualViewport.add_hotspot(rowB, (BORDER + native_dest_w + plat_w, y + BORDER))

            if layout.extra_col_width > 0:
                calling     = departures[i].get('_calling_at', [])
                inline_text = ("→ " + ", ".join(calling)) if calling else ""
                extra_w     = inner_w - NATIVE_W
                rowExtra    = snapshot(extra_w, row_h,
                                       _render_static_text(inline_text), interval=100)
                virtualViewport.add_hotspot(rowExtra, (BORDER + NATIVE_W, y + BORDER))
    else:
        # Standard / tall mode: calling-at row after train 0.
        # Train 0 at y=0, calling-at at y=pitch, trains 1..N-1 at y=(i+1)*pitch.
        calling_at_w, _ = _textsize("Calling at:", font)
        cp_mode, cp_interval, cp_pps = _get_scroll_config('callingPoints', 'character', 0.1)
        stations_str = ", ".join(firstDepartureDestinations)
        cp_render = (renderStationsPixel(stations_str, cp_pps / cp_interval)
                     if cp_mode == 'pixel' else renderStations(stations_str))
        rowTwoA = snapshot(calling_at_w, row_h, renderCallingAt, interval=100)
        rowTwoB = snapshot(inner_w - calling_at_w, row_h, cp_render, interval=cp_interval)
        virtualViewport.add_hotspot(rowTwoA, (BORDER, pitch + BORDER))
        virtualViewport.add_hotspot(rowTwoB, (BORDER + calling_at_w, pitch + BORDER))

        for i in range(num_trains):
            y        = 0 if i == 0 else (i + 1) * pitch
            dep_font = fontBold if i == 0 else font
            dest_w   = inner_w - status_w - plat_w

            rowA = snapshot(dest_w, row_h,
                            renderDestination(departures[i], dep_font), interval=10)
            rowC = snapshot(plat_w, row_h,
                            renderPlatform(departures[i]), interval=10)
            rowB = snapshot(status_w, row_h,
                            renderServiceStatus(departures[i]), interval=1.3)
            virtualViewport.add_hotspot(rowA, (BORDER, y + BORDER))
            virtualViewport.add_hotspot(rowC, (BORDER + dest_w, y + BORDER))
            virtualViewport.add_hotspot(rowB, (BORDER + dest_w + plat_w, y + BORDER))

    return virtualViewport


# ── UI state machine constants ────────────────────────────────────────────────
UI_NORMAL           = "NORMAL"
UI_CORRIDOR_SELECT  = "CORRIDOR_SELECT"
UI_STATION_SCROLL   = "STATION_SCROLL"
STATION_SCROLL_TIMEOUT = 2.0  # seconds of inactivity before auto-selecting station


# ── Persistent state helpers ──────────────────────────────────────────────────

def load_state() -> dict:
    """Load persisted UI state from state.json. Returns {} if absent or corrupt."""
    try:
        with open('state.json') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(corridor_code: str, tiploc: str) -> None:
    """Persist the selected corridor code and tiploc to state.json."""
    with open('state.json', 'w') as f:
        json.dump({'corridor_code': corridor_code, 'tiploc': tiploc}, f)


# ── Direct-PIL draw helpers (used outside luma viewport) ─────────────────────

def _draw_clock(draw, width, height):
    """Draw HH:MM:SS clock at y=50/55, matching renderTime's layout."""
    rawTime = datetime.now().time()
    hour, minute, second = str(rawTime).split('.')[0].split(':')
    time_str = "{}:{}".format(hour, minute)
    sec_str  = ":{}".format(second)
    w1, _ = _textsize(time_str, fontBoldLarge)
    w2, _ = _textsize(":00",    fontBoldTall)
    inner_w = width - 2 * BORDER
    x       = BORDER + (inner_w - w1 - w2) / 2
    y_clock = height - _layout.clock_height - BORDER
    y_sec   = y_clock + round(5 * _layout.font_scale)
    draw.text((x,      y_clock), text=time_str, font=fontBoldLarge, fill="yellow")
    draw.text((x + w1, y_sec),   text=sec_str,  font=fontBoldTall,  fill="yellow")


def drawThreeRowSelector(device, width, height, items, idx, header=None):
    """
    Render a 3-row prev/current/next selector directly to a PIL Image (RGB).

    Layout (10px font, 12px row pitch):
      y=0  "< {prev}"   font      dim yellow  (160, 120, 0)
      y=12 "> {current}" fontBold full yellow
      y=24 "  {next}"   font      dim yellow
      y=50  clock

    If header is given it is drawn at y=0 and all rows shift down 12px.
    Items wraps around; idx is taken mod len(items).
    Returns Image of size (width, height), mode "RGB".
    """
    img  = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    _draw_clock(draw, width, height)

    n = len(items)
    if n == 0:
        return img

    cur  = idx % n
    prev = (cur - 1) % n
    nxt  = (cur + 1) % n
    dim  = (160, 120, 0)

    pitch = _layout.row_pitch
    row_y = BORDER
    if header:
        draw.text((BORDER, row_y), text=header, font=fontBold, fill="yellow")
        row_y = BORDER + pitch

    draw.text((BORDER, row_y),          text="< {}".format(items[prev]), font=font,     fill=dim)
    draw.text((BORDER, row_y + pitch),  text="> {}".format(items[cur]),  font=fontBold, fill="yellow")
    draw.text((BORDER, row_y + 2*pitch),text="  {}".format(items[nxt]),  font=font,     fill=dim)
    return img


def drawLoadingFrame(device, width, height, message="Loading..."):
    """Black frame with centred message + clock. Use for transient status."""
    img  = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    w, _ = _textsize(message, fontBold)
    inner_w  = width - 2 * BORDER
    inner_h  = height - _layout.clock_height - 2 * BORDER
    y_msg    = BORDER + max(0, (inner_h - _layout.font_size) // 2)
    x_msg    = BORDER + (inner_w - w) // 2
    draw.text((x_msg, y_msg), text=message, font=fontBold, fill="yellow")
    _draw_clock(draw, width, height)
    return img


# ── Board reload helper ───────────────────────────────────────────────────────

def _reload_board_data():
    """Re-fetch departure data and rebuild the luma viewport. Updates globals."""
    global data, virtual, timeAtStart
    if config["apiMethod"] == 'describrr':
        data = loadDataDescribrr(config["describrr"], config["journey"])
    elif config["apiMethod"] == 'rtt':
        data = loadDataRTT(config["rttApi"], config["journey"])
    else:
        data = loadData(config["transportApi"], config["journey"])

    if data[0] == False:
        virtual = drawBlankSignage(device, width=widgetWidth, height=widgetHeight,
                                   departureStation=data[2])
    else:
        virtual = drawSignage(device, width=widgetWidth, height=widgetHeight, data=data)
    timeAtStart = time.time()


# ── Station selection (called on 2s scroll timeout) ──────────────────────────

def _select_current_station():
    """
    Select the currently highlighted station, persist, restart the live pass
    listener on the new tiploc, reload board data, and return to UI_NORMAL.
    """
    global ui_state, live_pass_stop_event

    if not corridor_stations:
        ui_state = UI_NORMAL
        return

    selected   = corridor_stations[station_idx % len(corridor_stations)]
    new_tiploc = selected['tiploc']
    logger.info("Station selected by timeout: %s (%s)", selected['name'], new_tiploc)

    config['describrr']['tiploc'] = new_tiploc
    save_state(corridor_code_selected or '', new_tiploc)
    device.display(drawLoadingFrame(device, widgetWidth, widgetHeight, "Updating..."))

    # Restart the live pass WebSocket listener on the new tiploc
    if config["apiMethod"] == 'describrr':
        live_pass_stop_event.set()
        live_pass_stop_event = startLivePassListener(
            config["journey"], config["describrr"], live_pass_queue, refresh_event)
        logger.info("Restarted live pass listener for tiploc=%s", new_tiploc)

    _reload_board_data()
    ui_state = UI_NORMAL


# ── Background board data refresh ──────────────────────────────────────────────

def _load_board_data_async():
    """Fetch departure data in a background thread.

    The periodic board refresh otherwise calls loadData*() synchronously from
    the main render loop, which can block for up to the request timeout (10s)
    if the upstream API is slow — freezing the display. Running it here and
    handing the result to the main loop via board_data_queue keeps the 30fps
    render loop unblocked regardless of API latency.
    """
    if config["apiMethod"] == 'describrr':
        result = loadDataDescribrr(config["describrr"], config["journey"])
    elif config["apiMethod"] == 'rtt':
        result = loadDataRTT(config["rttApi"], config["journey"])
    else:
        result = loadData(config["transportApi"], config["journey"])
    board_data_queue.put(result)
    _refresh_in_flight.clear()


# ── Background corridor preload ───────────────────────────────────────────────

def _background_load_corridor(code):
    """Load corridor stations in a background thread (used at startup for state restore)."""
    global corridor_stations, corridor_code_selected, station_idx
    stations = loadCorridorDetail(config['describrr'], code)
    if stations:
        corridor_stations     = stations
        corridor_code_selected = code
        station_idx           = 0
        logger.info("Background-loaded %d stations for corridor %s", len(stations), code)


# ── Encoder event dispatcher ──────────────────────────────────────────────────

def _handle_encoder_event(ev):
    """
    Dispatch a single encoder event dict from encoder_queue.
    Mutates global ui_state, corridor/station indices, and live pass state.
    """
    global ui_state, corridor_idx, station_idx, corridors, corridor_stations
    global corridor_code_selected, station_scroll_last_input
    global live_pass_active, livePassLaps, livePassOffset, virtual

    event = ev.get("event")

    # ── NORMAL ────────────────────────────────────────────────────────────
    if ui_state == UI_NORMAL:
        if event == "ENCODER_PUSH":
            if live_pass_active:
                live_pass_active = False
                livePassLaps     = 0
                livePassOffset   = 0
                _reload_board_data()
            device.display(drawLoadingFrame(device, widgetWidth, widgetHeight, "Loading..."))
            fetched = loadCorridors(config['describrr'])
            if not fetched:
                device.display(drawLoadingFrame(device, widgetWidth, widgetHeight, "No corridors"))
                return
            corridors    = fetched
            corridor_idx = 0
            ui_state     = UI_CORRIDOR_SELECT
            logger.info("Entered UI_CORRIDOR_SELECT (%d corridors)", len(corridors))

        elif event == "ENCODER_DELTA":
            if not corridor_stations:
                return  # no corridor loaded yet — ignore
            if live_pass_active:
                live_pass_active = False
                livePassLaps     = 0
                livePassOffset   = 0
                _reload_board_data()
            delta       = int(ev.get("delta", 0))
            station_idx = (station_idx + delta) % len(corridor_stations)
            station_scroll_last_input = time.time()
            ui_state    = UI_STATION_SCROLL
            logger.info("Entered UI_STATION_SCROLL at idx=%d", station_idx)

    # ── CORRIDOR_SELECT ───────────────────────────────────────────────────
    elif ui_state == UI_CORRIDOR_SELECT:
        if event == "ENCODER_DELTA":
            corridor_idx = (corridor_idx + int(ev.get("delta", 0))) % len(corridors)

        elif event == "ENCODER_PUSH":
            selected = corridors[corridor_idx]
            code     = selected['code']
            logger.info("Corridor selected: %s (%s)", selected['name'], code)
            device.display(drawLoadingFrame(device, widgetWidth, widgetHeight, "Loading..."))
            stations = loadCorridorDetail(config['describrr'], code)
            if not stations:
                device.display(drawLoadingFrame(device, widgetWidth, widgetHeight, "Load failed"))
                return
            corridor_stations      = stations
            corridor_code_selected = code
            station_idx            = 0
            save_state(code, config['describrr']['tiploc'])
            _reload_board_data()
            ui_state = UI_NORMAL
            logger.info("Returned to UI_NORMAL after corridor selection")

    # ── STATION_SCROLL ────────────────────────────────────────────────────
    elif ui_state == UI_STATION_SCROLL:
        if event == "ENCODER_DELTA":
            station_idx = (station_idx + int(ev.get("delta", 0))) % len(corridor_stations)
            station_scroll_last_input = time.time()


try:
    config = loadConfig()

    widgetWidth, widgetHeight = _parse_display_sizes()[0]
    _layout = _compute_layout(widgetWidth, widgetHeight)

    device = create_output()
    font = _get_font("Dot Matrix Regular.ttf", _layout.font_size)
    fontBold = _get_font("Dot Matrix Bold.ttf", _layout.font_size)
    fontBoldTall = _get_font("Dot Matrix Bold Tall.ttf", _layout.font_size)
    fontBoldLarge = _get_font("Dot Matrix Bold.ttf", _layout.font_large_size)

    stationRenderCount = 0
    pauseCount = 0
    loop_count = 0

    live_pass_queue  = queue.Queue()
    live_pass_active = False
    refresh_event    = threading.Event()

    board_data_queue   = queue.Queue()
    _refresh_in_flight = threading.Event()

    # ── Encoder queue (ZMQ only; None in SPI mode) ────────────────────────
    encoder_queue = getattr(device, 'encoder_queue', None)

    # ── UI state machine ──────────────────────────────────────────────────
    ui_state                  = UI_NORMAL
    corridor_idx              = 0
    station_idx               = 0
    corridors                 = []
    corridor_stations         = []
    corridor_code_selected    = None
    station_scroll_last_input = 0.0

    # ── Restore persisted state ───────────────────────────────────────────
    saved = load_state()
    if saved.get('tiploc') and config["apiMethod"] == 'describrr':
        config['describrr']['tiploc'] = saved['tiploc']
        logger.info("Restored tiploc from state.json: %s", saved['tiploc'])
    if saved.get('corridor_code') and config["apiMethod"] == 'describrr':
        corridor_code_selected = saved['corridor_code']
        threading.Thread(
            target=_background_load_corridor,
            args=(saved['corridor_code'],),
            daemon=True,
            name="corridor-preload",
        ).start()

    # ── Initial data load ─────────────────────────────────────────────────
    if config["apiMethod"] == 'describrr':
        data = loadDataDescribrr(config["describrr"], config["journey"])
        logger.info("Starting live pass WebSocket listener")
        live_pass_stop_event = startLivePassListener(
            config["journey"], config["describrr"], live_pass_queue, refresh_event)
    elif config["apiMethod"] == 'rtt':
        data = loadDataRTT(config["rttApi"], config["journey"])
        live_pass_stop_event = threading.Event()
    else:
        data = loadData(config["transportApi"], config["journey"])
        live_pass_stop_event = threading.Event()

    if data[0] == False:
        virtual = drawBlankSignage(
            device, width=widgetWidth, height=widgetHeight, departureStation=data[2])
    else:
        virtual = drawSignage(device, width=widgetWidth,
                              height=widgetHeight, data=data)

    timeAtStart = time.time()
    timeNow     = time.time()
    _fps_frames = 0
    _fps_last   = time.time()
    target_fps  = 30
    frame_time  = 1.0 / target_fps
    live_pass_laps = int(config.get('scrolling', {}).get('livePass', {}).get('laps', 2))

    while True:
        loop_start = time.time()

        # ── Drain encoder queue ───────────────────────────────────────────
        if encoder_queue is not None:
            while True:
                try:
                    _handle_encoder_event(encoder_queue.get_nowait())
                except queue.Empty:
                    break

        # ── Live pass queue (only while showing the normal board) ─────────
        if ui_state == UI_NORMAL and config["apiMethod"] == 'describrr':
            try:
                pass_data = live_pass_queue.get_nowait()
                livePassOffset = 0
                livePassLaps = 0
                live_pass_message = (
                    f"LIVE PASS  {pass_data['headcode']}  {pass_data['uid']}  "
                    f"{pass_data['origin']} to {pass_data['destination']}"
                )
                logger.info("Live pass display triggered: %s", live_pass_message)
                if data[0] == False:
                    virtual = drawLivePassSignage(
                        device, width=widgetWidth, height=widgetHeight, message=live_pass_message)
                else:
                    virtual = drawSignageWithLivePass(
                        device, width=widgetWidth, height=widgetHeight, data=data, message=live_pass_message)
                live_pass_active = True
            except queue.Empty:
                pass

        # ── Normal board state updates ────────────────────────────────────
        if ui_state == UI_NORMAL:
            if live_pass_active:
                if livePassLaps >= live_pass_laps:
                    logger.info("Live pass display complete, resuming normal board")
                    live_pass_active = False
                    if data[0] == False:
                        virtual = drawBlankSignage(
                            device, width=widgetWidth, height=widgetHeight, departureStation=data[2])
                    else:
                        virtual = drawSignage(device, width=widgetWidth,
                                              height=widgetHeight, data=data)
            else:
                ws_triggered = config["apiMethod"] == 'describrr' and refresh_event.is_set()
                if not _refresh_in_flight.is_set() and (
                        ws_triggered or timeNow - timeAtStart >= config["refreshTime"]):
                    if ws_triggered:
                        refresh_event.clear()
                        logger.info("WebSocket event triggered board refresh")
                    timeAtStart = time.time()
                    _refresh_in_flight.set()
                    threading.Thread(target=_load_board_data_async, daemon=True,
                                      name="board-refresh").start()

                try:
                    new_data = board_data_queue.get_nowait()
                except queue.Empty:
                    new_data = None
                if new_data is not None:
                    data = new_data
                    if data[0] == False:
                        virtual = drawBlankSignage(
                            device, width=widgetWidth, height=widgetHeight, departureStation=data[2])
                    else:
                        virtual = drawSignage(device, width=widgetWidth,
                                              height=widgetHeight, data=data)

        # ── Station scroll 2s timeout → auto-select ───────────────────────
        if ui_state == UI_STATION_SCROLL:
            if time.time() - station_scroll_last_input >= STATION_SCROLL_TIMEOUT:
                _select_current_station()

        # ── Render ────────────────────────────────────────────────────────
        timeNow = time.time()
        if ui_state == UI_NORMAL:
            virtual.refresh()
        elif ui_state == UI_CORRIDOR_SELECT:
            labels = ["{} ({})".format(c['name'], c['train_count']) for c in corridors]
            device.display(drawThreeRowSelector(device, widgetWidth, widgetHeight,
                                                labels, corridor_idx, header="Select corridor"))
        elif ui_state == UI_STATION_SCROLL:
            labels = [s['name'] for s in corridor_stations]
            device.display(drawThreeRowSelector(device, widgetWidth, widgetHeight,
                                                labels, station_idx))

        # ── FPS accounting + frame-rate cap ───────────────────────────────
        _fps_frames += 1
        if timeNow - _fps_last >= 10:
            logger.info("Display refresh rate: %.1f fps", _fps_frames / (timeNow - _fps_last))
            _fps_frames = 0
            _fps_last = timeNow

        elapsed    = time.time() - loop_start
        sleep_time = frame_time - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)

except KeyboardInterrupt:
    pass
except ValueError as err:
    print(f"Error: {err}")
except KeyError as err:
    print(f"Error: Please ensure the {err} environment variable is set")
