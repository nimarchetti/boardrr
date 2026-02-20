# Plan: Go Rewrite of Train Departure Display

## Context
User wants to explore a Go rewrite of the Python/luma.core display app, keeping Docker.
Performance is not the driver — this is about learning Go and having a compiled, single-binary
deployment. The existing Python implementation stays on `master`; Go work goes on a new branch.

---

## Branch
```bash
git checkout -b go-rewrite
```

---

## Project layout

New top-level directory `go-display/` alongside `src/`:

```
go-display/
├── cmd/display/main.go          # Config loading, main loop, goroutine wiring
├── internal/
│   ├── ssd1322/driver.go        # SPI driver (ported from luma.oled)
│   ├── render/renderer.go       # Framebuffer, font loading, text draw helpers
│   ├── board/
│   │   ├── board.go             # drawSignage / drawBlankSignage equivalents
│   │   └── scroll.go            # Pixel-scroll state machine
│   ├── api/describrr.go         # HTTP client (boards + services endpoints)
│   └── ws/listener.go           # WebSocket listener + reconnect loop
├── fonts/                       # Symlink → ../src/fonts (reuse existing TTFs)
├── go.mod
├── Dockerfile.go                # Multi-stage Go build → debian slim runtime
└── docker-compose.go.yml        # Separate compose file for Go service
```

---

## Libraries

| Purpose | Package |
|---------|---------|
| SPI bus | `periph.io/x/conn/v3/spi/spireg` |
| GPIO (DC/RST pins) | `periph.io/x/conn/v3/gpio/gpioreg` |
| periph.io driver init | `periph.io/x/host/v3` |
| TTF font loading + text | `golang.org/x/image/font/opentype` |
| Image buffer | `image`, `image/draw` (stdlib) |
| HTTP client | `net/http` (stdlib) |
| JSON | `encoding/json` (stdlib) |
| WebSocket | `github.com/gorilla/websocket` (battle-tested; coder/websocket also fine) |

---

## SSD1322 driver (`internal/ssd1322/driver.go`)

**Key facts from luma.oled source:**

| Parameter | Value |
|-----------|-------|
| SPI DC pin | GPIO 24 (default) |
| SPI RST pin | GPIO 25 (default) |
| Column offset | `(480 - 256) / 2 = 112` pixels → address 28 |
| Column address unit | 4 pixels (divide x by 4) |
| Framebuffer size | `256 × 64 / 2 = 8192 bytes` |
| Pixel packing | 2 pixels/byte; even pixel → upper nibble [7:4], odd → lower [3:0] |
| DC LOW | command mode |
| DC HIGH | data mode |

**Init sequence (port directly from luma.oled):**
```go
cmds := [][]byte{
    {0xFD, 0x12},         // Unlock IC
    {0xA4},               // All pixels off
    {0xB3, 0xF2},         // Clock divider/freq
    {0xCA, 0x3F},         // MUX ratio (64)
    {0xA2, 0x00},         // Display offset
    {0xA1, 0x00},         // Start line
    {0xA0, 0x14, 0x11},   // Remap + dual COM
    {0xB5, 0x00},         // GPIO disabled
    {0xAB, 0x01},         // Function select (internal Vdd)
    {0xB4, 0xA0, 0xFD},   // Enhancement A
    {0xC7, 0x0F},         // Master contrast
    {0xB9},               // Default greyscale table
    {0xB1, 0xF0},         // Phase length
    {0xD1, 0x82, 0x20},   // Enhancement B
    {0xBB, 0x0D},         // Pre-charge voltage
    {0xB6, 0x08},         // 2nd pre-charge period
    {0xBE, 0x00},         // VCOMH
    {0xA6},               // Normal display
    {0xA9},               // Exit partial
    {0xAF},               // Display ON
}
```

**Display method:**
```go
func (d *Device) Display(img *image.Gray) {
    // Set column/row window
    colStart := (112 + 0) / 4   // = 28
    colEnd   := (112 + 256) / 4 - 1  // = 91
    d.command(0x15, byte(colStart), byte(colEnd))
    d.command(0x75, 0, 63)
    d.command(0x5C) // write RAM

    // Pack pixels: 2 per byte, upper nibble first
    buf := make([]byte, 256*64/2)
    pix := img.Pix
    for i := 0; i < len(pix); i += 2 {
        hi := pix[i] >> 4
        lo := pix[i+1] >> 4
        buf[i/2] = (hi << 4) | lo
    }
    d.data(buf)
}
```

---

## Renderer (`internal/render/renderer.go`)

Equivalent of luma.core's `canvas` + PIL `ImageDraw`:

