# Data Schema Reference

## events.jsonl Event Types

### bluetooth
```json
{
  "ts": 1771830522720559000,
  "ts_monotonic": 21403676299708,
  "type": "bluetooth",
  "address": "BE94E0BA-A5A9-1F12-8D2D-9F8E5228E977",
  "name": "Inkeun의 Apple Watch",
  "rssi": -52,
  "tx_power": 0,
  "manufacturer_data": {"76": "10052998cf6554"},
  "service_uuids": [],
  "service_data": {}
}
```

- **ts**: wall clock nanoseconds
- **rssi**: signal strength (dBm). > -40 접촉, -40~-60 1m, -60~-80 같은방, < -80 먼곳
- **manufacturer_data key**: 76=Apple, 6=Microsoft, 117=Samsung, 1373=LG

### context
```json
{
  "ts": 1771830521755163000,
  "type": "context",
  "power": {"source": "Battery Power", "percent": 100, "charging": false},
  "active_app": {"name": "Google Chrome", "bundle_id": "com.google.Chrome", "window_title": "GRIP"},
  "audio_devices": {"output": "다중 출력 기기", "input": "MacBook Pro 마이크"},
  "idle": {"idle_seconds": 1.4},
  "displays": {"count": 1, "builtin": true, "external": 0},
  "meeting_apps": {"running": ["Chrome", "Slack"], "foreground": true}
}
```

### recording
```json
{"ts": ..., "type": "recording", "action": "start|stop", "options": {...}}
```

## bluetooth.jsonl (구버전 포맷)

events.jsonl의 bluetooth 이벤트와 동일하나 `type` 필드 없음. `rssi` 필드 존재 여부로 판별.

## reference_time.json

```json
{
  "wall_ns": 1771830519824921000,
  "monotonic_ns": 21400780662791,
  "iso": "2026-02-23T16:08:39.824940"
}
```

## Fixed Device Keywords (장소 앵커)

TV, Soundbar, Venus(로봇청소기), Jet(로봇청소기), LHB-(IoT허브), AD_402(공기청정기), SS_(삼성IoT)

## Known Location Fingerprints

- **집**: Samsung Soundbar MS650, Venus_A848FA5A2106, AD_402_AIR, 18220A02843245EAD
- **사무실**: Jet V/C, LHB-B9178C01, LHB-CC82DAC7, LHB-8DD7BF7B, SS_517309
