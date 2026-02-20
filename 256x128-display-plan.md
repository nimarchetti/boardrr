# 256×128 Display Support — Future Work

## Summary
Extend the display to support a 256×128 OLED (SSD1363), showing 5 departures instead of 3, with a larger clock at the bottom. Full backward compatibility with the existing 256×64 (SSD1322) setup.

---

## Driver situation
SSD1363 is **not** in luma.oled 3.14.0. The supported list ends at `ssd1362` (256×64, same greyscale family).

Options in order of effort:
1. **Try `ssd1362` driver first** — same greyscale family, may work with SSD1363 hardware
2. **Write a minimal luma.oled SSD1363 driver** — extend from `ssd1362` if the init sequence differs
3. **u8g2** (https://github.com/olikraus/u8g2) — C library that supports SSD1363 but would require rewriting all PIL rendering; last resort

All layout code can be tested with `--display pygame --height 128` before having the hardware.

---

## Target layout

### 128px (new)
```
Y=0    Departure 1   HH:MM  Destination       On time  Plat N
Y=12   Calling at: Station1, Station2…  (scrolling)
Y=24   Departure 2
Y=36   Departure 3
Y=48   Departure 4   ← new
Y=60   Departure 5   ← new
       [gap]
Y=98   Clock  HH:MM:SS  (28px tall, larger font)
```

### 64px (unchanged)
```
Y=0    Departure 1
Y=12   Calling at: …
Y=24   Departure 2
Y=36   Departure 3
Y=50   Clock (14px tall, existing fonts)
```

The same code serves both heights by deriving row count and Y positions from the `height` parameter.

---

## Code changes required

### `src/main.py`

**1. Read dimensions from the luma device (lines 335–336)**
```python
# Replace:
widgetWidth = 256
widgetHeight = 64
# With:
widgetWidth = device.width
widgetHeight = device.height
```
`device.width` / `device.height` are already set by luma.core from the `--width` / `--height` CLI args.

**2. Add a larger clock font (after line 333)**
```python
fontBoldXLarge = makeFont("Dot Matrix Bold.ttf", 28)
```

**3. Convert `renderTime` to a closure accepting font parameters**

Currently `renderTime` is a bare function using the globals `fontBoldLarge` and `fontBoldTall`. Convert it to the same closure pattern used by `renderDestination` etc., so the caller can pass size-appropriate fonts:
```python
def renderTime(clockFont, secondsFont):
    def drawText(draw, width, height):
        # ... same body, but use clockFont / secondsFont instead of globals
    return drawText
```
All three `add_hotspot` calls that currently pass `renderTime` directly become:
- 64px: `renderTime(fontBoldLarge, fontBoldTall)`
- 128px: `renderTime(fontBoldXLarge, fontBoldLarge)`

**4. `drawSignage` — derive row count and Y positions from height**
```python
if height >= 100:
    time_y, time_h = 98, 28
    clock_font, sec_font = fontBoldXLarge, fontBoldLarge
    departure_rows = 5
else:
    time_y, time_h = 50, 14
    clock_font, sec_font = fontBoldLarge, fontBoldTall
    departure_rows = 3
```
Add snapshot/hotspot pairs for `departures[3]` and `departures[4]`, each guarded by `if len(departures) > N and departure_rows >= N+1`. Row Y positions: 0, 12, 24, 36, 48, 60 (12px spacing throughout).

**5. `drawBlankSignage` — height-aware clock position**
```python
time_y = 98 if height >= 100 else 50
# centre the three welcome rows in 0..time_y
```

**6. `drawLivePassSignage` — height-aware**
```python
time_y = 98 if height >= 100 else 50
scroll_h = 40 if height >= 100 else 20
```

### `src/trains.py`

Increase the API `limit` in `loadServicesForStationDescribrr` (around line 162) so that 5 upcoming services survive the `passed`/`arrived` filter:
```python
'limit': 10,  # was 5
```

### `run.sh`
```bash
.venv/bin/python3 ./src/main.py --display ssd1362 --width 256 --height 128 --interface spi --mode 1 --rotate 2
```
Change `--display` to `ssd1363` once driver support is confirmed.

---

## Verification steps
1. `--display pygame --width 256 --height 128` — visually check 5-departure layout
2. `--display pygame --width 256 --height 64` — confirm existing 3-departure layout is unchanged
3. On hardware once the display is purchased and driver question resolved
