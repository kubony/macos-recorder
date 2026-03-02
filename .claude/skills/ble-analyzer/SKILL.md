---
name: ble-analyzer
description: >
  ~/Recordings/ 폴더의 BLE 블루투스 및 녹화 세션 데이터를 분석하는 스킬.
  events.jsonl의 블루투스 이벤트, 컨텍스트 이벤트, 녹화 메타데이터를 파싱하여
  디바이스 식별, 장소 추론, 사람 추적, 근접도 분석, 시간대별 출석 패턴을 제공한다.
  트리거: "BLE 분석", "블루투스 분석", "녹화 분석", "Recordings 분석",
  "디바이스 분석", "누가 있었는지", "장소 추론", "출석 분석", "RSSI 분석",
  "회의 참석자", "세션 분석"
---

# BLE & Recording Analyzer

`~/Recordings/` 폴더의 events.jsonl 데이터를 분석한다.

## 분석 스크립트

`scripts/analyze.py`를 실행. 의존성 없음 (stdlib only). 모든 출력은 JSON.

```bash
python3 .claude/skills/ble-analyzer/scripts/analyze.py <command> [options]
```

### Commands

- **summary**: 전체 세션/디바이스/이벤트 요약
- **devices --top N**: 디바이스 목록 (이름, 제조사, RSSI, 세션 수)
- **locations**: 고정 디바이스 기반 장소 fingerprint
- **presence SESSION_NAME**: 세션 내 5분 간격 디바이스 출현 + 입퇴장 감지
- **cooccurrence --min N**: 여러 세션에 동시 출현하는 디바이스 쌍/그룹
- **proximity SESSION_NAME**: RSSI 기반 거리 분류 (접촉/1m/같은방/먼곳)
- **context --top N**: 사용 앱, 미팅 앱, 배터리, idle 분석
- **timeline --date YYYY-MM-DD**: 날짜별 세션 타임라인
- **diff SESSION1 SESSION2**: 두 세션의 디바이스 차이
- **person DEVICE_NAME**: 특정 디바이스/사람 전 세션 추적

### 분석 패턴

**일상 점검**: `summary` → 전체 현황 파악

**회의 분석**: `presence SESSION` + `proximity SESSION` → 참석자, 입퇴장, 좌석 근접도

**장소 식별**: `locations` → 고정 디바이스 조합으로 장소 자동 분류

**사람 추적**: `person 이름` → 특정 사람의 출현 패턴, `cooccurrence` → 항상 함께 나타나는 디바이스 그룹

**세션 비교**: `diff S1 S2` → 누가 새로 왔고 누가 빠졌는지

### 데이터 스키마

상세 이벤트 구조와 알려진 장소 fingerprint는 `references/data-schema.md` 참조.

### RSSI 거리 기준

- **> -40 dBm**: contact (접촉 거리, 본인 기기)
- **-40 ~ -60**: near (1m 이내, 바로 옆자리)
- **-60 ~ -80**: room (같은 방/회의실)
- **< -80**: far (복도, 옆 방)

### Manufacturer ID

- 76: Apple
- 6: Microsoft
- 117: Samsung
- 1373: LG
