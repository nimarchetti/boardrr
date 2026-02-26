import os
import sys
import time
import json
import queue
import logging
import threading

from datetime import datetime

logger = logging.getLogger(__name__)
from PIL import ImageFont, Image
from helpers import get_device
from trains import (loadDeparturesForStation, loadDestinationsForDeparture,
                    loadDeparturesForStationRTT, loadDestinationsForDepartureRTT,
                    loadServicesForStationDescribrr, loadDestinationsForServiceDescribrr,
                    startLivePassListener)
from luma.core.render import canvas
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

def renderStationsPixel(stations, pixels_per_step):
    def drawText(draw, width, height):
        global stationRenderCount, pauseCount
        text_width = int(font.getlength(stations))
        if text_width <= width:
            draw.text((0, 0), text=stations, font=font, fill="yellow")
            stationRenderCount = 0
            pauseCount = 0
            return
        draw.text((-stationRenderCount, 0), text=stations, font=font, fill="yellow")
        if stationRenderCount == 0 and pauseCount < 8:
            pauseCount += 1
        else:
            pauseCount = 0
            stationRenderCount += pixels_per_step
            if stationRenderCount >= text_width + 30:
                stationRenderCount = 0
    return drawText


def renderTime(draw, width, height):
    rawTime = datetime.now().time()
    hour, minute, second = str(rawTime).split('.')[0].split(':')

    w1, h1 = _textsize("{}:{}".format(hour, minute), fontBoldLarge)
    w2, h2 = _textsize(":00", fontBoldTall)

    draw.text(((width - w1 - w2) / 2, 0), text="{}:{}".format(hour, minute),
              font=fontBoldLarge, fill="yellow")
    draw.text((((width - w1 -w2) / 2) + w1, 5), text=":{}".format(second),
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


def renderLivePassTextPixel(message, pixels_per_step):
    def drawText(draw, width, height):
        global livePassOffset, livePassLaps
        padded = message + "     "
        text_width = int(fontBoldLarge.getlength(padded))
        if livePassOffset >= text_width:
            livePassOffset = 0
            livePassLaps += 1
        draw.text((-livePassOffset, 0), text=padded, font=fontBoldLarge, fill="yellow")
        livePassOffset += pixels_per_step
    return drawText


def _get_scroll_config(cfg_key, default_mode, default_interval):
    """Read scrolling config for a given key ('callingPoints' or 'livePass')."""
    cfg = config.get('scrolling', {}).get(cfg_key, {})
    mode = cfg.get('mode', default_mode)
    interval = float(cfg.get('interval', default_interval))
    pixels_per_step = int(cfg.get('pixelsPerStep', 1))
    return mode, interval, pixels_per_step


def drawLivePassSignage(device, width, height, message):
    global livePassOffset, livePassLaps

    virtualViewport = viewport(device, width=width, height=height)

    lp_mode, lp_interval, lp_pps = _get_scroll_config('livePass', 'character', 0.05)
    lp_render = renderLivePassTextPixel(message, lp_pps) if lp_mode == 'pixel' else renderLivePassText(message)
    rowScroll = snapshot(width, 20, lp_render, interval=lp_interval)
    rowTime = snapshot(width, 14, renderTime, interval=1)

    if len(virtualViewport._hotspots) > 0:
        for hotspot, xy in virtualViewport._hotspots:
            virtualViewport.remove_hotspot(hotspot, xy)

    virtualViewport.add_hotspot(rowScroll, (0, 15))
    virtualViewport.add_hotspot(rowTime, (0, 50))

    return virtualViewport


def drawSignageWithLivePass(device, width, height, data, message):
    global stationRenderCount, pauseCount

    virtualViewport = viewport(device, width=width, height=height)

    status = "Exp 00:00"
    callingAt = "Calling at:"

    departures, firstDepartureDestinations, departureStation = data

    w, h = _textsize(callingAt, font)
    callingWidth = w
    width = virtualViewport.width

    w, h = _textsize(status, font)
    pw, ph = _textsize("At Plat 88", font)

    rowOneA = snapshot(width - w - pw, 10, renderDestination(departures[0], fontBold), interval=10)
    rowOneB = snapshot(w, 10, renderServiceStatus(departures[0]), interval=1)
    rowOneC = snapshot(pw, 10, renderPlatform(departures[0]), interval=10)
    cp_mode, cp_interval, cp_pps = _get_scroll_config('callingPoints', 'character', 0.1)
    lp_mode, lp_interval, lp_pps = _get_scroll_config('livePass', 'character', 0.05)
    stations_str = ", ".join(firstDepartureDestinations)
    cp_render = renderStationsPixel(stations_str, cp_pps) if cp_mode == 'pixel' else renderStations(stations_str)
    lp_render = renderLivePassTextPixel(message, lp_pps) if lp_mode == 'pixel' else renderLivePassText(message)
    rowTwoA = snapshot(callingWidth, 10, renderCallingAt, interval=100)
    rowTwoB = snapshot(width - callingWidth, 10, cp_render, interval=cp_interval)
    rowLivePass = snapshot(width, 20, lp_render, interval=lp_interval)
    rowTime = snapshot(width, 14, renderTime, interval=1)

    if len(virtualViewport._hotspots) > 0:
        for hotspot, xy in virtualViewport._hotspots:
            virtualViewport.remove_hotspot(hotspot, xy)

    virtualViewport.add_hotspot(rowOneA, (0, 0))
    virtualViewport.add_hotspot(rowOneB, (width - w, 0))
    virtualViewport.add_hotspot(rowOneC, (width - w - pw, 0))
    virtualViewport.add_hotspot(rowTwoA, (0, 12))
    virtualViewport.add_hotspot(rowTwoB, (callingWidth, 12))
    virtualViewport.add_hotspot(rowLivePass, (0, 27))
    virtualViewport.add_hotspot(rowTime, (0, 50))

    return virtualViewport


def drawBlankSignage(device, width, height, departureStation):
    global stationRenderCount, pauseCount

    welcomeSize = _textsize("Welcome to", fontBold)
    stationSize = _textsize(departureStation, fontBold)

    virtualViewport = viewport(device, width=width, height=height)

    rowOne = snapshot(width, 10, renderWelcomeTo(
        (width - welcomeSize[0]) / 2), interval=10)
    rowTwo = snapshot(width, 10, renderDepartureStation(
        departureStation, (width - stationSize[0]) / 2), interval=10)
    rowThree = snapshot(width, 10, renderDots, interval=10)
    rowTime = snapshot(width, 14, renderTime, interval=1)

    if len(virtualViewport._hotspots) > 0:
        for hotspot, xy in virtualViewport._hotspots:
            virtualViewport.remove_hotspot(hotspot, xy)

    virtualViewport.add_hotspot(rowOne, (0, 0))
    virtualViewport.add_hotspot(rowTwo, (0, 12))
    virtualViewport.add_hotspot(rowThree, (0, 24))
    virtualViewport.add_hotspot(rowTime, (0, 50))

    return virtualViewport


def drawSignage(device, width, height, data):
    global stationRenderCount, pauseCount

    virtualViewport = viewport(device, width=width, height=height)

    status = "Exp 00:00"
    callingAt = "Calling at:"

    departures, firstDepartureDestinations, departureStation = data

    w, h = _textsize(callingAt, font)
    callingWidth = w
    width = virtualViewport.width

    w, h = _textsize(status, font)
    pw, ph = _textsize("At Plat 88", font)

    rowOneA = snapshot(
        width - w - pw, 10, renderDestination(departures[0], fontBold), interval=10)
    rowOneB = snapshot(w, 10, renderServiceStatus(
        departures[0]), interval=1)
    rowOneC = snapshot(pw, 10, renderPlatform(departures[0]), interval=10)
    cp_mode, cp_interval, cp_pps = _get_scroll_config('callingPoints', 'character', 0.1)
    stations_str = ", ".join(firstDepartureDestinations)
    cp_render = renderStationsPixel(stations_str, cp_pps) if cp_mode == 'pixel' else renderStations(stations_str)
    rowTwoA = snapshot(callingWidth, 10, renderCallingAt, interval=100)
    rowTwoB = snapshot(width - callingWidth, 10, cp_render, interval=cp_interval)
    if(len(departures) > 1):
        rowThreeA = snapshot(width - w - pw, 10, renderDestination(
            departures[1],font), interval=10)
        rowThreeB = snapshot(w, 10, renderServiceStatus(
            departures[1]), interval=1)
        rowThreeC = snapshot(pw, 10, renderPlatform(departures[1]), interval=10)

    if(len(departures) > 2):
        rowFourA = snapshot(width - w - pw, 10, renderDestination(
            departures[2],font), interval=10)
        rowFourB = snapshot(w, 10, renderServiceStatus(
            departures[2]), interval=1)
        rowFourC = snapshot(pw, 10, renderPlatform(departures[2]), interval=10)

    rowTime = snapshot(width, 14, renderTime, interval=1)

    if len(virtualViewport._hotspots) > 0:
        for hotspot, xy in virtualViewport._hotspots:
            virtualViewport.remove_hotspot(hotspot, xy)

    stationRenderCount = 0
    pauseCount = 0

    virtualViewport.add_hotspot(rowOneA, (0, 0))
    virtualViewport.add_hotspot(rowOneB, (width - w, 0))
    virtualViewport.add_hotspot(rowOneC, (width - w - pw, 0))
    virtualViewport.add_hotspot(rowTwoA, (0, 12))
    virtualViewport.add_hotspot(rowTwoB, (callingWidth, 12))
    if(len(departures) > 1):
        virtualViewport.add_hotspot(rowThreeA, (0, 24))
        virtualViewport.add_hotspot(rowThreeB, (width - w, 24))
        virtualViewport.add_hotspot(rowThreeC, (width - w - pw, 24))
    if(len(departures) > 2):
        virtualViewport.add_hotspot(rowFourA, (0, 36))
        virtualViewport.add_hotspot(rowFourB, (width - w, 36))
        virtualViewport.add_hotspot(rowFourC, (width - w - pw, 36))
    virtualViewport.add_hotspot(rowTime, (0, 50))

    return virtualViewport


try:
    config = loadConfig()

    device = get_device()
    font = makeFont("Dot Matrix Regular.ttf", 10)
    fontBold = makeFont("Dot Matrix Bold.ttf", 10)
    fontBoldTall = makeFont("Dot Matrix Bold Tall.ttf", 10)
    fontBoldLarge = makeFont("Dot Matrix Bold.ttf", 20)

    widgetWidth = 256
    widgetHeight = 64

    stationRenderCount = 0
    pauseCount = 0
    loop_count = 0

    live_pass_queue = queue.Queue()
    live_pass_active = False
    refresh_event = threading.Event()

    if config["apiMethod"] == 'describrr':
        data = loadDataDescribrr(config["describrr"], config["journey"])
        logger.info("Starting live pass WebSocket listener")
        startLivePassListener(config["journey"], config["describrr"], live_pass_queue, refresh_event)
    elif config["apiMethod"] == 'rtt':
        data = loadDataRTT(config["rttApi"], config["journey"])
    else:
        data = loadData(config["transportApi"], config["journey"])

    if data[0] == False:
        virtual = drawBlankSignage(
            device, width=widgetWidth, height=widgetHeight, departureStation=data[2])
    else:
        virtual = drawSignage(device, width=widgetWidth,
                              height=widgetHeight, data=data)

    timeAtStart = time.time()
    timeNow = time.time()
    _fps_frames = 0
    _fps_last = time.time()
    target_fps = 30
    frame_time = 1.0 / target_fps

    while True:
        loop_start = time.time()
        
        # Check for a live pass event from the WebSocket listener
        if config["apiMethod"] == 'describrr':
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

        if live_pass_active:
            if livePassLaps >= 2:
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
            if ws_triggered or timeNow - timeAtStart >= config["refreshTime"]:
                if ws_triggered:
                    refresh_event.clear()
                    logger.info("WebSocket event triggered board refresh")
                if config["apiMethod"] == 'describrr':
                    data = loadDataDescribrr(config["describrr"], config["journey"])
                elif config["apiMethod"] == 'rtt':
                    data = loadDataRTT(config["rttApi"], config["journey"])
                else:
                    data = loadData(config["transportApi"], config["journey"])

                if data[0] == False:
                    virtual = drawBlankSignage(
                        device, width=widgetWidth, height=widgetHeight, departureStation=data[2])
                else:
                    virtual = drawSignage(device, width=widgetWidth,
                                          height=widgetHeight, data=data)

                timeAtStart = time.time()

        timeNow = time.time()
        virtual.refresh()
        _fps_frames += 1
        if timeNow - _fps_last >= 10:
            logger.info("Display refresh rate: %.1f fps", _fps_frames / (timeNow - _fps_last))
            _fps_frames = 0
            _fps_last = timeNow
        
        # Sleep to limit frame rate and reduce CPU usage
        elapsed = time.time() - loop_start
        sleep_time = frame_time - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)

except KeyboardInterrupt:
    pass
except ValueError as err:
    print(f"Error: {err}")
except KeyError as err:
    print(f"Error: Please ensure the {err} environment variable is set")
