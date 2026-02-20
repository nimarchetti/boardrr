import os
import logging
import requests
import json
import threading
import time as time_module
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

try:
    import websocket
except ImportError:
    websocket = None

def abbrStation(journeyConfig, inputStr):
    inputStr = inputStr.title()
    dict = journeyConfig['stationAbbr']
    for key in dict.keys():
        inputStr = inputStr.replace(key, dict[key])
    return inputStr

def loadDeparturesForStationRTT(journeyConfig, username, password):
    if journeyConfig["departureStation"] == "":
        raise ValueError(
            "Please set the journey.departureStation property in config.json")

    if username == "" or password == "":
        raise ValueError(
            "Please complete the rttApi section of your config.json file")

    departureStation = journeyConfig["departureStation"]

    response = requests.get(f"https://api.rtt.io/api/v1/json/search/{departureStation}", auth=(username, password))
    data = response.json()
    translated_departures = []
    td = date.today()

    if data['services'] is None:
        return translated_departures, departureStation

    for item in data['services'][:5]:
        uid = item['serviceUid']
        destination_name = abbrStation(journeyConfig, item['locationDetail']['destination'][0]['description'])

        dt = item['locationDetail']['gbttBookedDeparture']
        try:
            edt = item['locationDetail']['realtimeDeparture']
        except:
            edt = item['locationDetail']['gbttBookedDeparture']

        aimed_departure_time = dt[:2] + ':' + dt[2:]
        expected_departure_time = edt[:2] + ':' + edt[2:]
        status = item['locationDetail']['displayAs']
        mode = item['serviceType']
        try:
            platform = item['locationDetail']['platform']
        except:
            platform = ""

        translated_departures.append({'uid': uid, 'destination_name': abbrStation(journeyConfig, destination_name), 'aimed_departure_time': aimed_departure_time, 
                                        'expected_departure_time': expected_departure_time,
                                        'status': status, 'mode': mode, 'platform': platform,
                                        'time_table_url': f"https://api.rtt.io/api/v1/json/service/{uid}/{td.year}/{td.month:02}/{td.day:02}"})

    return translated_departures, departureStation

def loadDestinationsForDepartureRTT(journeyConfig, username, password, timetableUrl):
    r = requests.get(url=timetableUrl, auth=(username, password))
    calling_data = r.json()

    index = 0
    for loc in calling_data['locations']:
        if loc['crs'] == journeyConfig["departureStation"]:
            break
        index += 1

    calling_at = []    
    for loc in calling_data['locations'][index+1:]:
        calling_at.append(abbrStation(journeyConfig, loc['description']))

    if len(calling_at) == 1:
        calling_at[0] = calling_at[0] + ' only.'

    return calling_at

def loadDeparturesForStation(journeyConfig, appId, apiKey):
    if journeyConfig["departureStation"] == "":
        raise ValueError(
            "Please set the journey.departureStation property in config.json")

    if appId == "" or apiKey == "":
        raise ValueError(
            "Please complete the transportApi section of your config.json file")

    departureStation = journeyConfig["departureStation"]

    URL = f"http://transportapi.com/v3/uk/train/station/{departureStation}/live.json"

    PARAMS = {'app_id': appId,
              'app_key': apiKey,
              'calling_at': journeyConfig["destinationStation"]}

    r = requests.get(url=URL, params=PARAMS)

    data = r.json()
    #apply abbreviations / replacements to station names (long stations names dont look great on layout)
    #see config file for replacement list 
    for item in data["departures"]["all"]:
         item['origin_name'] = abbrStation(journeyConfig, item['origin_name'])
         item['destination_name'] = abbrStation(journeyConfig, item['destination_name'])

    if "error" in data:
        raise ValueError(data["error"])

    return data["departures"]["all"], data["station_name"]


def loadDestinationsForDeparture(journeyConfig, timetableUrl):
    r = requests.get(url=timetableUrl)

    data = r.json()

    #apply abbreviations / replacements to station names (long stations names dont look great on layout)
    #see config file for replacement list
    foundDepartureStation = False

    for item in list(data["stops"]):
        if item['station_code'] == journeyConfig['departureStation']:
            foundDepartureStation = True

        if foundDepartureStation == False:
            data["stops"].remove(item)
            continue

        item['station_name'] = abbrStation(journeyConfig, item['station_name'])

    if "error" in data:
        raise ValueError(data["error"])

    departureDestinationList = list(map(lambda x: x["station_name"], data["stops"]))[1:]

    if len(departureDestinationList) == 1:
        departureDestinationList[0] = departureDestinationList[0] + ' only.'

    return departureDestinationList


