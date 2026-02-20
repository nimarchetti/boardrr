import os
import requests
import json
import threading
import time as time_module
from datetime import date

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
    params = {
        'direction': 'all',
        'from': 'now',
        'to': 'now+2h',
        'limit': 5,
    }
    r = requests.get(f"{host}/v1/boards/{tiploc}", params=params, timeout=10)
    data = r.json()
    station_name = data.get('name') or tiploc

    departures = []
    for e in data.get('entries', []):
        if e.get('status') in ('arrived', 'departed', 'passed'):
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

    dest_name = abbrStation(journeyConfig, stops[-1].get('name') or stops[-1]['tiploc'])

    board_idx = -1
    for i, s in enumerate(stops):
        if s['tiploc'] == board_tiploc:
            board_idx = i
            break

    calling_at = []
    for s in stops[board_idx + 1:]:
        calling_at.append(abbrStation(journeyConfig, s.get('name') or s['tiploc']))

    if len(calling_at) == 1:
        calling_at[0] += ' only.'

    return calling_at, dest_name


def startLivePassListener(journeyConfig, apiConfig, event_queue):
    if websocket is None:
        return

    host = apiConfig['host'].rstrip('/')
    tiploc = apiConfig['tiploc']
    ws_url = host.replace('https://', 'wss://').replace('http://', 'ws://') + f"/v1/ws/boards/{tiploc}"

    _seen_rids = {}  # rid -> float timestamp

    def _fetch_pass_data(rid):
        try:
            r = requests.get(f"{host}/v1/services/{rid}", timeout=10)
            data = r.json()
            stops = data.get('stops', [])
            headcode = data.get('headcode', '????')
            uid = data.get('uid') or ''
            origin = abbrStation(journeyConfig, stops[0].get('name') or stops[0]['tiploc']) if stops else ''
            dest = abbrStation(journeyConfig, stops[-1].get('name') or stops[-1]['tiploc']) if stops else ''
            calling = [abbrStation(journeyConfig, s.get('name') or s['tiploc']) for s in stops]
            return {
                'rid': rid,
                'headcode': headcode,
                'uid': uid,
                'origin': origin,
                'destination': dest,
                'calling_at': calling,
            }
        except Exception:
            return None

    def on_message(ws, message):
        try:
            msg = json.loads(message)
        except Exception:
            return

        if msg.get('type') == 'ping':
            ws.send(json.dumps({'type': 'pong'}))
            return

        if msg.get('type') == 'timing':
            d = msg.get('data', {})
            if d.get('event_type') == 'PASS' and d.get('at'):
                rid = d.get('rid', '')
                if not rid:
                    return

                now = time_module.time()
                cutoff = now - 300
                for old_rid in [k for k, v in list(_seen_rids.items()) if v < cutoff]:
                    del _seen_rids[old_rid]

                if rid in _seen_rids:
                    return
                _seen_rids[rid] = now

                pass_data = _fetch_pass_data(rid)
                if pass_data:
                    event_queue.put(pass_data)

    def run():
        backoff = 2
        while True:
            try:
                ws = websocket.WebSocketApp(ws_url, on_message=on_message)
                ws.run_forever(ping_interval=25, ping_timeout=30)
            except Exception:
                pass
            time_module.sleep(backoff)
            backoff = min(backoff * 2, 30)

    t = threading.Thread(target=run, daemon=True)
    t.start()

