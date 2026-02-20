# Describrr fixes required

Issues found during integration with the UK Train Departure Display. All three are in the `services/api-server` or `services/tracker` packages.

---

## 1. Tracker never publishes to `channel:boards:{tiploc}` — live pass WebSocket is dead

### What's wrong
`handleWSBoards` in `services/api-server/ws.go` subscribes to `channel:boards:{tiploc}` and promises to forward timing events to connected clients. But the tracker **never publishes to that channel**. Grepping all tracker source files finds only two Publish calls, both to `channel:positions:{corridor}`. `channel:boards:*` is never written to.

Result: the boards WebSocket connects successfully and stays open, but never delivers any events. The live pass feature in the display has never worked for this reason.

### Evidence
```bash
grep -rn "channel:" services/tracker/*.go
# Only finds:
#   channel:positions:{corridor}  (position.go, state.go)
#   channel:day-reset             (main.go, db.go)
# No mention of channel:boards anywhere.
```

### Fix required
The tracker should publish a timing event to `channel:boards:{tiploc}` whenever it records an actual time for a stop at that location. This should fire for:

- **PASS events** — when `atp` is set for a PASS-type stop
- **ARR events** — when `ata` is set for an ARR-type stop (train arrives at platform)
- **DEP events** — when `atd` is set (optional, but useful for clearing "at platform" state)

The payload format should match what the display app already expects (and what `handleWSBoards` wraps as `{"type":"timing","data":{...}}`):
```json
{
  "rid": "20260220L36645",
  "event_type": "PASS",
  "at": "12:38:36",
  "tiploc": "SMILFD"
}
```

The tiploc-to-corridor mapping is already available in the tracker state (used for position publishing), so the tracker can determine which tiploc a timing event belongs to.

---

## 2. WebSocket `pongDeadline` is never reset — connection drops every 60 seconds

### What's wrong
`handleWSBoards` (and `handleWSPositions`) in `ws.go` set a 60-second read deadline on connection and configure a `SetPongHandler` to reset it:

```go
conn.SetReadDeadline(time.Now().Add(pongDeadline))  // pongDeadline = 60s
conn.SetPongHandler(func(string) error {
    conn.SetReadDeadline(time.Now().Add(pongDeadline))
    return nil
})
```

The `SetPongHandler` fires when the server **receives** a WebSocket-level PONG frame. This only happens if the server first sends a WebSocket-level PING frame and the client replies. But the server doesn't send WebSocket PING frames — it sends application-level JSON `{"type":"ping"}` messages every 30 seconds instead. The `PongHandler` therefore **never fires**, and the read deadline expires after 60 seconds every time.

### Evidence
Container logs show the connection dropping at exactly 60-second intervals regardless of client activity:
```
12:35:37 - Live pass WebSocket connected
12:36:37 - Live pass WebSocket error: Connection to remote host was lost.  (60s later)
12:36:39 - Live pass WebSocket connected
12:37:40 - Live pass WebSocket error: Connection to remote host was lost.  (61s later)
```

The client correctly responds to application pings with `{"type":"pong"}`, but these are application messages — they don't trigger the gorilla `PongHandler`.

### Fix required
Either:

**Option A (simplest):** Reset the read deadline whenever any message is received from the client, in the existing read goroutine:
```go
go func() {
    for {
        _, _, err := conn.ReadMessage()
        if err != nil {
            cancel()
            return
        }
        conn.SetReadDeadline(time.Now().Add(pongDeadline)) // reset on any read
    }
}()
```

**Option B (more correct):** Replace the application-level JSON ping with a WebSocket-level PING frame so the `PongHandler` actually fires:
```go
case <-ticker.C:
    conn.WriteControl(websocket.PingMessage, nil, time.Now().Add(writeDeadline))
    // Remove the application {"type":"ping"} send
```
The client (gorilla or any conforming library) will respond automatically with a WebSocket PONG, triggering `SetPongHandler` and resetting the deadline. The display app's `on_message` handler for `{"type":"ping"}` can be removed.

Option A is the smallest change and fixes the symptom. Option B is cleaner and aligns with how the gorilla deadline mechanism is intended to work.

---

## 3. Synthetic TD stops appear in `/v1/services/{rid}` stops list

### What's wrong
The stops list returned by `GET /v1/services/{rid}` includes synthetic Train Describer position reports appended after (and sometimes among) the real CIF timetable stops. These entries have:

- Very high sequence numbers (e.g. 1,681,401) — far outside the normal CIF range
- `name: null`
- No scheduled times (`wta`, `wtd`, `wtp` all null)
- Only an actual time (`atp`/`ata`/`atd`) with `time_source: "synthetic_td"`

Example from service 1K14 (Hull → Liverpool Lime Street):
```
seq=50   tiploc=LVRPLSH  name=LIVERPOOL LIME STREET  (real final stop)
seq=1681401  tiploc=MELTNLNLC  name=null  atp=10:12:08  time_source=synthetic_td
seq=1681471  tiploc=BROOMFLT   name=null  atp=10:19:47  time_source=synthetic_td
seq=1681511  tiploc=EASTRNGTN  name=null  atp=10:22:55  time_source=synthetic_td
```

Because these entries sort after the real timetable stops, any consumer that takes `stops[-1]` as the destination gets a nameless TD berth rather than the actual terminus. The TUI service view also shows these spurious entries.

These entries represent the train's position passing through signalling berths — useful for internal tracking, but should not be exposed as timetable stops in the service view.

### Fix required
Filter synthetic TD entries out of the stops list in the `/v1/services/{rid}` response. The cleanest criterion is entries where `time_source = 'synthetic_td'` (or equivalently, where all scheduled times are null and `seq` is abnormally large).

The tracker service should still create and use these entries internally for position tracking — the fix is purely to exclude them from the API response.

The service-level fields `origin` and `destination` (TIPLOCs) are already correct and unaffected; only the `stops` array needs filtering.