# ─────────────────────────────────────────────────────────────────────────────
# Describrr API functions
# ─────────────────────────────────────────────────────────────────────────────

def _pick_time(primary, *fallbacks):
    """Return the first non-None time string sliced to HH:MM."""
    for t in (primary,) + fallbacks:
        if t:
            return t[:5]
    return None


def loadServicesForStationDescribrr(journeyConfig, apiConfig):
    tiploc = apiConfig['tiploc']
    host = apiConfig['host'].rstrip('/')
    now = datetime.now()
    # Query from 3 minutes ago so recently-departed services are still returned.
    from_time = (now - timedelta(minutes=3)).strftime('%H:%M')
    params = {
        'direction': 'all',
        'from': from_time,
        'to': 'now+2h',
        'limit': 10,
    }
    r = requests.get(f"{host}/v1/boards/{tiploc}", params=params, timeout=10)
    data = r.json() or {}
    station_name = data.get('name') or tiploc

    departures = []
    for e in data.get('entries', []):
        api_status = e.get('status', '')

        # PASS events that have already passed through are useless.
        if api_status == 'passed':
            continue

        # Departed services: keep for 2 minutes so row 1 can show "Dep HH:MM".
        if api_status == 'departed':
            atd = e.get('atd')
            if not atd:
                continue
            try:
                parts = atd.split(':')
                dep_dt = now.replace(
                    hour=int(parts[0]), minute=int(parts[1]),
                    second=int(parts[2]) if len(parts) > 2 else 0,
                    microsecond=0)
                if (now - dep_dt).total_seconds() > 120:
                    continue
            except (ValueError, IndexError):
                continue

        event_type = e.get('event_type', 'DEP')

        if event_type == 'PASS':
            wt = _pick_time(e.get('wtp'), e.get('wta'), e.get('wtd'))
            et = _pick_time(e.get('etp'), e.get('eta'), e.get('etd'),
                            e.get('atp'), e.get('ata'), e.get('atd'), wt)
        elif event_type == 'ARR':
            wt = _pick_time(e.get('wta'), e.get('wtd'), e.get('wtp'))
            et = _pick_time(e.get('eta'), e.get('ata'),
                            e.get('etd'), e.get('atd'),
                            e.get('etp'), e.get('atp'), wt)
        else:  # DEP
            wt = _pick_time(e.get('wtd'), e.get('wta'), e.get('wtp'))
            et = _pick_time(e.get('etd'), e.get('atd'),
                            e.get('eta'), e.get('ata'),
                            e.get('etp'), e.get('atp'), wt)

        if wt is None:
            continue

        status = 'CANCELLED' if e.get('cancelled') else e.get('status', 'scheduled')

        departures.append({
            'rid': e['rid'],
            'destination_name': '',
            'aimed_departure_time': wt,
            'expected_departure_time': et or wt,
            'status': status,
            'mode': 'pass' if event_type == 'PASS' else 'train',
            'platform': e.get('platform') or '',
            'event_type': event_type,
            'ata': e.get('ata'),
            'atd': e.get('atd'),
        })

    return departures, station_name


def loadDestinationsForServiceDescribrr(journeyConfig, apiConfig, rid):
    host = apiConfig['host'].rstrip('/')
    board_tiploc = apiConfig['tiploc']
    r = requests.get(f"{host}/v1/services/{rid}", timeout=10)
    data = r.json()

    stops = data.get('stops', [])
    if not stops:
        return [], ''

    # Use the service's declared destination TIPLOC to find the destination name.
    # stops[-1] is unreliable because synthetic TD position reports (name=None)
    # are appended after the real final timetable stop.
    dest_tiploc = data.get('destination')
    dest_name = ''
    if dest_tiploc:
        for s in stops:
            if s['tiploc'] == dest_tiploc and s.get('name'):
                dest_name = abbrStation(journeyConfig, s['name'])
                break
    if not dest_name:
        for s in reversed(stops):
            if s.get('name'):
                dest_name = abbrStation(journeyConfig, s['name'])
                break
    if not dest_name:
        dest_name = abbrStation(journeyConfig, dest_tiploc or stops[-1]['tiploc'])

    board_idx = -1
    for i, s in enumerate(stops):
        if s['tiploc'] == board_tiploc:
            board_idx = i
            break

    calling_at = []
    for s in stops[board_idx + 1:]:
        if s.get('name'):
            calling_at.append(abbrStation(journeyConfig, s['name']))

    if len(calling_at) == 1:
        calling_at[0] += ' only.'

    return calling_at, dest_name