```go
type Renderer struct {
    buf   *image.Gray        // 256×64 working framebuffer
    fonts map[string]font.Face  // "regular", "bold", "boldTall", "boldLarge"
}

// DrawText renders text at (x,y) in the given font face.
// Equivalent of draw.text((x,y), text=t, font=f, fill="yellow")
func (r *Renderer) DrawText(x, y int, text string, face font.Face)

// MeasureText returns pixel width & height of text in face.
// Equivalent of _textsize(text, font)
func (r *Renderer) MeasureText(text string, face font.Face) (w, h int)

// Clear fills framebuffer with black.
func (r *Renderer) Clear()
```

Font loading uses `opentype.NewFace(data, &opentype.FaceOptions{Size: 10, DPI: 72})`.

---

## Board layout (`internal/board/board.go`)

Replaces `drawSignage` / `drawBlankSignage` / `drawSignageWithLivePass`.
Instead of luma's viewport/snapshot interval model, use a **simple timed render loop**:

```go
// Called at ~60fps from main loop
func (b *Board) Render(r *Renderer, now time.Time) {
    r.Clear()
    b.renderRow1(r, now)      // departure 1 + status + platform
    b.renderCallingAt(r)      // "Calling at:" label
    b.renderStations(r, now)  // scrolling calling points
    b.renderRow2(r)           // departure 2
    b.renderRow3(r)           // departure 3
    b.renderClock(r, now)     // HH:MM:SS
}
```

Scroll state machine (`board/scroll.go`) tracks pixel offset + pause counter,
same logic as Python `renderStationsPixel` / `renderLivePassTextPixel`.

---

## Concurrency model

```
main goroutine:  60fps render loop → display.Display(frame)
ws goroutine:    WebSocket → livePassCh chan PassEvent
                             refreshCh  chan struct{}
HTTP refresh:    ticker or on-demand via refreshCh
```

Channels replace Python's `queue.Queue` and `threading.Event`.

---

## Docker (`Dockerfile.go`)

Multi-stage build keeps the runtime image tiny (~25 MB):

```dockerfile
FROM golang:1.23 AS builder
WORKDIR /build
COPY go-display/ .
RUN go build -o display ./cmd/display/

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /build/display .
COPY src/fonts/ ./fonts/
ENTRYPOINT ["./display"]
```

`docker-compose.go.yml` mirrors the existing compose file (privileged, network_mode host,
config.json volume) but uses `Dockerfile.go` and the Go binary entrypoint flags:
```
--spi SPI0.0 --dc 24 --rst 25
```

---

## Implementation order

1. **`internal/ssd1322/driver.go`** — SPI init, `Display(*image.Gray)`, `Clear()`
   _Test_: write a solid rectangle, confirm pixels light up on hardware
2. **`internal/render/renderer.go`** — font loading, `DrawText`, `MeasureText`
   _Test_: render "Hello 12:34" to PNG file to verify layout
3. **`internal/board/board.go`** (static) — hardcoded departure data, no scroll
   _Test_: full board visible on display, clock updating
4. **`internal/api/describrr.go`** — boards + services HTTP calls
   _Test_: live data appears on display
5. **`internal/board/scroll.go`** — pixel scroll state machine
   _Test_: calling points scroll smoothly
6. **`internal/ws/listener.go`** — WebSocket + reconnect + PASS/ARR/DEP events
   _Test_: live pass fires; ARR/DEP triggers refresh
7. **Dockerfile.go + docker-compose.go.yml**
   _Test_: `docker compose -f docker-compose.go.yml up` runs cleanly
8. **`cmd/display/main.go`** — wires everything, reads config.json
   _Test_: full end-to-end, config switches work

---

## Files to create

- `go-display/cmd/display/main.go`
- `go-display/internal/ssd1322/driver.go`
- `go-display/internal/render/renderer.go`
- `go-display/internal/board/board.go`
- `go-display/internal/board/scroll.go`
- `go-display/internal/api/describrr.go`
- `go-display/internal/ws/listener.go`
- `go-display/go.mod`
- `go-display/Dockerfile.go`
- `go-display/docker-compose.go.yml`

## Files unchanged

Everything in `src/` — Python version stays on master and this branch.
`src/fonts/` is reused by the Go binary (copied into Docker image).
`config.json` is reused unchanged.

---

## Open question: emulator for development

luma has a pygame emulator (`--display pygame`). For Go development without having the
physical display attached, we can render to a PNG file on each frame and use a file watcher
to preview it, or write a simple framebuffer-to-PNG debug mode controlled by a `--debug` flag.
