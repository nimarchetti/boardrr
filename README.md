# UK Train Departure Display

A set of Python scripts to display near real-time UK railway service data on SSD13xx OLED screens (256×64 pixels). Originally built to show passenger departures, the display now supports three data sources — including **describrr**, a self-hosted train tracking backend that provides full visibility of all service types: passenger stopping services, passenger and freight passing services, and unscheduled movements.

![](normal.gif)

---

## Data Sources

### Describrr (Recommended for full-service visibility)

[Describrr](https://github.com/nimarchetti/describrr) is a self-hosted train tracking system built to monitor specific railway corridors in real time. It fuses data from three live feeds:

- **Darwin** — the national passenger train running information feed, providing scheduled, estimated, and actual times for passenger services
- **TRUST** — the TOPS Real-time Upgrade to SMART Technology feed, used by Network Rail for operational train movements including freight services
- **TD (Train Describer)** — a berth-level signalling feed that gives near-continuous position updates as trains step through the signalling berths along a line

By combining all three, describrr tracks **every** train moving through a corridor — not just the passenger services that appear on a standard departure board. This includes:

- Passenger services (stopping and passing)
- Freight trains of all classes (coal, intermodal, infrastructure, tankers, etc.)
- Light engine moves and empty coaching stock
- Unscheduled and engineering movements detected from the TD feed

When connected to describrr, the display shows all of these. Passing services — trains that pass through the location without stopping — are shown with a **PASS** label in place of the platform number. When a train is detected passing in real time via describrr's WebSocket feed, the display interrupts the normal board and shows a large scrolling **LIVE PASS** message with the headcode, service UID, origin, destination, and calling points.

The display connects to describrr over HTTPS and WebSocket, and every API/WebSocket request includes an API key in the `X-API-Key` header.

### Real Time Trains API

The [Real Time Trains API](https://www.realtimetrains.co.uk/about/developer/) covers passenger services only. It is a straightforward option if you do not need freight or passing services and do not want to run a local backend.

The free developer tier allows up to 1,000 requests per day.

### Transport API (not recommended)

Transport API now has a free tier of only 30 calls per day, making it impractical for real-time use. It remains supported in the code for users with a commercial agreement.

---

## Installation

Requires Python 3.6 or later.

To install the latest Python on Raspbian, see [this guide](https://gist.github.com/SeppPenner/6a5a30ebc8f79936fa136c524417761d). You may need to alias `python` to point to your installed version — see [this guide](https://linuxconfig.org/how-to-change-from-default-to-alternative-python-version-on-debian-linux).

> **Raspbian Lite:** You will also need to install `libopenjp2-7`:
> ```bash
> sudo apt-get install libopenjp2-7
> ```

Clone this repo, then install dependencies:

```bash
pip3 install -r requirements.txt
```

If using the describrr integration, `websocket-client` is included in `requirements.txt` and will be installed automatically.

---

## Configuration

Copy `config.sample.json` to `config.json` and fill in the relevant sections for your chosen data source.

```json
{
  "journey": {
    "departureStation": "",
    "destinationStation": null,
    "outOfHoursName": "Your Station Name",
    "stationAbbr": {
      "International": "Intl."
    }
  },
  "refreshTime": 180,
  "transportApi": {
    "appId": "",
    "apiKey": "",
    "operatingHours": "0-23"
  },
  "rttApi": {
    "username": "",
    "password": "",
    "operatingHours": "6-23"
  },
  "describrr": {
    "tiploc": "",
    "host": "https://describrr.foro.co.uk",
    "apiKey": "",
    "operatingHours": "6-23"
  },
  "apiMethod": "rtt"
}
```

### General Settings

`refreshTime` — how often (in seconds) to poll the data source for an updated board. Two or more API calls are made on each refresh. With the RTT free tier, 180 seconds keeps comfortably within the daily limit. With describrr the limit is self-imposed so this can be set lower.

### Journey Settings

`departureStation` — the [CRS station code](https://www.nationalrail.co.uk/stations_destinations/48541.aspx) for your display location. Used by RTT and Transport API only.

`destinationStation` — optional CRS code to filter services by destination. Used by Transport API only.

`outOfHoursName` — the station name shown on the blank screen outside operating hours.

`stationAbbr` — a dictionary of text replacements applied to station names, useful for fitting long names on the small display. For example `"International": "Intl."` shortens "Manchester Airport International" to fit.

### Describrr Settings

`tiploc` — the [TIPLOC code](https://wiki.openraildata.com/index.php/TIPLOC) for your display location. TIPLOCs are the internal location identifiers used by the UK rail network — for example `SMILFD` for South Milford or `LEEDS` for Leeds. This must match a location that describrr has in its timetable and berth data.

`host` — the base URL of your describrr API server, for example `https://describrr.foro.co.uk`.

`apiKey` — your describrr API key. This is sent on every HTTP request and as part of the WebSocket upgrade request using the `X-API-Key` header.

`operatingHours` — the range of hours during which the display should actively fetch data, in `HH-HH` format (e.g. `6-23`).

Set `"apiMethod": "describrr"` to activate.

### Real Time Trains API Settings

`username` and `password` — your RTT API credentials from [api.rtt.io](https://api.rtt.io).

`operatingHours` — active hours range.

Set `"apiMethod": "rtt"` to activate.

### Transport API Settings

`appId` and `apiKey` — your Transport API credentials.

`operatingHours` — active hours range.

Set `"apiMethod": "transport"` to activate. Note: the free tier limit of 30 calls per day makes this impractical for continuous real-time use.

---

## Display Behaviour

### Normal board

Up to three services are shown simultaneously, each row displaying:

| Column | Stopping service | Passing service |
|---|---|---|
| Time | Scheduled time | Scheduled time |
| Destination | Final destination | Final destination |
| Status | On time / Exp HH:MM / Cancelled | On time / Exp HH:MM |
| Right-hand label | Plat N | **PASS** |

The second row beneath the first service scrolls through the calling points for that service, regardless of whether it stops or passes through.

### Live pass interruption (describrr only)

When describrr's WebSocket feed fires a real-time timing event confirming a train has passed the display location, the normal board is immediately replaced by a large scrolling message:

```
LIVE PASS  6Z45  Y25686  Doncaster to Leeds  Calling: Milford Jn, Woodlesford, Hunslet, Leeds
```

The message uses the same large font as the clock and scrolls across the display twice before the normal board resumes. The clock remains visible at the bottom throughout.

This fires for all service types — passenger, freight, and unscheduled — as soon as the actual pass time is recorded, with no polling delay.

### Out of hours / no services

Outside the configured operating hours, or when no upcoming services are found, the display shows a welcome screen with the station name and a clock.

---

## Running

There is an example `run.sh` in the root directory for a SSD1322 display over SPI.

```bash
python ./src/main.py --display ssd1322 --width 256 --height 64 --interface spi
```

Change `--display` to alter the output mechanism. Use `pygame` to run a visual emulator on a desktop, or `capture` to save frames to images. A full list of display options is in the [luma.examples README](https://github.com/rm-hull/luma.examples).

Pass `--interface spi` for SPI displays; omit it for I2C.

---

## Example Output

### Normal operating hours
![](normal.gif)

### Out of hours / no trains
![](outofhours.gif)

---

## Video demo

Chris Hutchinson tweeted a video demo of the original software running on a real device: https://twitter.com/chrishutchinson/status/1136743837244768257

---

## Thanks

A big thanks to Chris Hutchinson who originally built this code — find him on GitHub at https://github.com/chrishutchinson/

The fonts were painstakingly put together by `DanielHartUK` and can be found at https://github.com/DanielHartUK/Dot-Matrix-Typeface — a huge thanks for making that resource available.