def startLivePassListener(journeyConfig, apiConfig, event_queue, refresh_event=None):
    if websocket is None:
        logger.warning("websocket-client not installed — live pass listener disabled")
        return

    host = apiConfig['host'].rstrip('/')
    tiploc = apiConfig['tiploc']
    ws_url = host.replace('https://', 'wss://').replace('http://', 'ws://') + f"/v1/ws/boards/{tiploc}"

    logger.info("Live pass listener starting — connecting to %s", ws_url)

    _seen_rids = {}  # rid -> float timestamp

    def _fetch_pass_data(rid):
        try:
            r = requests.get(f"{host}/v1/services/{rid}", timeout=10)
            data = r.json()
            stops = data.get('stops', [])
            headcode = data.get('headcode', '????')
            uid = data.get('uid') or ''
            origin = abbrStation(journeyConfig, stops[0].get('name') or stops[0]['tiploc']) if stops else ''
            dest_tiploc = data.get('destination')
            dest = ''
            if dest_tiploc:
                for s in stops:
                    if s['tiploc'] == dest_tiploc and s.get('name'):
                        dest = abbrStation(journeyConfig, s['name'])
                        break
            if not dest:
                for s in reversed(stops):
                    if s.get('name'):
                        dest = abbrStation(journeyConfig, s['name'])
                        break
            calling = [abbrStation(journeyConfig, s['name']) for s in stops if s.get('name')]
            return {
                'rid': rid,
                'headcode': headcode,
                'uid': uid,
                'origin': origin,
                'destination': dest,
                'calling_at': calling,
            }
        except Exception as e:
            logger.warning("Failed to fetch service data for RID %s: %s", rid, e)
            return None

    def on_open(ws):
        logger.info("Live pass WebSocket connected to %s", ws_url)

    def on_message(ws, message):
        try:
            msg = json.loads(message)
        except Exception as e:
            logger.debug("Failed to parse WebSocket message: %s", e)
            return

        msg_type = msg.get('type')
        logger.debug("WebSocket message received: type=%s", msg_type)

        if msg_type == 'ping':
            ws.send(json.dumps({'type': 'pong'}))
            return

        if msg_type == 'timing':
            d = msg.get('data', {})
            event_type = d.get('event_type')
            at = d.get('at')
            rid = d.get('rid', '')
            logger.debug("Timing event: event_type=%s rid=%s at=%s", event_type, rid, at)

            if event_type == 'PASS' and at:
                if not rid:
                    logger.debug("Timing PASS event has no RID, skipping")
                    return

                now = time_module.time()
                cutoff = now - 300
                for old_rid in [k for k, v in list(_seen_rids.items()) if v < cutoff]:
                    del _seen_rids[old_rid]

                if rid in _seen_rids:
                    logger.debug("RID %s already seen, skipping duplicate PASS event", rid)
                    return
                _seen_rids[rid] = now

                logger.info("Live PASS detected: RID=%s — fetching service data", rid)
                pass_data = _fetch_pass_data(rid)
                if pass_data:
                    logger.info("Queuing live pass display: %s %s %s→%s",
                                pass_data['headcode'], pass_data['uid'],
                                pass_data['origin'], pass_data['destination'])
                    event_queue.put(pass_data)
                else:
                    logger.warning("Could not fetch service data for PASS RID %s — not displaying", rid)

            elif event_type in ('ARR', 'DEP') and at and refresh_event is not None:
                logger.info("Timing %s at %s for RID %s — triggering board refresh", event_type, tiploc, rid)
                refresh_event.set()

    def on_error(ws, error):
        logger.warning("Live pass WebSocket error: %s", error)

    def on_close(ws, close_status_code, close_msg):
        logger.warning("Live pass WebSocket closed (code=%s msg=%s)", close_status_code, close_msg)

    def run():
        backoff = 2
        while True:
            try:
                logger.debug("Live pass WebSocket connecting (backoff=%ds)", backoff)
                ws = websocket.WebSocketApp(
                    ws_url,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logger.warning("Live pass WebSocket run_forever raised: %s", e)
            time_module.sleep(backoff)
            backoff = min(backoff * 2, 30)
            logger.info("Live pass WebSocket reconnecting in %ds", backoff)

    t = threading.Thread(target=run, daemon=True)
    t.start()

